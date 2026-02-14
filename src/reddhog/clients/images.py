import asyncio
import logging
from pathlib import Path
import time

import aiofiles
import httpx

from reddhog.clients.base import _is_closed_error
from reddhog.clients.cooldown import build_cooldown
from reddhog.config import IMG_CONCURRENCY

logger = logging.getLogger("reddit_scraper")


class ImageDownloader:
    def __init__(self, concurrency: int = IMG_CONCURRENCY):
        self.semaphore = asyncio.Semaphore(concurrency)
        self._client: httpx.AsyncClient | None = None
        self.images_disabled_until: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (compatible; RedditScraper/1.0)"},
                timeout=30,
                follow_redirects=True,
            )
        return self._client

    def _set_images_cooldown(self, status_code: int, resp: httpx.Response | None = None) -> None:
        now = time.monotonic()
        total, msg = build_cooldown(status_code, resp)
        self.images_disabled_until = now + total
        logger.info(f"Image downloads {msg}: cooldown {total:.0f}s")

    async def _download_via_browser(self, url: str, local_path: str, browser_page) -> bool:
        try:
            response = await browser_page.request.get(url, timeout=30000)
            if response.ok:
                body = await response.body()
                async with aiofiles.open(local_path, "wb") as f:
                    await f.write(body)
                return True
        except Exception:
            logger.debug(f"Browser image download failed: {url}")
        return False

    async def _download_one_attempt(self, url: str, local_path: str) -> tuple[bool, int | None]:
        client = await self._get_client()
        resp = await client.get(url)
        if resp.status_code == 200:
            async with aiofiles.open(local_path, "wb") as f:
                await f.write(resp.content)
            return True, None
        if resp.status_code == 429:
            self._set_images_cooldown(429, resp)
            return False, 429
        if resp.status_code == 403:
            self._set_images_cooldown(403)
            return False, 403
        if 500 <= resp.status_code < 600:
            return False, resp.status_code
        return False, None

    async def _try_browser_after_error(
        self, url: str, local_path: str, browser_page, status: int | None
    ) -> bool:
        if status in (429, 403) and browser_page:
            return await self._download_via_browser(url, local_path, browser_page)
        return False

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.aclose()
            except Exception as e:
                if not _is_closed_error(e):
                    raise
            self._client = None

    async def download(self, url: str, local_path: str, browser_page=None) -> bool:
        if Path(local_path).exists():
            return True
        try:
            async with self.semaphore:
                now = time.monotonic()
                if self.images_disabled_until > 0 and now >= self.images_disabled_until:
                    logger.info("Image downloads re-enabled after cooldown")
                    self.images_disabled_until = 0.0
                if now < self.images_disabled_until:
                    return await self._try_browser_after_error(url, local_path, browser_page, 429)

                success, status = await self._download_one_attempt(url, local_path)
                if success:
                    return True
                if status in (429, 403):
                    return await self._try_browser_after_error(url, local_path, browser_page, status)
                if status is not None and 500 <= status < 600:
                    logger.debug(f"Image download 5xx for {url}: skip (no retry)")
                    return False
                return False
        except Exception:
            if browser_page:
                return await self._download_via_browser(url, local_path, browser_page)
            return False

    async def download_batch(self, items: list[tuple[str, str]], browser_page=None) -> list[bool]:
        tasks = [self.download(url, path, browser_page) for url, path in items]
        return await asyncio.gather(*tasks, return_exceptions=False)
