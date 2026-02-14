from click.testing import CliRunner

from reddhog import cli as cli_mod


def test_subreddit_rejects_zero_concurrency() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli_group,
        ["subreddit", "python", "1", "--concurrency", "0"],
    )
    assert result.exit_code != 0
    assert "x>=1" in result.output


def test_subreddit_accepts_positive_concurrency(monkeypatch) -> None:
    called: dict[str, tuple | dict] = {}

    def fake_run_async(async_run_fn, *args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs

    monkeypatch.setattr(cli_mod, "_run_async", fake_run_async)
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli_group,
        ["subreddit", "python", "1", "--concurrency", "1"],
    )
    assert result.exit_code == 0
    assert called["args"][1] == 1
