from click.testing import CliRunner

from reddhog import __version__
from reddhog.cli import cli_group
from reddhog.settings import Settings, get_settings


def test_version() -> None:
    assert __version__ == "0.1.0"


def test_settings_defaults() -> None:
    s = Settings()
    assert s.log_level == "INFO"


def test_get_settings() -> None:
    s = get_settings()
    assert isinstance(s, Settings)


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_group, ["--help"])
    assert result.exit_code == 0
    assert "Commands" in result.output


def test_cli_version() -> None:
    runner = CliRunner()
    result = runner.invoke(cli_group, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output
