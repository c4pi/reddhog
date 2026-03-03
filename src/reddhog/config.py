import os
from pathlib import Path
import sys


def _default_profile_base() -> Path:
    """Return an OS-appropriate persistent profile base directory."""
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "reddhog" / "browser_profile"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "reddhog" / "browser_profile"
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local" / "state")))
    return state_home / "reddhog" / "browser_profile"


def _profile_base_from_env_or_default() -> Path:
    configured = os.environ.get("REDDHOG_PROFILE_BASE")
    if configured:
        return Path(configured).expanduser()
    return _default_profile_base()


SCRAPER_DATA = Path("data")
IMG_DIR = SCRAPER_DATA / "images"

DEFAULT_CONCURRENCY = 6
REQUEST_DELAY = 1.5
TIMEOUT = 30
IMG_CONCURRENCY = 10
COOLDOWN_JITTER_MIN = 5
COOLDOWN_JITTER_MAX = 20
COOLDOWN_FALLBACK_MIN = 300
COOLDOWN_FALLBACK_MAX = 600
PAGE_WAIT_SHREDDIT_POST_MS = 2000
PAGE_WAIT_SHREDDIT_POST_FIRST_MS = 500
TABS_PER_BROWSER = 2
BROWSER_PROFILE_BASE = _profile_base_from_env_or_default()
BROWSER_NUM_PROFILES = 3
BROWSER_FIRST_PROFILE_DIR = (Path(f"{BROWSER_PROFILE_BASE}_{0}") if BROWSER_NUM_PROFILES > 1 else BROWSER_PROFILE_BASE)
BROWSER_ROTATION_QUOTA = 50
BROWSER_COOLDOWN_FALLBACK_SECONDS = 60
BROWSER_FIRST_LOAD_TIMEOUT_MS = 2000

SCRAPER_DATA.mkdir(parents=True, exist_ok=True)
