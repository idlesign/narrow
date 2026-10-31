import pytest
from click.testing import CliRunner

from narrow import VERSION, NarrowException, cli


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(["--version"], f"version {VERSION}", id="version"),
        pytest.param(["--help"], "runlocal", id="help"),
        pytest.param(["list-apps"], "py: Pure wsgi application", id="apps"),
        pytest.param(["list-stands"], "uwsgi: uwsgi HTTP router", id="stands"),
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
