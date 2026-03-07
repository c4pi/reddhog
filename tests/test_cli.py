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


def test_subreddit_comma_list_with_limit(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_async(async_run_fn, *args, **kwargs):
        _ = async_run_fn, args
        calls.append(kwargs)

    monkeypatch.setattr(cli_mod, "_run_async", fake_run_async)
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli_group,
        ["subreddit", "python,learnpython,programming", "100"],
    )
    assert result.exit_code == 0
    assert [c["name"] for c in calls] == ["python", "learnpython", "programming"]
    assert all(c["limit"] == 100 for c in calls)


def test_subreddit_comma_list_without_limit(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_async(async_run_fn, *args, **kwargs):
        _ = async_run_fn, args
        calls.append(kwargs)

    monkeypatch.setattr(cli_mod, "_run_async", fake_run_async)
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli_group,
        ["subreddit", "python,learnpython,programming"],
    )
    assert result.exit_code == 0
    assert [c["name"] for c in calls] == ["python", "learnpython", "programming"]
    assert all(c["limit"] is None for c in calls)


def test_refresh_comma_list_with_limit(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_async(async_run_fn, *args, **kwargs):
        _ = async_run_fn, args
        calls.append(kwargs)

    monkeypatch.setattr(cli_mod, "_run_async", fake_run_async)
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli_group,
        ["refresh", "python,learnpython,programming", "20"],
    )
    assert result.exit_code == 0
    assert [c["name"] for c in calls] == ["python", "learnpython", "programming"]
    assert all(c["limit"] == 20 for c in calls)


def test_refresh_comma_list_without_limit(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_run_async(async_run_fn, *args, **kwargs):
        _ = async_run_fn, args
        calls.append(kwargs)

    monkeypatch.setattr(cli_mod, "_run_async", fake_run_async)
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli_group,
        ["refresh", "python,learnpython,programming"],
    )
    assert result.exit_code == 0
    assert [c["name"] for c in calls] == ["python", "learnpython", "programming"]
    assert all(c["limit"] is None for c in calls)
