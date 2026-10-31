from unittest.mock import Mock
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from click.testing import CliRunner

from narrow import VERSION, NarrowException, cli
from narrow import stats_load
from narrow.reports.asv import read_results
from narrow.reports.plotting import RunReport


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(["--version"], f"version {VERSION}", id="version"),
        pytest.param(["--help"], "run", id="help"),
        pytest.param(["list-apps"], "py: Pure wsgi application", id="apps"),
        pytest.param(["list-stands"], "uwsgi: uWSGI direct HTTP socket", id="stands"),
        pytest.param(["list-stands"], "granian: Granian HTTP/1.1", id="granian"),
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
    ["run"], ["run", "--execute", "--stand", "nginx_static", "--output", "new-run"],
    ["run", "--execute", "--stand", "nginx_static", "--requests", "1", "--duration", "1", "--output", "new-run"],
    ["run", "--execute", "--stand", "nginx_static", "--requests", "1", "--budget", "nan", "--output", "new-run"],
])
def test_cli_rejects_implicit_or_invalid_load(arguments: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    gather = Mock(side_effect=AssertionError("invalid workload reached runner"))
    monkeypatch.setattr(cli, "gather_stats", gather)

    result = CliRunner().invoke(cli.entry_point, arguments)

    assert result.exit_code != 0
    gather.assert_not_called()

def test_dry_run_from_foreign_directory_has_no_external_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gather = Mock(side_effect=AssertionError("dry-run started a workload"))
    monkeypatch.setattr(cli, "gather_stats", gather)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.entry_point, [
        "run", "--url", "https://example.test/original", "--method", "POST", "--path", "/upload?key=PRIVATE",
        "--body", "not-opened.bin", "--header", "Authorization: Bearer SECRET", "--requests", "3", "--dry-run",
        "--generator-cpu", "3", "--generator-cpu", "2", "--generator-address-space-bytes", "1073741824",
        "--budget-file", "not-opened-budget.json",
        "--workload-file", "scenario=not-opened.py", "--target-settings", '{"workers":2}',
    ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "planned"
    assert data["generator_attempts"] == 1
    assert not data["runtime_tools_verified"] and not data["input_files_verified"]
    assert data["cases"][0]["request_url"].startswith("https://example.test/upload?")
    assert data["options"]["request"]["method"] == "POST"
    assert data["options"]["budget_file"] == "not-opened-budget.json"
    assert data["options"]["workload_files"] == {"scenario": "not-opened.py"}
    assert data["options"]["target_settings"] == {"workers": 2}
    assert data["options"]["generator_limits"] == {"cpus": [2, 3], "address_space_bytes": 1024**3, "file_size_bytes": None}
    assert "PRIVATE" not in result.stdout and "SECRET" not in result.stdout
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []
    gather.assert_not_called()

def test_dry_run_preserves_command_arguments_after_separator(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.entry_point, [
        "run", "--url", "http://127.0.0.1:8080/", "--cwd", f"{tmp_path}", "--env", "TOKEN=PRIVATE",
        "--ready-path", "/health", "--requests", "1", "--dry-run", "--", "not-installed-server", "--option", "value",
    ])

    assert result.exit_code == 0, result.output
    target = json.loads(result.stdout)["cases"][0]
    assert target["argv"] == ["not-installed-server", "--option", "value"]
    assert target["env_keys"] == ["TOKEN"]
    assert target["ready_url"] == "http://127.0.0.1:8080/health"
    assert "PRIVATE" not in result.stdout

@pytest.mark.parametrize("extra", [[], ["--execute"], ["--execute", "--dry-run"], ["--dry-run", "--env", "BAD"]])
def test_cli_requires_valid_opt_in_and_command_options(extra: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    gather = Mock(side_effect=AssertionError("invalid invocation reached runner"))
    monkeypatch.setattr(cli, "gather_stats", gather)

    result = CliRunner().invoke(cli.entry_point, ["run", "--url", "http://example.test/", "--requests", "1", *extra])

    assert result.exit_code != 0
    gather.assert_not_called()

@pytest.mark.parametrize(("status", "exit_code"), [("complete", 0), ("failed", 1)])
def test_cli_execute_and_plot_uses_the_same_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, exit_code: int,
) -> None:
    stats = {"status": status}
    gather = Mock(return_value=stats)
    create_plot = Mock()
    monkeypatch.setattr(cli, "gather_stats", gather)
    monkeypatch.setattr(cli, "plot", create_plot)

    result = CliRunner().invoke(cli.entry_point, [
        "run", "--url", "https://example.test/", "--method", "HEAD", "--expected-status", "200", "--expected-bytes", "0",
        "--requests", "1", "--execute", "--output", f"{tmp_path / "run"}", "--plot",
        "--budget-file", f"{tmp_path / "shared-budget.json"}",
    ])

    assert result.exit_code == exit_code, result.output
    options, output = gather.call_args.args
    assert options.request.method == "HEAD" and options.request.expected_response_bytes == 0
    assert options.targets[0].url == "https://example.test/"
    assert options.budget_file == tmp_path / "shared-budget.json"
    assert output == tmp_path / "run"
    create_plot.assert_called_once_with(stats, filename=output / "report.html")

def test_stats_plot_from_foreign_directory_starts_no_workload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "saved"
    source.mkdir()
    (source / "run.json").write_text(json.dumps({"status": "failed", "meta": {}, "attempts": []}))
    gather = Mock(side_effect=AssertionError("report started a workload"))
    monkeypatch.setattr(cli, "gather_stats", gather)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.entry_point, ["stats-plot", "saved", "--output", "report.html"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "report.html\n" and result.stderr == ""
    assert (tmp_path / "report.html").is_file()
    gather.assert_not_called()

def test_toml_dry_run_from_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, datafix_read: Callable[[str], str],
) -> None:
    directory = tmp_path / "config"
    directory.mkdir()
    source = directory / "run.toml"
    source.write_text(datafix_read("run.toml"))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    gather = Mock(side_effect=AssertionError("config dry-run started a workload"))
    monkeypatch.setattr(cli, "gather_stats", gather)

    result = CliRunner().invoke(cli.entry_point, [
        "run", "--cfg", "../config/run.toml", "--dry-run", "--output", "not-created", "--plot",
    ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["generator_attempts"] == 12
    assert [target["name"] for target in data["cases"]] == ["external", "owned"]
    assert data["cases"][1]["cwd"] == f"{directory / "server"}"
    assert not any(secret in result.stdout for secret in ("HEADER_SECRET", "PATH_SECRET", "URL_SECRET", "ENV_SECRET"))
    assert result.stderr == ""
    assert list(foreign.iterdir()) == []
    gather.assert_not_called()

@pytest.mark.parametrize("arguments", [[], ["--execute"], ["--execute", "--dry-run"]])
def test_config_cannot_opt_in_to_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> None:
    read = Mock(side_effect=AssertionError("invalid opt-in read a config"))
    gather = Mock(side_effect=AssertionError("invalid opt-in started a workload"))
    monkeypatch.setattr(cli, "read_run_config", read)
    monkeypatch.setattr(cli, "gather_stats", gather)

    result = CliRunner().invoke(cli.entry_point, ["run", "--cfg", f"{tmp_path / "missing.toml"}", *arguments])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Select exactly one" in result.stderr or "--execute requires --output" in result.stderr
    read.assert_not_called()
    gather.assert_not_called()

@pytest.mark.parametrize("extra", [
    ["--requests", "1"], ["--clients", "1"], ["--method", "GET"], ["--keepalive"], ["--no-keepalive"],
    ["--url", "http://example.test/"], ["--stand", "nginx_static"], ["--app", "py"], ["--name", "custom"],
    ["--header", "Authorization: PRIVATE"], ["--env", "TOKEN=PRIVATE"], ["--", "server", "--flag"],
    ["--budget-file", "not-opened-budget.json"],
])
def test_config_rejects_explicit_cli_run_settings_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str],
) -> None:
    read = Mock(side_effect=AssertionError("conflicting settings read a config"))
    gather = Mock(side_effect=AssertionError("conflicting settings started a workload"))
    monkeypatch.setattr(cli, "read_run_config", read)
    monkeypatch.setattr(cli, "gather_stats", gather)

    result = CliRunner().invoke(cli.entry_point, [
        "run", "--cfg", f"{tmp_path / "missing.toml"}", "--dry-run", *extra,
    ])

    assert result.exit_code == 1
    assert result.stdout == "" and "cannot be combined" in result.stderr
    assert "PRIVATE" not in result.stderr
    read.assert_not_called()
    gather.assert_not_called()

@pytest.mark.parametrize(("contents", "message"), [
    (b"[run", "Invalid run config TOML"),
    (b"execute = true\n[run]\nrequests = 1\n", "Unknown fields in config"),
    (b"[run]\nrequests = 1\n[[targets]]\nname = \"external\"\nurl = \"http://example.test\"\ncwd = \".\"\n", "require argv"),
])
def test_config_errors_use_stderr_without_starting_a_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: bytes, message: str,
) -> None:
    source = tmp_path / "run.toml"
    source.write_bytes(contents)
    gather = Mock(side_effect=AssertionError("invalid config started a workload"))
    monkeypatch.setattr(cli, "gather_stats", gather)

    result = CliRunner().invoke(cli.entry_point, ["run", "--cfg", f"{source}", "--dry-run"])

    assert result.exit_code == 1
    assert result.stdout == "" and message in result.stderr
    gather.assert_not_called()

def test_toml_targets_share_runner_checkpoint_asv_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_environment: SimpleNamespace, command_environment: SimpleNamespace,
) -> None:
    source = tmp_path / "run.toml"
    source.write_text("""[run]
clients = [1, 4]
requests = 2
repeats = 2
warmup_requests = 3

[request]
path = "/api?key=PATH_SECRET"
headers = [["Authorization", "Bearer HEADER_SECRET"]]

[[targets]]
name = "external"
url = "http://example.test"

[[targets]]
name = "owned"
url = "http://127.0.0.1:8080"
argv = ["./server", "--flag"]
env = { TOKEN = "PRIVATE", PYTHONDONTWRITEBYTECODE = "1", PYTHONNOUSERSITE = "1" }
""")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    output = foreign / "run"

    result = CliRunner().invoke(cli.entry_point, [
        "run", "--cfg", f"{source}", "--execute", "--output", "run", "--plot",
    ])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"{output / "run.json"}\n" and result.stderr == ""
    saved = stats_load(output)
    assert saved["status"] == "complete"
    assert [target["ownership"] for target in saved["stands"]] == ["external", "owned"]
    assert len(saved["attempts"]) == run_environment.bencher.run_once.call_count == 12
    assert (output / "report.html").is_file()
    assert not list(output.rglob("*.toml"))
    assert not any(secret in json.dumps(saved) for secret in ("PATH_SECRET", "HEADER_SECRET", "PRIVATE"))
    assert json.loads((output / "stand-0002/owned-ready-stdout.bin").read_bytes()) == {"attempts": 1}
    assert read_results(output, stand="external")[4]["rps"] == 10
    assert read_results(output, stand="owned")[4]["rps"] == 10
    figure = RunReport(saved).create_figure()
    assert [trace.name for trace in figure.data if trace.showlegend] == ["S1 · external", "S2 · owned"]
    command_environment.server.stop.assert_called_once()


def test_granian_config_shares_runner_checkpoint_asv_and_report(
    tmp_path: Path, run_environment: SimpleNamespace,
) -> None:
    source = tmp_path / "run.toml"
    source.write_text(
        "[run]\nstands = [\"granian\", \"granian_ssl\"]\napps = [\"py\"]\nclients = [1, 4]\nrequests = 2\n"
    )
    output = tmp_path / "run"

    result = CliRunner().invoke(cli.entry_point, [
        "run", "--cfg", f"{source}", "--execute", "--output", f"{output}", "--plot",
    ])

    assert result.exit_code == 0, result.output
    saved = stats_load(output)
    assert saved["status"] == "complete"
    assert saved["meta"]["servers"]["granian"]["version"] == "granian test"
    assert len(saved["attempts"]) == run_environment.bencher.run_once.call_count == 4
    assert (output / "report.html").is_file()
    assert read_results(output, stand="granian", app="py")[4]["rps"] == 10
    assert read_results(output, stand="granian_ssl", app="py")[4]["rps"] == 10
    legends = [trace for trace in RunReport(saved).create_figure().data if trace.showlegend]
    assert [trace.meta["target"] for trace in legends] == ["granian", "granian_ssl"]
    assert [trace.meta["subject"]["platform"] for trace in legends] == ["granian", "granian"]
    assert len(run_environment.handles) == 2

    for handle in run_environment.handles:
        handle.stop.assert_called_once()
