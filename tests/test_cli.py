from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from narrow import VERSION, NarrowException, cli


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(["--version"], f"version {VERSION}", id="version"),
        pytest.param(["--help"], "runlocal", id="help"),
        pytest.param(["list-apps"], "py: Pure wsgi application", id="apps"),
        pytest.param(["list-stands"], "uwsgi: uWSGI direct HTTP socket", id="stands"),
        pytest.param(["list-benchers"], "h2load: h2load from nghttp2", id="benchers"),
    ],
)
def test_inspect_cli(arguments: list[str], expected: str) -> None:
    runner = CliRunner()

    result = runner.invoke(cli.entry_point, arguments)

    assert result.exit_code == 0, result.output
    assert expected in result.output


def test_cli_failure_goes_to_stderr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:

    def fail_entry_point(**kwargs: object) -> None:
        raise NarrowException("benchmark unavailable")

    monkeypatch.setattr(cli, "entry_point", fail_entry_point)

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "benchmark unavailable\n"


@pytest.mark.parametrize("arguments", [
    ["runlocal"], ["runlocal", "--stand", "nginx_static", "--output", "new-run"],
    ["runlocal", "--stand", "nginx_static", "--requests", "1", "--duration", "1", "--output", "new-run"],
    ["runlocal", "--stand", "nginx_static", "--requests", "1", "--budget", "nan", "--output", "new-run"],
])
def test_cli_rejects_implicit_or_invalid_load(arguments: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    gather = Mock(side_effect=AssertionError("invalid workload reached runner"))
    monkeypatch.setattr(cli, "gather_stats", gather)

    result = CliRunner().invoke(cli.entry_point, arguments)

    assert result.exit_code != 0
    gather.assert_not_called()
