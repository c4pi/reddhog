from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    import httpx


class Comment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    author: str
    score: str
    text: str
    depth: int = 0
    parent_id: str | None = None


class Image(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    local_path: str


def _default_crawled_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Post(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    flair: str = ""
    description: str = ""
    url: str = ""
    upvotes: int = 0
    comments_count: int = 0
    author: str = "unknown"
    timestamp: str = ""
    images: list[Image] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)
    crawled_at: str = Field(default_factory=_default_crawled_at)

def merge_comment_lists(
    existing: list[Comment], new: list[Comment]
) -> list[Comment]:
    new_by_id = {c.id: c for c in new}
    merged: list[Comment] = []
    seen_ids: set[str] = set()

    for c in existing:
        seen_ids.add(c.id)
        if c.id in new_by_id:
            merged.append(new_by_id[c.id])
        else:
            merged.append(c)

    for c in new:
        if c.id not in seen_ids:
            merged.append(c)
            seen_ids.add(c.id)

    return merged


class RateLimitInfo(BaseModel):
    used: int | None = None
    remaining: int | None = None
    reset_seconds: float | None = None

    @classmethod
    def from_response(cls, resp: "httpx.Response") -> "RateLimitInfo":
        h = resp.headers
        headers = {
            "x-ratelimit-used": h.get("x-ratelimit-used") or h.get("X-Ratelimit-Used"),
            "x-ratelimit-remaining": h.get("x-ratelimit-remaining") or h.get("X-Ratelimit-Remaining"),
            "x-ratelimit-reset": h.get("x-ratelimit-reset") or h.get("X-Ratelimit-Reset"),
        }
        return cls.from_headers(headers)

    @classmethod
    def from_headers(cls, headers: dict[str, str | None]) -> "RateLimitInfo":
        def parse_int(val: str | None) -> int | None:
            if val is None:
                return None
            try:
                return int(float(val))
            except (TypeError, ValueError):
                return None

        def parse_float(val: str | None) -> float | None:
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        return cls(
            used=parse_int(headers.get("x-ratelimit-used")),
            remaining=parse_int(headers.get("x-ratelimit-remaining")),
            reset_seconds=parse_float(headers.get("x-ratelimit-reset")),
        )
