import asyncio
import logging
from pathlib import Path
import sys
from typing import TypedDict, cast

import httpx
import rich_click as click
from rich_click import RichGroup
from rich_click import rich_click as click_config
from tqdm import tqdm

from reddhog import __version__
from reddhog.config import (
    BROWSER_NUM_PROFILES,
    BROWSER_PROFILE_BASE,
    DEFAULT_CONCURRENCY,
)
from reddhog.scraper import RedditScraper
from reddhog.settings import Settings, get_settings
from reddhog.warmup import DEFAULT_PROFILE_DIR, update_headless_profiles_json
from reddhog.warmup import warmup as warmup_fn


def _has_warmup_profiles() -> bool:
    if BROWSER_NUM_PROFILES <= 1:
        return (BROWSER_PROFILE_BASE / "storage_state.json").exists()
    for i in range(BROWSER_NUM_PROFILES):
        if (Path(f"{BROWSER_PROFILE_BASE}_{i}") / "storage_state.json").exists():
            return True
    return False


class CliContext(TypedDict):
    settings: Settings


def _run_async(async_run_fn, *args, **kwargs) -> None:
    try:
        asyncio.run(async_run_fn(*args, **kwargs))
    except FileNotFoundError as e:
        if "storage_state" in str(e) or "storage state" in str(e).lower():
            click.secho(
                "Browser profile not found. Run 'reddhog warmup' first.",
                err=True,
                fg="red",
            )
        else:
            click.secho(str(e), err=True, fg="red")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            click.secho("Reddit is not available.", err=True, fg="red")
            sys.exit(1)
        raise


class _TqdmLoggingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tqdm.write(msg, file=sys.stderr)
        except Exception:
            self.handleError(record)


def _configure_logging(*, settings: Settings) -> None:
    handler = _TqdmLoggingHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    log_level = getattr(logging, settings.log_level, logging.INFO)
    root.setLevel(log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)

click_config.HEADER_TEXT = "[bold cyan]reddhog[/] — [dim]Resilient Reddit scraper[/]"
click_config.OPTIONS_PANEL_TITLE = "Options"
click_config.COMMANDS_PANEL_TITLE = "Commands"
click_config.TEXT_MARKUP = "rich"


class OrderPreservingGroup(RichGroup):
    def list_commands(self, _ctx: click.Context) -> list[str]:
        return list(self.commands)


@click.group(cls=OrderPreservingGroup, help="Resilient Reddit scraper. Results under ./data/<name>/ as data.json, optionally Excel/CSV.")
@click.pass_context
def cli_group(ctx: click.Context) -> None:
    settings = get_settings()
    _configure_logging(settings=settings)
    ctx.obj = {"settings": settings}


@cli_group.command("version", help="Print the application version.")
def cmd_version() -> None:
    click.echo(__version__)


@cli_group.command("settings", help="Show effective settings.")
@click.pass_context
def cmd_settings(ctx: click.Context) -> None:
    context = cast("CliContext", ctx.obj)
    settings = context["settings"]
    click.echo(f"log_level={settings.log_level}")


def warmup_options(f):
    f = click.option(
        "--profile",
        type=click.Path(path_type=Path),
        default=DEFAULT_PROFILE_DIR,
        show_default=True,
        help="Profile directory when num-profiles=1.",
    )(f)
    f = click.option(
        "--profile-base",
        type=click.Path(path_type=Path),
        default=DEFAULT_PROFILE_DIR,
        show_default=True,
        help="Base path for profiles when num-profiles>1; dirs will be {base}_0, {base}_1, ...",
    )(f)
    f = click.option(
        "--num-profiles",
        "-n",
        type=int,
        default=3,
        metavar="N",
        show_default=True,
        help="Number of profiles to warm.",
    )(f)
    f = click.option(
        "--test-detection",
        is_flag=True,
        help="Also open bot-detection test pages.",
    )(f)
    return f


@cli_group.command("warmup", help="Warm up browser profiles (run once after install). Opens Chrome, loads Reddit; solve CAPTCHAs / accept cookies, then press ENTER in the terminal to save. Do not close the browser with X.")
@warmup_options
def cmd_warmup(
    profile: Path,
    profile_base: Path,
    num_profiles: int,
    test_detection: bool,
) -> None:
    def remove_saved_profile(profile_dir: Path) -> None:
        for name in ("storage_state.json", "last_user_agent.txt"):
            (profile_dir / name).unlink(missing_ok=True)

    async def run() -> None:
        log = logging.getLogger("reddhog.warmup")
        for i in range(num_profiles):
            profile_dir = Path(f"{profile_base}_{i}") if num_profiles > 1 else profile
            is_retry = False
            while True:
                if num_profiles > 1:
                    log.info("Warming profile %d/%d: %s", i + 1, num_profiles, profile_dir)
                saved = await warmup_fn(
                    profile_dir,
                    run_detection_tests=test_detection,
                    is_retry=is_retry,
                )
                if saved:
                    break
                remove_saved_profile(profile_dir)
                is_retry = True
                sys.stdout.write("\n")
                sys.stdout.flush()
                log.info(
                    "You closed the browser with X. The profile was not saved. Re-opening the same profile."
                )
        if num_profiles > 1:
            log.info("All %d profiles warmed.", num_profiles)
        profile_dirs = (
            [Path(f"{profile_base}_{i}") for i in range(num_profiles)]
            if num_profiles > 1
            else [profile]
        )
        update_headless_profiles_json(profile_dirs)

    asyncio.run(run())


def shared_options(f):
    f = click.option(
        "--strategy",
        "-s",
        type=click.Choice(["auto", "json", "browser"]),
        default="auto",
        show_default=True,
        help="auto=JSON+fallback to browser, json=JSON only, browser=browser only.",
    )(f)
    f = click.option(
        "--headless/--no-headless",
        "headless",
        default=True,
        show_default=True,
        help="Run browser headless (no window). Use --no-headless to see the browser window.",
    )(f)
    f = click.option(
        "--concurrency",
        "-c",
        type=click.IntRange(min=1),
        default=DEFAULT_CONCURRENCY,
        show_default=True,
        help="Number of requests to run in parallel. Lower if you hit rate limits.",
    )(f)
    f = click.option(
        "--export",
        type=click.Choice(["excel", "csv"]),
        default=None,
        help="Also write an Excel or CSV file next to data.json. If you omit this, only data.json is created; if data.xlsx or data.csv already exist in that folder, they are updated instead of creating new files.",
    )(f)
    return f


async def _run_command(scraper: RedditScraper, command: str, kwargs: dict) -> None:
    handlers = {
        "subreddit": lambda: scraper.scrape_subreddit(kwargs["name"], max_posts=kwargs.get("limit")),
        "url": lambda: scraper.scrape_single_post(kwargs["url"]),
        "refresh": lambda: scraper.refresh_posts(kwargs["name"], limit=kwargs.get("limit")),
    }
    handler = handlers.get(command)
    if handler is None:
        raise click.UsageError(f"Unknown command: {command}")
    await handler()


def _run_exports(scraper: RedditScraper, export: str | None) -> None:
    if export == "excel":
        scraper.data_manager.export_excel()  # type: ignore[union-attr]
        return
    if export == "csv":
        scraper.data_manager.export_csv()  # type: ignore[union-attr]
        return
    if not scraper.data_dir:
        return
    if (scraper.data_dir / "data.xlsx").exists():
        scraper.data_manager.export_excel()  # type: ignore[union-attr]
    if (scraper.data_dir / "data.csv").exists():
        scraper.data_manager.export_csv()  # type: ignore[union-attr]


async def async_run(
    strategy: str,
    concurrency: int,
    headless: bool,
    export: str | None,
    command: str,
    **kwargs,
) -> None:
    if strategy in ("auto", "browser") and not _has_warmup_profiles():
        click.secho(
            "Browser profiles not found. Run 'reddhog warmup' first.\n"
            "Required for --strategy browser and for browser fallback when JSON is rate-limited.",
            err=True,
            fg="red",
        )
        sys.exit(1)
    scraper = RedditScraper(
        strategy=strategy,
        concurrency=concurrency,
        headless=headless,
    )
    try:
        await _run_command(scraper, command, kwargs)
        _run_exports(scraper, export)

        if scraper.data_dir:
            click.secho(f"Wrote data to {scraper.data_dir.resolve()}/", fg="green")
    finally:
        await scraper.close()


@cli_group.command("subreddit", help="Scrape new posts from a subreddit; results go to ./data/<name>/. LIMIT caps new posts; omit to scrape until existing. Use 'refresh' to update.")
@click.argument("name", required=True)
@click.argument("limit", required=False, type=int, metavar="LIMIT")
@shared_options
def cmd_subreddit(
    name: str,
    limit: int | None,
    strategy: str,
    concurrency: int,
    headless: bool,
    export: str | None,
) -> None:
    _run_async(
        async_run,
        strategy,
        concurrency,
        headless,
        export,
        "subreddit",
        name=name.strip(),
        limit=limit,
    )


@cli_group.command("url", help="Scrape one post from a Reddit URL.")
@click.argument("url", required=True)
@shared_options
def cmd_url(
    url: str,
    strategy: str,
    concurrency: int,
    headless: bool,
    export: str | None,
) -> None:
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        raise click.BadParameter("URL must start with http:// or https://", param_hint="url")
    _run_async(
        async_run,
        strategy,
        concurrency,
        headless,
        export,
        "url",
        url=u,
    )


@cli_group.command("refresh", help="Update ./data/<name>/: re-fetch upvotes, comment counts, new comments. NAME = dataset name. LIMIT = newest N posts only; omit = all.")
@click.argument("name", required=True)
@click.argument("limit", required=False, type=int, metavar="LIMIT")
@shared_options
def cmd_refresh(
    name: str,
    limit: int | None,
    strategy: str,
    concurrency: int,
    headless: bool,
    export: str | None,
) -> None:
    _run_async(
        async_run,
        strategy,
        concurrency,
        headless,
        export,
        "refresh",
        name=name.strip(),
        limit=limit,
    )


def main() -> None:
    cli_group()


if __name__ == "__main__":
    main()
