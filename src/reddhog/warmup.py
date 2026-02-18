"""Warm up the Patchright persistent browser profile for ReddHog."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
from typing import Literal

from anyio import Path as AnyioPath
from patchright.async_api import Page, async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("reddhog.warmup")

DEFAULT_PROFILE_DIR = Path(tempfile.gettempdir()) / "reddhog_browser_profile"
WARMUP_URL = "https://www.reddit.com/r/popular/"
DETECTION_TEST_URLS = [
    "https://bot.sannysoft.com/",
    "https://abrahamjuliot.github.io/creepjs/",
    "https://www.browserscan.net/",
]


async def warmup(profile_dir: Path, run_detection_tests: bool = False, is_retry: bool = False) -> bool:

    await AnyioPath(profile_dir).mkdir(parents=True, exist_ok=True)
    if is_retry:
        logger.info("")
        logger.info("  Same profile again. Complete the steps in the browser, then press ENTER here to save.")
        logger.info("  Do not close the browser with X.")
        logger.info("")
    logger.info("Profile directory: %s", profile_dir)
    if (profile_dir / "storage_state.json").exists():
        logger.info("Refreshing existing profile (will overwrite with new session).")

    logger.info("")
    logger.info("══════════════════════════════════════════════")
    logger.info("  Instructions:")
    logger.info("  1. Solve CAPTCHAs if needed")
    logger.info("  2. Accept cookies")
    logger.info("  3. Scroll a bit")
    logger.info("")
    logger.info("  When done: press ENTER in the terminal to save the profile.")
    logger.info("══════════════════════════════════════════════")
    logger.info("")

    logger.info("Opening browser in 5 seconds…")
    await asyncio.sleep(5)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=[
                "--window-size=1920,1080",
                "--window-position=0,0",
            ],
        )

        page = await context.new_page()
        logger.info("Opening %s …", WARMUP_URL)
        try:
            await page.goto(WARMUP_URL, timeout=30_000, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("Initial page load issue (may be fine): %s", exc)

        if run_detection_tests:
            logger.info("")
            logger.info("Opening bot-detection test pages …")
            for url in DETECTION_TEST_URLS:
                logger.info("  Opening %s", url)
                try:
                    test_page = await context.new_page()
                    await test_page.goto(url, timeout=30_000, wait_until="load")
                    await asyncio.sleep(5)
                except Exception as exc:
                    logger.warning("  ⚠ %s: %s", url, exc)

        logger.info("")
        logger.info("Browser is ready. Follow the instructions above.")
        logger.info("")

        loop = asyncio.get_running_loop()
        done_fut: asyncio.Future[Literal["enter", "closed"]] = loop.create_future()

        async def on_page_close(p: Page) -> None:  # noqa: ARG001
            if not done_fut.done():
                loop.call_soon_threadsafe(done_fut.set_result, "closed")

        page.on("close", on_page_close)

        state_path = profile_dir / "storage_state.json"

        async def save_and_close() -> None:
            await context.storage_state(path=str(state_path))
            logger.info("Saved storage state to %s", state_path)
            pages = context.pages
            if pages:
                ua = await pages[0].evaluate("() => navigator.userAgent")
                if ua and isinstance(ua, str):
                    (profile_dir / "last_user_agent.txt").write_text(ua.strip(), encoding="utf-8")
                    logger.info("Saved User-Agent for JSON client to %s", profile_dir / "last_user_agent.txt")
            try:
                cookies = await context.cookies()
                cf_cookies = [c for c in cookies if "cf" in c.get("name", "").lower()]
                reddit_cookies = [c for c in cookies if "reddit" in c.get("domain", "")]
                logger.info("")
                logger.info("═══ Cookie summary ═══")
                logger.info("Total cookies:   %d", len(cookies))
                logger.info("Cloudflare:      %d  %s", len(cf_cookies), [c["name"] for c in cf_cookies])
                logger.info("Reddit domain:   %d", len(reddit_cookies))
                if cf_cookies:
                    logger.info("✅  Cloudflare cookies found — profile is warmed up.")
                else:
                    logger.info(
                        "No Cloudflare cookies in this snapshot. "
                        "If Reddit loaded normally, the profile may still be fine."
                    )
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await context.close()

        def wait_enter_thread() -> None:
            if sys.stdin.isatty():
                input("Press ENTER in the terminal to save the profile: ")
            if not done_fut.done():
                loop.call_soon_threadsafe(done_fut.set_result, "enter")

        if sys.stdin.isatty():
            t = threading.Thread(target=wait_enter_thread, daemon=True)
            t.start()
        else:
            async def wait_enter() -> None:
                await asyncio.sleep(30)
                if not done_fut.done():
                    done_fut.set_result("enter")
            asyncio.create_task(wait_enter())  # noqa: RUF006

        await done_fut
        how = done_fut.result()

        if how == "enter":
            await save_and_close()
            logger.info("Profile saved to: %s", profile_dir)
            logger.info("You can now run the scraper (even headless).")
            return True
        with contextlib.suppress(Exception):
            await context.close()
        return False


def update_headless_profiles_json(profile_dirs: list[Path]) -> None:
    """Read last_user_agent.txt from each profile dir and write defaults/headless_profiles.json.
    Deduplicates by user_agent (same OS + Chrome yields the same UA per profile).
    """
    seen_ua: set[str] = set()
    entries: list[dict[str, str]] = []
    for d in profile_dirs:
        ua_path = d / "last_user_agent.txt"
        if ua_path.exists():
            try:
                ua = ua_path.read_text(encoding="utf-8").strip()
                if ua and ua not in seen_ua:
                    seen_ua.add(ua)
                    entries.append({"user_agent": ua})
            except Exception:
                pass
    if not entries:
        return
    path = Path(__file__).resolve().parent / "defaults" / "headless_profiles.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        logger.info("Updated %s with %d unique UA(s) from %d profile(s).", path.name, len(entries), len(profile_dirs))
    except OSError as e:
        logger.warning("Could not write %s: %s", path.name, e)


if __name__ == "__main__":
    saved = asyncio.run(warmup(DEFAULT_PROFILE_DIR, run_detection_tests=False))
    if not saved:
        for name in ("storage_state.json", "last_user_agent.txt"):
            (DEFAULT_PROFILE_DIR / name).unlink(missing_ok=True)
        logger.info(
            "You closed the browser with X. We couldn't save. Re-run and press ENTER to save."
        )
