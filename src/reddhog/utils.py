from datetime import UTC, datetime
import re


def safe_int(value: str | int | None, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def utc_to_iso(created_utc: float) -> str:
    try:
        dt = datetime.fromtimestamp(created_utc, tz=UTC)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def extract_post_id_from_url(url: str) -> str | None:
    m = re.search(r"/comments/([a-zA-Z0-9]+)", url)
    if m:
        return "t3_" + m.group(1)
    return None


def extract_subreddit_from_url(url: str) -> str | None:
    m = re.search(r"reddit\.com/r/([a-zA-Z0-9_]+)", url)
    if m:
        return m.group(1)
    return None
