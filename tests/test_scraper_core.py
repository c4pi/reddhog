import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile

import httpx
import pytest

from reddhog.models import Post
from reddhog.scraper import core as core_mod


def _post(post_id: str) -> Post:
    return Post(id=post_id, url=f"https://www.reddit.com/r/test/comments/{post_id.replace('t3_', '')}/")


def _raise_http_status(status_code: int, url: str):
    async def _raiser(*_args, **_kwargs):
        req = httpx.Request("GET", url)
        resp = httpx.Response(status_code, request=req)
        raise httpx.HTTPStatusError(str(status_code), request=req, response=resp)

    return _raiser


def test_collect_posts_returns_new_and_collected_total(monkeypatch):
    scraper = core_mod.RedditScraper(strategy="auto")
    listings = [
        ([_post("t3_a"), _post("t3_b")], "after_1"),
        ([_post("t3_c")], None),
    ]

    async def fake_get_listing(_subreddit: str, _after: str | None = None):
        return listings.pop(0)

    monkeypatch.setattr(scraper, "_get_listing", fake_get_listing)
    new_posts, collected_total = asyncio.run(scraper._collect_posts("test", {"t3_b"}, None))

    assert collected_total == 3
    assert [post.id for post in new_posts] == ["t3_a", "t3_c"]


def test_auto_strategy_prefers_json_for_post_scraping(monkeypatch):
    scraper = core_mod.RedditScraper(strategy="auto")
    scraper.data_dir = Path("data/test")

    async def fake_json_details(_subreddit: str, _post_id: str):
        return (
            {
                "title": "json title",
                "link_flair_text": "",
                "selftext": "",
                "score": 10,
                "num_comments": 2,
                "author": "json_user",
                "created_utc": 0,
            },
            [],
        )

    async def fake_download(_post_id: str, _image_urls: list[str], _browser_page=None):
        return []

    @asynccontextmanager
    async def fail_if_browser_used():
        raise AssertionError("browser should not be used when JSON is available")
        yield

    monkeypatch.setattr(scraper._json, "get_post_details", fake_json_details)
    monkeypatch.setattr(scraper, "_download_post_images", fake_download)
    monkeypatch.setattr(scraper, "_acquire_browser_lease", fail_if_browser_used)

    post = asyncio.run(scraper._get_post("test", "t3_abc"))
    assert post.title == "json title"
    assert post.author == "json_user"


def test_browser_strategy_never_calls_json_for_post_scraping(monkeypatch):
    scraper = core_mod.RedditScraper(strategy="browser")
    scraper.data_dir = Path("data/test")

    async def fail_json(*_args, **_kwargs):
        raise AssertionError("JSON should not be called in browser strategy post scraping")

    class FakeBrowser:
        async def get_post_details(self, subreddit: str, post_id: str, img_dir: str, **_kwargs):
            _ = subreddit, img_dir
            return _post(post_id), [], []

    @asynccontextmanager
    async def fake_lease():
        yield FakeBrowser()

    monkeypatch.setattr(scraper._json, "get_post_details", fail_json)
    monkeypatch.setattr(scraper, "_acquire_browser_lease", fake_lease)
    monkeypatch.setattr(scraper, "_browser_available", lambda: True)

    post = asyncio.run(scraper._get_post("test", "t3_browser"))
    assert post.id == "t3_browser"


def test_browser_strategy_waits_only_for_browser_cooldown(monkeypatch):
    scraper = core_mod.RedditScraper(strategy="browser")
    scraper.browser_cooldown_until = 321.0
    scraper._json.json_disabled_until = 999.0
    waited_until: list[float] = []

    async def fake_wait_until(value: float):
        waited_until.append(value)

    monkeypatch.setattr(scraper, "_wait_until", fake_wait_until)
    asyncio.run(scraper._wait_for_post_backend_ready())

    assert waited_until == [321.0]


def test_json_strategy_listing_raises_non_retriable_http_status(monkeypatch):
    scraper = core_mod.RedditScraper(strategy="json")
    monkeypatch.setattr(
        scraper._json,
        "get_subreddit_posts",
        _raise_http_status(404, "https://www.reddit.com/r/missing/new.json"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(scraper._get_listing("missing"))


def test_json_strategy_post_raises_non_retriable_http_status(monkeypatch):
    scraper = core_mod.RedditScraper(strategy="json")
    scraper.data_dir = Path("data/test")
    monkeypatch.setattr(
        scraper._json,
        "get_post_details",
        _raise_http_status(404, "https://www.reddit.com/r/test/comments/missing.json"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(scraper._get_post("test", "t3_missing"))


def test_browser_pool_uses_rotation_quota(monkeypatch):
    class FakeBrowserClient:
        created = 0
        closed = 0

        def __init__(self, **_kwargs):
            type(self).created += 1

        async def start(self):
            return None

        async def close(self):
            type(self).closed += 1

    monkeypatch.setattr(core_mod, "RedditBrowserClient", FakeBrowserClient)
    monkeypatch.setattr(core_mod, "BROWSER_NUM_PROFILES", 1)
    monkeypatch.setattr(
        core_mod, "BROWSER_PROFILE_BASE", Path(tempfile.gettempdir()) / "reddhog_browser_profile"
    )

    pool = core_mod._BrowserPool(
        concurrency=1,
        tabs_per_browser=1,
        rotation_quota=3,
        headless=True,
    )

    async def use_pool():
        for _ in range(3):
            async with pool.acquire_lease():
                pass

    asyncio.run(use_pool())
    slot = pool._slots[0]

    assert FakeBrowserClient.created == 1
    assert FakeBrowserClient.closed == 1
    assert slot.browser is None
    assert slot.quota_remaining == 3
