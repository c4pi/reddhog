from pathlib import Path

from reddhog import config as config_mod


def test_profile_base_env_override(monkeypatch) -> None:
    monkeypatch.setenv("REDDHOG_PROFILE_BASE", "~/profiles/reddhog")
    expected = Path("~/profiles/reddhog").expanduser()
    assert config_mod._profile_base_from_env_or_default() == expected


def test_default_profile_base_linux(monkeypatch) -> None:
    monkeypatch.setattr(config_mod.sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(config_mod.Path, "home", lambda: Path("/home/tester"))
    assert config_mod._default_profile_base() == Path("/home/tester/.local/state/reddhog/browser_profile")


def test_default_profile_base_linux_xdg_state_home(monkeypatch) -> None:
    monkeypatch.setattr(config_mod.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", "/var/lib/state")
    assert config_mod._default_profile_base() == Path("/var/lib/state/reddhog/browser_profile")


def test_default_profile_base_macos(monkeypatch) -> None:
    monkeypatch.setattr(config_mod.sys, "platform", "darwin")
    monkeypatch.setattr(config_mod.Path, "home", lambda: Path("/Users/tester"))
    expected = Path("/Users/tester/Library/Application Support/reddhog/browser_profile")
    assert config_mod._default_profile_base() == expected


def test_default_profile_base_windows(monkeypatch) -> None:
    monkeypatch.setattr(config_mod.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/tester/AppData/Local")
    expected = Path("C:/Users/tester/AppData/Local/reddhog/browser_profile")
    assert config_mod._default_profile_base() == expected
