from patchright.async_api import Error as PatchrightError
from patchright.async_api import Page


class BrowserRateLimitError(Exception):
    def __init__(self, cooldown_seconds: float | None, message: str):
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds
        self.message = message


def _is_closed_error(e: BaseException) -> bool:
    if isinstance(e, PatchrightError):
        return True
    msg = str(e).lower()
    return "closed" in msg or "target" in msg or "connection" in msg


async def _safe_close_page(page: Page | None) -> None:
    if page is None:
        return
    try:
        await page.close()
    except Exception as e:
        if not _is_closed_error(e):
            raise
