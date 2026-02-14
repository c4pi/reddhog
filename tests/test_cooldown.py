import httpx

from reddhog.clients.cooldown import build_cooldown


def test_build_cooldown_429_uses_header_reset(monkeypatch):
    monkeypatch.setattr("reddhog.clients.cooldown.random.uniform", lambda a, _b: a)
    req = httpx.Request("GET", "https://www.reddit.com/r/test/new.json")
    resp = httpx.Response(
        429,
        headers={
            "x-ratelimit-reset": "56",
            "x-ratelimit-remaining": "0",
        },
        request=req,
    )
    total, msg = build_cooldown(429, resp)
    assert total == 61.0
    assert "429" in msg
    assert "reset=56.0" in msg


def test_build_cooldown_403_uses_fallback_and_jitter(monkeypatch):
    monkeypatch.setattr("reddhog.clients.cooldown.random.uniform", lambda a, _b: a)
    total, msg = build_cooldown(403, None)
    assert total == 305
    assert msg == "403"
