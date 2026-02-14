import asyncio
import contextlib
from datetime import UTC, datetime
import logging
import time

import httpx

from reddhog.clients.cooldown import build_cooldown
from reddhog.config import DEFAULT_CONCURRENCY, REQUEST_DELAY, TIMEOUT
from reddhog.models import Comment, Post
from reddhog.utils import utc_to_iso

logger = logging.getLogger("reddit_scraper")


class RedditJSONClient:
    BASE_URL = "https://www.reddit.com"

    def __init__(self, concurrency: int = DEFAULT_CONCURRENCY):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.json_disabled_until: float = 0.0
        self.client: httpx.AsyncClient | None = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (compatible; RedditScraper/1.0)"},
            timeout=TIMEOUT,
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.aclose()
            self.client = None

    def cooldown_remaining(self) -> float:
        return max(0.0, self.json_disabled_until - time.monotonic())

    def is_available(self) -> bool:
        return time.monotonic() >= self.json_disabled_until

    def _set_json_cooldown(self, status_code: int, resp: httpx.Response) -> None:
        now = time.monotonic()
        total, msg = build_cooldown(status_code, resp)
        previous_until = self.json_disabled_until
        new_until = max(previous_until, now + total)
        self.json_disabled_until = new_until
        if previous_until <= now:
            logger.info("JSON %s: cooldown for %.0fs", msg, new_until - now)

    def _handle_json_response(self, resp: httpx.Response, url: str) -> dict | None:
        if resp.status_code in (403, 429):
            self._set_json_cooldown(resp.status_code, resp)
            raise httpx.HTTPStatusError(
                f"JSON {resp.status_code}", request=resp.request, response=resp
            )
        if resp.is_success:
            return resp.json()
        if 500 <= resp.status_code < 600:
            raise httpx.HTTPStatusError(
                f"Reddit unavailable ({resp.status_code})",
                request=resp.request,
                response=resp,
            )
        if "/api/morechildren.json" in url:
            logger.debug(f"morechildren {resp.status_code} for {url}")
        else:
            logger.info(f"JSON {resp.status_code} for {url}")
        raise httpx.HTTPStatusError(
            f"JSON error {resp.status_code} for {url}",
            request=resp.request,
            response=resp,
        )

    async def fetch_json(self, endpoint: str) -> dict:
        if not self.is_available():
            wait_sec = self.cooldown_remaining()
            if wait_sec > 0:
                logger.info(
                    "JSON in cooldown (429/403): sleeping %.0fs", wait_sec
                )
                await asyncio.sleep(wait_sec)
                self.json_disabled_until = 0.0
                logger.info("JSON re-enabled after cooldown")

        async with self.semaphore:
            url = self.BASE_URL + endpoint
            await asyncio.sleep(REQUEST_DELAY)
            resp = await self.client.get(url)  # type: ignore[union-attr]
            data = self._handle_json_response(resp, url)
            return data  # type: ignore[return-value]

    def parse_listing_post(self, data: dict, _subreddit: str) -> Post:
        name = data.get("name", "")
        permalink = data.get("permalink", "")
        if not permalink.startswith("/"):
            permalink = "/" + permalink
        url = "https://www.reddit.com" + permalink.rstrip("/") + "/"
        return Post(
            id=name,
            title=data.get("title", ""),
            flair=data.get("link_flair_text") or "",
            description=data.get("selftext") or "",
            url=url,
            upvotes=int(data.get("score", 0)),
            comments_count=int(data.get("num_comments", 0)),
            author=data.get("author") or "unknown",
            timestamp=utc_to_iso(data.get("created_utc", 0)),
            images=[],
            comments=[],
            crawled_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

    async def get_subreddit_posts(self, subreddit: str, after: str | None = None) -> tuple[list[Post], str | None]:
        endpoint = f"/r/{subreddit}/new.json?limit=100"
        if after:
            endpoint += f"&after={after}"
        data = await self.fetch_json(endpoint)
        children = data.get("data", {}).get("children", [])
        posts = []
        for c in children:
            if c.get("kind") != "t3":
                continue
            post = self.parse_listing_post(c["data"], subreddit)
            posts.append(post)
        next_after = data.get("data", {}).get("after")
        return posts, next_after

    _MORECHILDREN_BATCH = 100

    async def fetch_more_comments(self, link_id: str, children_ids: list[str]) -> list:
        fullnames = [
            cid if cid.startswith("t1_") else "t1_" + cid
            for cid in children_ids
            if cid
        ]
        if not fullnames:
            return []
        things: list = []
        for i in range(0, len(fullnames), self._MORECHILDREN_BATCH):
            batch = fullnames[i : i + self._MORECHILDREN_BATCH]
            children_param = ",".join(batch)
            endpoint = f"/api/morechildren.json?link_id={link_id}&children={children_param}&sort=confidence"
            try:
                resp = await self.fetch_json(endpoint)
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    raise
                logger.debug(f"morechildren {e.response.status_code}: skip {len(batch)} ID(s)")
                continue
            except (RuntimeError, Exception):
                continue
            if isinstance(resp, dict):
                inner = resp.get("json") if "json" in resp else resp
                if isinstance(inner, dict):
                    data = inner.get("data", inner)
                    batch_things = data.get("things", []) if isinstance(data, dict) else resp.get("things", [])
                else:
                    batch_things = resp.get("things", [])
            else:
                batch_things = []
            if isinstance(batch_things, list):
                things.extend(batch_things)
        return things

    @staticmethod
    def _flatten_comments(children: list, post_id: str, depth: int = 0) -> tuple[list[Comment], list[dict]]:
        result: list[Comment] = []
        more_list: list[dict] = []
        for child in children:
            if child.get("kind") == "t1":
                d = child["data"]
                cid = d.get("name", "")
                parent_id = d.get("parent_id")
                if parent_id == post_id:
                    parent_id = None
                comment = Comment(
                    id=cid,
                    author=d.get("author") or "unknown",
                    score=str(d.get("score", 0)),
                    text=d.get("body", ""),
                    depth=depth,
                    parent_id=parent_id,
                )
                result.append(comment)
                replies = d.get("replies")
                if isinstance(replies, dict) and "data" in replies:
                    kids = replies["data"].get("children", [])
                    sub_comments, sub_more = RedditJSONClient._flatten_comments(kids, post_id, depth + 1)
                    result.extend(sub_comments)
                    more_list.extend(sub_more)
            elif child.get("kind") == "more":
                data = child.get("data", {})
                raw_children = data.get("children", [])
                children_fullnames = [
                    c if isinstance(c, str) and c.startswith("t1_") else "t1_" + str(c)
                    for c in raw_children
                    if c
                ]
                if children_fullnames:
                    more_list.append({
                        "parent_id": data.get("parent_id"),
                        "children": children_fullnames,
                    })
        return result, more_list

    @staticmethod
    def _fix_orphan_parents(comments: list[Comment], post_id: str) -> None:
        valid_ids = {c.id for c in comments}
        for c in comments:
            pid = c.parent_id
            if pid is not None and pid != post_id and pid not in valid_ids:
                c.parent_id = None
                c.depth = 0

    @staticmethod
    def _url_from_media_item(item: dict) -> str | None:
        s = item.get("s", {}) or item.get("p", [{}])
        if isinstance(s, list):
            s = s[-1] if s else {}
        u = s.get("u") if isinstance(s, dict) else None
        return (u.replace("&amp;", "&")) if u else None

    @staticmethod
    def _image_urls_from_preview(post_data: dict) -> list[str]:
        urls: list[str] = []
        preview = post_data.get("preview", {})
        for img in preview.get("images", []):
            src = img.get("source", {}).get("url")
            if not src and img.get("images"):
                src = img["images"][-1].get("url")
            if src:
                urls.append(src.replace("&amp;", "&"))
        return urls

    @classmethod
    def _image_urls_from_gallery_and_metadata(cls, post_data: dict) -> list[str]:
        urls: list[str] = []
        meta = post_data.get("media_metadata") or {}
        gallery_data = post_data.get("gallery_data") or {}
        gallery_items = gallery_data.get("items") or []
        if gallery_items:
            for item in gallery_items:
                if not isinstance(item, dict):
                    continue
                media_id = item.get("media_id")
                if not media_id:
                    continue
                m = meta.get(media_id)
                if isinstance(m, dict):
                    u = cls._url_from_media_item(m)
                    if u:
                        urls.append(u)
        else:
            for item in meta.values():
                if isinstance(item, dict):
                    u = cls._url_from_media_item(item)
                    if u:
                        urls.append(u)
        return urls

    @classmethod
    def extract_image_urls(cls, post_data: dict) -> list[str]:
        urls = cls._image_urls_from_preview(post_data) + cls._image_urls_from_gallery_and_metadata(post_data)
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            clean = u.split("?")[0]
            if clean not in seen:
                seen.add(clean)
                out.append(u)
        return out

    _MORE_DEPTH_LIMIT = 4

    async def get_post_details(self, subreddit: str, post_id: str) -> tuple[dict, list[Comment]]:
        clean_id = post_id.replace("t3_", "")
        endpoint = f"/r/{subreddit}/comments/{clean_id}.json?limit=500"
        data = await self.fetch_json(endpoint)
        if not data or not isinstance(data, list) or len(data) < 1:
            raise RuntimeError("Invalid comments response")
        first = data[0]
        post_children = first.get("data", {}).get("children", [])
        if not post_children or post_children[0].get("kind") != "t3":
            raise RuntimeError("No post in response")
        post_data = post_children[0]["data"]
        comment_children = []
        if len(data) >= 2 and isinstance(data[1].get("data"), dict):
            comment_children = data[1]["data"].get("children", [])
        comments, pending_more = self._flatten_comments(comment_children, post_id)
        all_comments = list(comments)
        for _ in range(self._MORE_DEPTH_LIMIT):
            if not pending_more:
                break
            new_raw: list = []
            for item in pending_more:
                child_ids = item.get("children") or []
                things = await self.fetch_more_comments(post_id, child_ids)
                new_raw.extend(things)
            new_comments, pending_more = self._flatten_comments(new_raw, post_id)
            all_comments.extend(new_comments)
        self._fix_orphan_parents(all_comments, post_id)
        return post_data, all_comments
