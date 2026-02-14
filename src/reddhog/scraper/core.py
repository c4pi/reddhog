import asyncio
import contextlib
from datetime import UTC, datetime
import logging
from pathlib import Path
import time

import httpx
from tqdm import tqdm

from reddhog.clients import (
    BrowserRateLimitError,
    ImageDownloader,
    RedditBrowserClient,
    RedditJSONClient,
)
from reddhog.clients.base import _is_closed_error
from reddhog.config import (
    BROWSER_COOLDOWN_FALLBACK_SECONDS,
    BROWSER_ROTATION_QUOTA,
    DEFAULT_CONCURRENCY,
    IMG_CONCURRENCY,
    IMG_DIR,
    SCRAPER_DATA,
    TABS_PER_BROWSER,
)
from reddhog.models import Image, Post, merge_comment_lists
from reddhog.persistence import DataManager
from reddhog.utils import extract_post_id_from_url, extract_subreddit_from_url, utc_to_iso

logger = logging.getLogger("reddit_scraper")

CONSECUTIVE_EXISTING_THRESHOLD = 100


class _BrowserSlot:
    __slots__ = ("browser", "inflight", "quota_remaining", "slot_lock", "tab_sem")

    def __init__(self, tabs_per_browser: int, rotation_quota: int):
        self.tab_sem = asyncio.Semaphore(tabs_per_browser)
        self.inflight = 0
        self.quota_remaining = rotation_quota
        self.browser: RedditBrowserClient | None = None
        self.slot_lock = asyncio.Lock()


class _BrowserLease:
    def __init__(self, pool: "_BrowserPool", slot: _BrowserSlot):
        self.pool = pool
        self._slot = slot
        self._browser: RedditBrowserClient | None = None

    async def __aenter__(self) -> RedditBrowserClient:
        await self._slot.tab_sem.acquire()
        async with self._slot.slot_lock:
            if self._slot.browser is None:
                self._slot.browser = RedditBrowserClient(headless=self.pool.headless)
                await self._slot.browser.start()
            self._browser = self._slot.browser
            self._slot.inflight += 1
            self._slot.quota_remaining -= 1
        return self._browser

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        async with self._slot.slot_lock:
            self._slot.inflight -= 1
            need_replace = (
                self._slot.quota_remaining == 0 and self._slot.inflight == 0
            )
            old_browser = None
            if need_replace and self._slot.browser is not None:
                old_browser = self._slot.browser
                self._slot.browser = None
                self._slot.quota_remaining = self.pool.rotation_quota
        self._slot.tab_sem.release()
        if old_browser is not None:
            try:
                await old_browser.close()
            except Exception as e:
                logger.debug("Browser close during rotation: %s", e)


class _BrowserPool:
    def __init__(
        self,
        concurrency: int,
        tabs_per_browser: int,
        rotation_quota: int,
        headless: bool,
    ):
        self.headless = headless
        self._tabs_per_browser = tabs_per_browser
        self.rotation_quota = rotation_quota
        n = max(1, (concurrency + tabs_per_browser - 1) // tabs_per_browser)
        self._slots: list[_BrowserSlot] = [
            _BrowserSlot(tabs_per_browser, rotation_quota) for _ in range(n)
        ]
        self._index = 0
        self._pool_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def acquire_lease(self):
        async with self._pool_lock:
            idx = self._index % len(self._slots)
            self._index += 1
            slot = self._slots[idx]
        lease = _BrowserLease(self, slot)
        async with lease as browser:
            yield browser

    async def close_all(self) -> None:
        for slot in self._slots:
            async with slot.slot_lock:
                old = slot.browser
                slot.browser = None
                slot.quota_remaining = self.rotation_quota
            if old is not None:
                try:
                    await old.close()
                except Exception as e:
                    logger.debug("Browser close_all: %s", e)
        self._index = 0


class RedditScraper:
    def __init__(
        self,
        strategy: str = "auto",
        concurrency: int = DEFAULT_CONCURRENCY,
        headless: bool = True,
    ):
        self.strategy = strategy
        self.concurrency = concurrency
        self.headless = headless
        self.data_dir: Path | None = None
        self.data_manager: DataManager | None = None
        self.image_downloader = ImageDownloader(IMG_CONCURRENCY)
        self._json: RedditJSONClient = RedditJSONClient(concurrency)
        self._browser_pool: _BrowserPool | None = None
        self._browser_pool_lock = asyncio.Lock()
        self.browser_cooldown_until: float = 0.0
        self._listing_backend: str | None = None
        self._scrape_backend: str | None = None
        self._strategy_notices: set[str] = set()

    def _ensure_data_dir(self, subreddit: str) -> None:
        normalized = subreddit.strip().lower()
        self.data_dir = SCRAPER_DATA / normalized
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "images").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "debug").mkdir(parents=True, exist_ok=True)

    def _ensure_browser_pool(self) -> None:
        if self._browser_pool is None:
            self._browser_pool = _BrowserPool(
                self.concurrency, TABS_PER_BROWSER, BROWSER_ROTATION_QUOTA, self.headless
            )

    @contextlib.asynccontextmanager
    async def _acquire_browser_lease(self):
        async with self._browser_pool_lock:
            self._ensure_browser_pool()
            pool = self._browser_pool
        async with pool.acquire_lease() as browser:
            yield browser

    def _json_available(self) -> bool:
        if self.strategy == "browser":
            return False
        return time.monotonic() >= self._json.json_disabled_until

    def _browser_available(self) -> bool:
        if self.strategy == "json":
            return False
        return time.monotonic() >= self.browser_cooldown_until

    async def _wait_until(self, ready_at: float) -> None:
        wait_sec = ready_at - time.monotonic()
        if wait_sec <= 0:
            return
        await asyncio.sleep(wait_sec)

    async def _wait_for_listing_backend_ready(self) -> None:
        if time.monotonic() >= self._json.json_disabled_until:
            return
        await self._wait_until(self._json.json_disabled_until)

    async def _wait_for_post_backend_ready(self) -> None:
        waits: list[float] = []
        if self.strategy != "browser":
            waits.append(self._json.json_disabled_until)
        if self.strategy != "json":
            waits.append(self.browser_cooldown_until)
        if not waits:
            raise RuntimeError("No backend available for post scraping")
        await self._wait_until(min(waits))

    def _log_backend_switch(
        self,
        phase: str,
        backend: str,
        *,
        reason: str | None = None,
    ) -> None:
        attr_name = "_listing_backend" if phase == "listing" else "_scrape_backend"
        previous = getattr(self, attr_name)
        if previous == backend:
            return
        message = f"{phase.capitalize()} backend: {backend.upper()}"
        if reason:
            message = f"{message} ({reason})"
        logger.info(message)
        setattr(self, attr_name, backend)

    def _log_strategy_notice(self, key: str, message: str) -> None:
        if key in self._strategy_notices:
            return
        self._strategy_notices.add(key)
        logger.info(message)

    def _set_browser_cooldown(self, cooldown_seconds: float | None) -> float:
        cooldown = (
            cooldown_seconds
            if cooldown_seconds is not None
            else BROWSER_COOLDOWN_FALLBACK_SECONDS
        )
        self.browser_cooldown_until = time.monotonic() + cooldown
        return cooldown

    async def close(self) -> None:
        await self.image_downloader.close()
        await self._json.close()
        if self._browser_pool is not None:
            await self._browser_pool.close_all()

    async def _download_post_images(
        self, post_id: str, image_urls: list[str], browser_page=None
    ) -> list[Image]:
        result: list[Image] = []
        img_dir = self.data_dir / "images" if self.data_dir else Path(IMG_DIR)
        items = [(url, str(img_dir / f"{post_id}_detail_{i}.jpg")) for i, url in enumerate(image_urls)]
        if not items:
            return result
        await self.image_downloader.download_batch(items, browser_page)
        for url, path in items:
            if Path(path).exists():
                result.append(Image(url=url.split("?")[0], local_path=str(path)))
        return result

    def _json_available_for_listing(self) -> bool:
        return time.monotonic() >= self._json.json_disabled_until

    @staticmethod
    def _is_retriable_json_status(status_code: int) -> bool:
        return status_code in (403, 429)

    async def _get_listing(
        self, subreddit: str, after: str | None = None
    ) -> tuple[list[Post], str | None]:
        debug_dir = (self.data_dir / "debug") if self.data_dir else None
        while True:
            if self._json_available_for_listing():
                try:
                    reason = "resumed JSON after cooldown" if self._listing_backend == "browser" else None
                    self._log_backend_switch("listing", "json", reason=reason)
                    return await self._json.get_subreddit_posts(subreddit, after)
                except httpx.HTTPStatusError as e:
                    status_code = e.response.status_code if e.response is not None else 0
                    if status_code >= 500 or not self._is_retriable_json_status(status_code):
                        raise
            elif self.strategy == "json":
                self._log_strategy_notice(
                    "listing-json-only",
                    "Listing strategy=json: browser listing fallback disabled while JSON cools down.",
                )

            if self.strategy != "json" and self._browser_available():
                try:
                    reason = "fallback from JSON" if self._listing_backend == "json" else None
                    self._log_backend_switch("listing", "browser", reason=reason)
                    async with self._acquire_browser_lease() as browser:
                        return await browser.get_subreddit_posts(
                            subreddit,
                            after,
                            debug_dir=debug_dir,
                        )
                except BrowserRateLimitError as e:
                    cooldown = self._set_browser_cooldown(e.cooldown_seconds)
                    if self._browser_pool is not None:
                        await self._browser_pool.close_all()
                    logger.info("Listing browser cooldown %.0fs", cooldown)
                except Exception as e:
                    if _is_closed_error(e):
                        continue
                    raise

            if self.strategy == "json":
                await self._wait_for_listing_backend_ready()
                continue
            await self._wait_for_post_backend_ready()

    async def _get_post(
        self, subreddit: str, post_id: str, skip_images: bool = False
    ) -> Post:
        img_dir = str(self.data_dir / "images") if self.data_dir else str(IMG_DIR)
        debug_dir = (self.data_dir / "debug") if self.data_dir else None
        while True:
            if self._json_available():
                try:
                    reason = "resumed JSON after cooldown" if self._scrape_backend == "browser" else None
                    self._log_backend_switch("scraping", "json", reason=reason)
                    post_data, comments = await self._json.get_post_details(
                        subreddit, post_id
                    )
                    image_urls = (
                        []
                        if skip_images
                        else self._json.extract_image_urls(post_data)
                    )
                    images = await self._download_post_images(
                        post_id, image_urls
                    )
                    clean_id = post_id.replace("t3_", "")
                    return Post(
                        id=post_id,
                        title=post_data.get("title", ""),
                        flair=post_data.get("link_flair_text") or "",
                        description=post_data.get("selftext") or "",
                        url=f"https://www.reddit.com/r/{subreddit}/comments/{clean_id}/",
                        upvotes=int(post_data.get("score", 0)),
                        comments_count=int(post_data.get("num_comments", 0)),
                        author=post_data.get("author") or "unknown",
                        timestamp=utc_to_iso(post_data.get("created_utc", 0)),
                        images=images,
                        comments=comments,
                        crawled_at=datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    )
                except httpx.HTTPStatusError as e:
                    status_code = e.response.status_code if e.response is not None else 0
                    if status_code >= 500 or not self._is_retriable_json_status(status_code):
                        raise
            elif self.strategy == "browser":
                self._log_strategy_notice(
                    "scraping-browser-only",
                    "Scraping strategy=browser: JSON post scraping disabled; using browser-only retries.",
                )
            if self.strategy == "json":
                self._log_strategy_notice(
                    "scraping-json-only",
                    "Scraping strategy=json: browser post fallback disabled while JSON cools down.",
                )
                await self._wait_for_post_backend_ready()
                continue
            if not self._browser_available():
                await self._wait_for_post_backend_ready()
                continue
            try:
                reason = "fallback from JSON" if self._scrape_backend == "json" else None
                self._log_backend_switch("scraping", "browser", reason=reason)
                async with self._acquire_browser_lease() as browser:
                    post, _, _ = await browser.get_post_details(
                        subreddit,
                        post_id,
                        img_dir,
                        skip_images=skip_images,
                        debug_dir=debug_dir,
                    )
                return post
            except BrowserRateLimitError as e:
                cooldown = self._set_browser_cooldown(e.cooldown_seconds)
                if self._browser_pool is not None:
                    await self._browser_pool.close_all()
                logger.info("Scraping browser cooldown %.0fs", cooldown)
                await self._wait_for_post_backend_ready()
            except Exception as e:
                if _is_closed_error(e):
                    continue
                raise

    async def _fill_post(self, post: Post, subreddit: str) -> None:
        try:
            full_post = await self._get_post(subreddit, post.id)
            for field in (
                "title", "flair", "description", "upvotes", "comments_count",
                "author", "timestamp", "comments", "images", "crawled_at",
            ):
                setattr(post, field, getattr(full_post, field))
        except Exception as e:
            logger.warning(f"Failed to fill {post.id}: {e}")
        await self.data_manager.upsert_and_save(post)  # type: ignore[union-attr]

    async def _refresh_post(self, post: Post, subreddit: str) -> bool:
        try:
            full_post = await self._get_post(subreddit, post.id, skip_images=True)
            post.upvotes = full_post.upvotes
            post.comments_count = full_post.comments_count
            post.crawled_at = full_post.crawled_at
            post.comments = merge_comment_lists(post.comments, full_post.comments)
            return True
        except Exception as e:
            logger.warning(f"Failed to refresh {post.id}: {e}")
            return False

    async def _collect_posts(
        self,
        subreddit: str,
        existing_ids: set[str],
        max_posts: int | None,
    ) -> tuple[list[Post], int]:
        logger.info("Collecting posts for r/%s", subreddit)
        collected: list[Post] = []
        collected_ids: set[str] = set()
        consecutive_existing = 0
        after: str | None = None
        stopped_no_more_pages = False

        def reached_limit() -> bool:
            if consecutive_existing >= CONSECUTIVE_EXISTING_THRESHOLD:
                return True
            if max_posts is None:
                return False
            return len(collected) >= max_posts

        with tqdm(desc="Collecting posts", unit=" posts", dynamic_ncols=True) as pbar:
            while True:
                batch, next_after = await self._get_listing(subreddit, after)
                if not batch:
                    break
                for post in batch:
                    if post.id not in collected_ids:
                        collected.append(post)
                        collected_ids.add(post.id)
                        pbar.update(1)
                    consecutive_existing = consecutive_existing + 1 if post.id in existing_ids else 0
                    if reached_limit():
                        break
                if reached_limit():
                    break
                if not next_after:
                    stopped_no_more_pages = True
                    break
                after = next_after

        if stopped_no_more_pages:
            req = f" (requested {max_posts})" if max_posts is not None and len(collected) < max_posts else ""
            logger.info(f"Reddit limits listing to ~1000 posts. Collected {len(collected)}{req}. Run daily to get more history.")

        return [post for post in collected if post.id not in existing_ids], len(collected)

    async def _process_new_posts(
        self,
        posts: list[Post],
        subreddit: str,
    ) -> int:
        logger.info("Scraping new posts for r/%s: %d", subreddit, len(posts))
        semaphore = asyncio.Semaphore(max(1, self.concurrency))

        async def process_one(post: Post) -> None:
            async with semaphore:
                await self._fill_post(post, subreddit)

        tasks = [process_one(post) for post in posts]
        with tqdm(
            total=len(tasks),
            desc="Scraping new posts",
            unit=" posts",
            dynamic_ncols=True,
        ) as pbar:
            for coro in asyncio.as_completed(tasks):
                await coro
                pbar.update(1)
        return len(posts)

    async def scrape_subreddit(
        self,
        subreddit: str,
        max_posts: int | None = None,
    ) -> None:
        self._ensure_data_dir(subreddit)
        self.data_manager = DataManager(self.data_dir)
        await self.data_manager.load()
        existing_count = len(self.data_manager.data)
        existing_ids = set(self.data_manager.data.keys())

        new_posts, collected_total = await self._collect_posts(
            subreddit,
            existing_ids,
            max_posts,
        )
        new_posts_to_process = (
            new_posts[:max_posts] if max_posts is not None else new_posts
        )
        already_existing = max(0, collected_total - len(new_posts))
        logger.info(
            "Collection summary r/%s: collected_total=%d, already_existing=%d, new_to_scrape=%d",
            subreddit,
            collected_total,
            already_existing,
            len(new_posts_to_process),
        )
        new_posts_count = await self._process_new_posts(
            new_posts_to_process, subreddit
        )

        new_total = len(self.data_manager.data)
        logger.info(
            f"Scraped subreddit r/{subreddit}: {new_posts_count} new posts, {new_total} total (started with {existing_count})",
        )

    async def scrape_single_post(self, url: str) -> Post | None:
        post_id = extract_post_id_from_url(url)
        subreddit = extract_subreddit_from_url(url)
        if not post_id or not subreddit:
            logger.error(f"Could not parse URL: {url}")
            return None
        self._ensure_data_dir(subreddit)
        self.data_manager = DataManager(self.data_dir)
        await self.data_manager.load()
        existing = self.data_manager.data.get(post_id)
        if existing is not None:
            ok = await self._refresh_post(existing, subreddit)
            if ok:
                await self.data_manager.upsert_and_save(existing)
                logger.info(f"Refreshed post {post_id}")
                return existing
        clean_id = post_id.replace("t3_", "")
        post_url = f"https://www.reddit.com/r/{subreddit}/comments/{clean_id}/"
        post = Post(id=post_id, url=post_url)
        await self._fill_post(post, subreddit)
        return self.data_manager.data.get(post_id)

    async def refresh_posts(self, subreddit: str, limit: int | None = None) -> None:
        self._ensure_data_dir(subreddit)
        self.data_manager = DataManager(self.data_dir)
        await self.data_manager.load()
        posts = list(self.data_manager.data.values())
        if not posts:
            logger.info("No posts in data.json; nothing to refresh")
            return
        posts.sort(key=lambda p: p.timestamp or p.crawled_at or "", reverse=True)
        if limit is not None and limit > 0:
            posts = posts[:limit]
        to_refresh: list[tuple[Post, str]] = []
        for post in posts:
            subreddit_name = extract_subreddit_from_url(post.url or "")
            if not subreddit_name:
                logger.warning(f"Skipping post {post.id or '?'}: no Reddit URL")
                continue
            to_refresh.append((post, subreddit_name))
        if not to_refresh:
            return
        semaphore = asyncio.Semaphore(max(1, self.concurrency))

        async def refresh_one(post: Post, subreddit_name: str) -> tuple[str, bool]:
            async with semaphore:
                ok = await self._refresh_post(post, subreddit_name)
            return (post.id or "", ok)

        refreshed_count = 0
        for coro in tqdm(
            asyncio.as_completed(
                [refresh_one(post, subreddit_name) for post, subreddit_name in to_refresh]
            ),
            total=len(to_refresh),
            desc="Refreshing",
            unit=" posts",
            dynamic_ncols=True,
        ):
            post_id, ok = await coro
            if ok:
                refreshed_count += 1
                if post_id in self.data_manager.data:
                    await self.data_manager.upsert_and_save(
                        self.data_manager.data[post_id]
                    )
        logger.info(f"Refreshed {refreshed_count}/{len(to_refresh)} posts")
