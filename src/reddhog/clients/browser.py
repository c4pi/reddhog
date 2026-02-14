import asyncio
from datetime import UTC, datetime
import logging
from pathlib import Path
import re
from typing import Any

import aiofiles
from patchright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)

from reddhog.clients.base import BrowserRateLimitError, _is_closed_error, _safe_close_page
from reddhog.config import (
    BROWSER_COOLDOWN_FALLBACK_SECONDS,
    BROWSER_FIRST_LOAD_TIMEOUT_MS,
    BROWSER_WARMUP_TIMEOUT_MS,
    BROWSER_WARMUP_URL,
)
from reddhog.models import Comment, Image, Post
from reddhog.utils import safe_int

logger = logging.getLogger("reddit_scraper")

MORE_COMMENTS_SELECTORS = [
    "button[slot='more-comments-button']",
    "a[slot='morecomments-button']",
    "button:has-text('more repl')",
    "button:has-text('more comment')",
]
NAV_TIMEOUT = 60000
POST_LOAD_TIMEOUT = 15000
LISTING_LOAD_TIMEOUT = 15000
MAX_LOAD_ATTEMPTS = 5
MAX_MORE_COMMENT_ROUNDS = 10


async def _attr(el: Any, names: list[str], default: str = "") -> str:
    for name in names:
        try:
            value = await el.get_attribute(name)
        except Exception:
            continue
        if value and str(value).strip():
            return str(value).strip()
    return default


async def _best_image_src(img: Any) -> str | None:
    for attr in ("src", "data-src", "data-lazy-src", "data-hires-src"):
        value = await img.get_attribute(attr)
        if value:
            return value
    srcset = await img.get_attribute("srcset") or ""
    if not srcset:
        return None
    tokens = srcset.split(",")[-1].strip().split()
    return tokens[0] if tokens else None


class RedditBrowserClient:
    def __init__(self, headless: bool = True):
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._started = False
        self._first_real_load_pending = True

    async def start(self) -> None:
        if self._started:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        self._started = True
        self._first_real_load_pending = True
        await self._warmup()

    async def _warmup(self) -> None:
        page: Any | None = None
        try:
            page = await self._context.new_page()  # type: ignore[union-attr]
            await page.goto(BROWSER_WARMUP_URL, timeout=BROWSER_WARMUP_TIMEOUT_MS, wait_until="load")
        except Exception as exc:
            logger.debug("Browser warmup failed: %s", exc)
        finally:
            await _safe_close_page(page)

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception as exc:
                if not _is_closed_error(exc):
                    raise
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception as exc:
                if not _is_closed_error(exc):
                    raise
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as exc:
                if not _is_closed_error(exc):
                    raise
            self._playwright = None
        self._started = False
        self._first_real_load_pending = True

    async def _save_fail_artifacts(
        self,
        page: Any,
        key: str,
        debug_dir: Path | None,
        enabled: bool,
    ) -> None:
        if not enabled or debug_dir is None:
            return
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_key = re.sub(r"[^\w\-]", "_", key)[:80]
            await page.screenshot(path=str(debug_dir / f"{safe_key}_fail.png"))
            content = await page.content()
            async with aiofiles.open(debug_dir / f"{safe_key}_fail.html", "w", encoding="utf-8") as handle:
                await handle.write(content)
        except Exception:
            pass

    async def _open_page_with_retries(
        self,
        url: str,
        wait_timeout_ms: int,
        artifact_key: str,
        debug_dir: Path | None,
        save_fail_artifacts: bool,
    ) -> Any:
        last_error: Exception | None = None
        use_short_first_attempt = self._first_real_load_pending
        self._first_real_load_pending = False
        for attempt in range(MAX_LOAD_ATTEMPTS):
            page = await self._context.new_page()  # type: ignore[union-attr]
            try:
                wait_timeout = wait_timeout_ms
                if use_short_first_attempt and attempt == 0:
                    wait_timeout = min(wait_timeout_ms, BROWSER_FIRST_LOAD_TIMEOUT_MS)
                await page.goto(url, timeout=NAV_TIMEOUT, wait_until="load")
                await page.wait_for_selector("shreddit-post", state="attached", timeout=wait_timeout)
                return page
            except Exception as exc:
                last_error = exc
                await self._save_fail_artifacts(page, artifact_key, debug_dir, save_fail_artifacts)
                await _safe_close_page(page)
                if attempt < MAX_LOAD_ATTEMPTS - 1:
                    await asyncio.sleep(attempt + 1)
        message = f"Failed to load {url}"
        if last_error is not None:
            message = f"{message}: {last_error}"
        raise RuntimeError(message)

    async def get_subreddit_posts(
        self,
        subreddit: str,
        after: str | None = None,
        *,
        max_scrolls: int = 20,
        debug_dir: Path | None = None,
        save_fail_artifacts: bool = True,
    ) -> tuple[list[Post], str | None]:
        del after
        try:
            listing_url = f"https://www.reddit.com/r/{subreddit}/new/"
            page = await self._open_page_with_retries(
                listing_url,
                LISTING_LOAD_TIMEOUT,
                f"{subreddit}_listing",
                debug_dir,
                save_fail_artifacts,
            )
            posts = await self._extract_listing_posts(page, subreddit, max_scrolls)
            await _safe_close_page(page)
            return posts, None
        except BrowserRateLimitError:
            raise
        except Exception as exc:
            raise BrowserRateLimitError(
                BROWSER_COOLDOWN_FALLBACK_SECONDS,
                f"Browser listing unavailable: {exc}",
            ) from exc

    async def get_post_details(
        self,
        subreddit: str,
        post_id: str,
        img_dir: str,
        *,
        skip_images: bool = False,
        debug_dir: Path | None = None,
        save_fail_artifacts: bool = True,
    ) -> tuple[Post, list[Comment], list[Image]]:
        clean_id = post_id.replace("t3_", "")
        url = f"https://www.reddit.com/r/{subreddit}/comments/{clean_id}/"
        page: Any | None = None
        try:
            page = await self._open_page_with_retries(
                url,
                POST_LOAD_TIMEOUT,
                post_id,
                debug_dir,
                save_fail_artifacts,
            )
            post, comments, images = await self.extract_post_and_comments(
                page,
                post_id,
                subreddit,
                img_dir,
                skip_images=skip_images,
            )
            return post, comments, images
        except BrowserRateLimitError:
            raise
        except Exception as exc:
            raise BrowserRateLimitError(
                BROWSER_COOLDOWN_FALLBACK_SECONDS,
                f"Browser post unavailable: {exc}",
            ) from exc
        finally:
            await _safe_close_page(page)

    async def _extract_listing_posts(
        self,
        page: Any,
        subreddit: str,
        max_scrolls: int,
    ) -> list[Post]:
        seen_ids: set[str] = set()
        posts: list[Post] = []
        crawled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        for _ in range(max_scrolls):
            elements = await page.query_selector_all("shreddit-post")
            for element in elements:
                try:
                    raw_id = await element.get_attribute("thingid") or await element.get_attribute("id")
                    if not raw_id:
                        continue
                    post_id = raw_id if raw_id.startswith("t3_") else f"t3_{raw_id.replace('t3_', '')}"
                    if post_id in seen_ids:
                        continue
                    seen_ids.add(post_id)
                    clean_id = post_id.replace("t3_", "")
                    posts.append(
                        Post(
                            id=post_id,
                            title=await _attr(element, ["post-title", "title"]),
                            flair="",
                            description="",
                            url=f"https://www.reddit.com/r/{subreddit}/comments/{clean_id}/",
                            upvotes=safe_int(await _attr(element, ["score"], "0")),
                            comments_count=safe_int(await _attr(element, ["comment-count", "comment_count"], "0")),
                            author=await _attr(element, ["author"], "unknown") or "unknown",
                            timestamp="",
                            images=[],
                            comments=[],
                            crawled_at=crawled_at,
                        )
                    )
                except Exception:
                    continue
            await page.mouse.wheel(0, 800)
        return posts

    async def expand_all_comments(self, page: Any) -> None:
        for _ in range(MAX_MORE_COMMENT_ROUNDS):
            buttons: list[Any] = []
            for selector in MORE_COMMENTS_SELECTORS:
                buttons.extend(await page.query_selector_all(selector))
            if not buttons:
                return
            clicked_any = False
            for button in buttons:
                try:
                    tag_name = await button.evaluate("el => el.tagName")
                    if tag_name == "A":
                        href = await button.get_attribute("href")
                        if href and "/comments/" in href:
                            continue
                    if await button.is_visible():
                        await button.scroll_into_view_if_needed()
                        await button.click()
                        clicked_any = True
                except Exception:
                    continue
            if not clicked_any:
                return

    async def _extract_post_meta(self, page: Any, post_el: Any) -> dict[str, str]:
        meta = {
            "title": await _attr(post_el, ["post-title", "title"]),
            "author": await _attr(post_el, ["author"], "unknown") or "unknown",
            "score": await _attr(post_el, ["score"], "0") or "0",
            "comment_count": await _attr(post_el, ["comment-count", "comment_count"], "0") or "0",
            "timestamp": "",
            "flair": "",
            "description": "",
        }
        time_el = await post_el.query_selector("time")
        if time_el:
            meta["timestamp"] = await time_el.get_attribute("datetime") or ""
        flair_el = await post_el.query_selector("[aria-label^='Flair']")
        if flair_el:
            meta["flair"] = (await flair_el.inner_text()).strip()
        desc_el = await post_el.query_selector("[slot='text-body']")
        if desc_el:
            meta["description"] = (await desc_el.inner_text()).strip()
        if not meta["title"]:
            title_el = await page.query_selector("h1")
            if title_el:
                meta["title"] = (await title_el.inner_text()).strip()
        if meta["author"] == "unknown":
            author_el = await page.query_selector("a[href^='/user/']")
            if author_el:
                candidate = (await author_el.inner_text()).strip()
                if candidate:
                    meta["author"] = candidate
        return meta

    async def _extract_comments(self, page: Any, post_id: str) -> list[Comment]:
        elements = await page.query_selector_all("shreddit-comment")
        if not elements:
            elements = await page.query_selector_all("[id^='comment-']")
        comment_map: dict[str, Any] = {}
        for element in elements:
            try:
                comment_id = await element.get_attribute("thingid") or await element.get_attribute("id")
            except Exception:
                continue
            if comment_id:
                comment_map[comment_id] = element

        comments: list[Comment] = []
        for comment_id, element in comment_map.items():
            try:
                body_el = await element.query_selector("[slot='comment']")
                if not body_el:
                    continue
                text = (await body_el.inner_text()).strip()
                author = await element.get_attribute("author") or "unknown"
                score = await element.get_attribute("score") or "0"
                hierarchy = await element.evaluate(
                    """
                    (el) => {
                        let depth = 0;
                        let parentId = null;
                        let cur = el.parentElement;
                        while (cur) {
                            if (cur.tagName === 'SHREDDIT-COMMENT') {
                                depth += 1;
                                if (!parentId) {
                                    parentId = cur.getAttribute('thingid') || cur.getAttribute('id') || null;
                                }
                            }
                            if (cur.tagName === 'SHREDDIT-POST') break;
                            cur = cur.parentElement;
                        }
                        return {depth, parentId};
                    }
                    """
                )
                depth = hierarchy.get("depth", 0) if isinstance(hierarchy, dict) else 0
                parent_id = hierarchy.get("parentId") if isinstance(hierarchy, dict) else None
                if parent_id == post_id or (parent_id and parent_id not in comment_map):
                    parent_id = None
                    depth = 0
                comments.append(
                    Comment(
                        id=comment_id,
                        author=author,
                        score=score,
                        text=text,
                        depth=depth,
                        parent_id=parent_id,
                    )
                )
            except Exception:
                continue
        return comments

    async def _download_image(self, page: Any, url: str, local_path: Path) -> bool:
        if local_path.exists():
            return True
        try:
            response = await page.request.get(url, timeout=30000)
            if not response.ok:
                return False
            body = await response.body()
            async with aiofiles.open(local_path, "wb") as handle:
                await handle.write(body)
            return True
        except Exception:
            return False

    async def _extract_images(
        self,
        page: Any,
        post_el: Any,
        post_id: str,
        img_dir: str,
    ) -> list[Image]:
        Path(img_dir).mkdir(parents=True, exist_ok=True)
        images: list[Image] = []
        seen: set[str] = set()
        index = 0

        async def add_image(img: Any) -> None:
            nonlocal index
            src = await _best_image_src(img)
            if not src or "redd.it" not in src:
                return
            clean = src.split("?")[0]
            if clean in seen:
                return
            seen.add(clean)
            local_path = Path(img_dir) / f"{post_id}_detail_{index}.jpg"
            if await self._download_image(page, src, local_path):
                images.append(Image(url=clean, local_path=str(local_path)))
                index += 1

        for selector in ("gallery-carousel", "shreddit-gallery-carousel", "shreddit-gallery", "media-gallery"):
            container = await post_el.query_selector(selector)
            if container:
                for img in await container.query_selector_all("img"):
                    await add_image(img)
        for img in await post_el.query_selector_all("img"):
            await add_image(img)
        desc_el = await post_el.query_selector("[slot='text-body']")
        if desc_el:
            for img in await desc_el.query_selector_all("img"):
                await add_image(img)
        return images

    async def extract_post_and_comments(
        self,
        page: Any,
        post_id: str,
        subreddit: str,
        img_dir: str,
        *,
        skip_images: bool = False,
    ) -> tuple[Post, list[Comment], list[Image]]:
        for _ in range(3):
            await page.mouse.wheel(0, 2500)
        await self.expand_all_comments(page)

        post_el = await page.query_selector(f"shreddit-post[thingid='{post_id}']")
        if not post_el:
            post_el = await page.query_selector(f"shreddit-post[id='{post_id}']")
        if not post_el:
            post_el = await page.query_selector("shreddit-post")
        if not post_el:
            raise RuntimeError(f"Post element not found: {post_id}")

        meta = await self._extract_post_meta(page, post_el)
        comments = await self._extract_comments(page, post_id)
        images = [] if skip_images else await self._extract_images(page, post_el, post_id, img_dir)

        clean_id = post_id.replace("t3_", "")
        post = Post(
            id=post_id,
            title=meta["title"],
            flair=meta["flair"],
            description=meta["description"],
            url=f"https://www.reddit.com/r/{subreddit}/comments/{clean_id}/",
            upvotes=safe_int(meta["score"]),
            comments_count=safe_int(meta["comment_count"]),
            author=meta["author"],
            timestamp=meta["timestamp"],
            images=images,
            comments=comments,
            crawled_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        return post, comments, images
