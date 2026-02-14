import asyncio

from reddhog.clients import browser as browser_mod
from reddhog.config import (
    BROWSER_FIRST_LOAD_TIMEOUT_MS,
    BROWSER_WARMUP_TIMEOUT_MS,
    BROWSER_WARMUP_URL,
)


class DummyPage:
    def __init__(self, *, fail_wait: bool = False):
        self.fail_wait = fail_wait
        self.goto_calls: list[tuple[str, int, str]] = []
        self.wait_calls: list[int] = []
        self.closed = False

    async def goto(self, url: str, *, wait_until: str, **kwargs) -> None:
        self.goto_calls.append((url, kwargs["timeout"], wait_until))

    async def wait_for_selector(self, _selector: str, *, state: str, **kwargs) -> None:
        assert state == "attached"
        timeout = kwargs["timeout"]
        self.wait_calls.append(timeout)
        if self.fail_wait:
            raise TimeoutError("selector timeout")

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, path: str) -> None:
        _ = path

    async def content(self) -> str:
        return ""


class DummyContext:
    def __init__(self, pages: list[DummyPage]):
        self._pages = pages

    async def new_page(self) -> DummyPage:
        return self._pages.pop(0)

    async def close(self) -> None:
        return None


class DummyBrowser:
    def __init__(self, context: DummyContext):
        self._context = context

    async def new_context(self, **_kwargs) -> DummyContext:
        return self._context

    async def close(self) -> None:
        return None


class DummyChromium:
    def __init__(self, browser: DummyBrowser):
        self._browser = browser

    async def launch(self, **_kwargs) -> DummyBrowser:
        return self._browser


class DummyPlaywrightRuntime:
    def __init__(self, chromium: DummyChromium):
        self.chromium = chromium

    async def stop(self) -> None:
        return None


class DummyPlaywrightStarter:
    def __init__(self, runtime: DummyPlaywrightRuntime):
        self._runtime = runtime

    async def start(self) -> DummyPlaywrightRuntime:
        return self._runtime


def test_start_runs_browser_warmup(monkeypatch):
    warmup_page = DummyPage()
    context = DummyContext([warmup_page])
    runtime = DummyPlaywrightRuntime(DummyChromium(DummyBrowser(context)))
    monkeypatch.setattr(browser_mod, "async_playwright", lambda: DummyPlaywrightStarter(runtime))

    client = browser_mod.RedditBrowserClient(headless=True)
    asyncio.run(client.start())
    asyncio.run(client.close())

    assert warmup_page.goto_calls == [
        (BROWSER_WARMUP_URL, BROWSER_WARMUP_TIMEOUT_MS, "load"),
    ]
    assert warmup_page.closed


def test_open_page_uses_short_first_timeout_then_normal(monkeypatch):
    page1 = DummyPage(fail_wait=True)
    page2 = DummyPage(fail_wait=False)
    context = DummyContext([page1, page2])
    client = browser_mod.RedditBrowserClient(headless=True)
    client._context = context
    client._first_real_load_pending = True

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", no_sleep)

    result = asyncio.run(
        client._open_page_with_retries(
            "https://www.reddit.com/r/test/comments/abc/",
            wait_timeout_ms=10_000,
            artifact_key="t3_abc",
            debug_dir=None,
            save_fail_artifacts=False,
        )
    )

    assert result is page2
    assert page1.wait_calls == [BROWSER_FIRST_LOAD_TIMEOUT_MS]
    assert page2.wait_calls == [10_000]
