import random

import httpx

from reddhog.config import (
    COOLDOWN_FALLBACK_MAX,
    COOLDOWN_FALLBACK_MIN,
    COOLDOWN_JITTER_MAX,
    COOLDOWN_JITTER_MIN,
)
from reddhog.models import RateLimitInfo


def build_cooldown(status_code: int, response: httpx.Response | None = None) -> tuple[float, str]:
    if status_code == 429 and response is not None:
        rl_info = RateLimitInfo.from_response(response)
        if rl_info.reset_seconds is not None:
            base = rl_info.reset_seconds
        else:
            base = random.uniform(COOLDOWN_FALLBACK_MIN, COOLDOWN_FALLBACK_MAX)
        remaining = rl_info.remaining if rl_info.remaining is not None else "?"
        message = f"{status_code} (reset={rl_info.reset_seconds}, remaining={remaining})"
    else:
        base = random.uniform(COOLDOWN_FALLBACK_MIN, COOLDOWN_FALLBACK_MAX)
        message = str(status_code)
    total = base + random.uniform(COOLDOWN_JITTER_MIN, COOLDOWN_JITTER_MAX)
    return total, message
