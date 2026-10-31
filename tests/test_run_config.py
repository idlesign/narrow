import json
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest

from narrow import LoadSpec, NarrowException, Runner, SubjectSpec, read_run_config
from narrow.targets import command as target_commands

URL_TARGET = "[[targets]]\nname = \"external\"\nurl = \"http://example.test/\"\n"

def test_read_config_without_opening_references_or_starting_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, datafix_read: Callable[[str], str],
) -> None:
    source = tmp_path / "run.toml"
    source.write_text(datafix_read("run.toml"))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    start = Mock(side_effect=AssertionError("config started a process"))
    monkeypatch.setattr(target_commands, "ProcessHandle", start)

    options = read_run_config(source)
    plan = Runner(options).plan()

    assert options.loads == tuple(LoadSpec(
        clients=clients, requests=10, worker_threads=2, timeout=1, connect_timeout=0.5, keepalive=False, drain=False,
    ) for clients in (1, 4))
    assert options.repeats == 2 and options.warmup_requests == 3 and options.budget == 90
    assert options.ready_timeout == 2 and options.attempt_timeout == 12
    assert options.request.method == "POST" and options.request.expected_status == 201
    assert options.request.expected_response_bytes == 12 and options.request.max_body_bytes == 32
    assert options.request.body == tmp_path / "inputs/body.bin"
    assert options.request.ca_file == tmp_path / "inputs/ca.pem"
    assert [target.alias for target in options.targets] == ["external", "owned"]
    assert options.targets[1].cwd == tmp_path / "server"
    assert options.targets[1].argv == ("./venv/bin/python", "server.py", "ENV_SECRET")
    assert options.targets[1].env == {"TOKEN": "ENV_SECRET"}
    assert options.targets[1].ready_status == 204
    assert plan["generator_attempts"] == 12
    assert not plan["input_files_verified"] and not plan["runtime_tools_verified"]
    assert not any(secret in json.dumps(plan) for secret in ("HEADER_SECRET", "PATH_SECRET", "URL_SECRET", "ENV_SECRET"))
    assert list(foreign.iterdir()) == []
    assert not (tmp_path / "server").exists() and not (tmp_path / "inputs").exists()
    start.assert_not_called()

def test_command_defaults_to_config_directory_and_preserves_absolute_inputs(tmp_path: Path) -> None:
    source = tmp_path / "run.toml"
    body = tmp_path / "absent-body"
    source.write_text(
        f"[run]\nrequests = 1\n[request]\nbody = {json.dumps(f"{body}")}\n"
        "[[targets]]\nname = \"owned\"\nurl = \"http://127.0.0.1:8080\"\nargv = [\"missing-server\"]\n"
    )

    options = read_run_config(source)

    assert options.targets[0].cwd == tmp_path
    assert options.request.body == body
    assert not body.exists()

def test_builtin_selection_uses_existing_runner(tmp_path: Path) -> None:
    source = tmp_path / "run.toml"
    source.write_text(
        "[run]\nstands = [\"nginx_static\", \"uwsgi\", \"granian\", \"granian_ssl\"]\n"
        "apps = [\"py\"]\nrequests = 2\n"
    )

    options = read_run_config(source)
    plan = Runner(options).plan()

    assert options.stands == ("nginx_static", "uwsgi", "granian", "granian_ssl") and options.apps == ("py",)
    assert [case["name"] for case in plan["cases"]] == ["nginx_static", "uwsgi", "granian", "granian_ssl"]
    assert options.targets == ()

def test_duration_and_requested_qps_use_load_spec(tmp_path: Path) -> None:
    source = tmp_path / "run.toml"
    source.write_text("[run]\nclients = [2]\nduration = 0.5\nqps = 10\n" + URL_TARGET)

    options = read_run_config(source)

    assert options.loads == (LoadSpec(clients=2, duration=0.5, qps=10),)

@pytest.mark.parametrize("command", ["", "argv = [\"absent-server\"]\n"])
def test_subject_config_is_portable_and_does_not_inspect_the_runtime(tmp_path: Path, command: str) -> None:
    source = tmp_path / "run.toml"
    source.write_text(
        "[run]\nrequests = 2\n[[targets]]\nname = \"server\"\nurl = \"http://127.0.0.1:8080/\"\n"
        f"{command}[targets.subject]\nplatform = \"any-platform\"\nrevision = \"unchanged-version\"\n"
        f"[targets.subject.artifact_sha256]\nruntime = \"{"a" * 64}\"\n"
    )

    options = read_run_config(source)
    plan = Runner(options).plan()

    expected = SubjectSpec("any-platform", "unchanged-version", {"runtime": "a" * 64}).serialize()
    assert plan["cases"][0]["subject"] == plan["options"]["targets"][0]["subject"] == expected
    assert not plan["runtime_tools_verified"]
    assert list(tmp_path.iterdir()) == [source]

@pytest.mark.parametrize(("subject", "message"), [
    ("subject = []", "subject must be a table"),
    ("[targets.subject]\nrevision = \"label\"", "subject requires platform"),
    ("[targets.subject]\nplatform = \"server\"\nversion = \"1\"", "Unknown fields"),
    ("[targets.subject]\nplatform = \"server\"\nartifact_sha256 = {runtime = \"short\"}", "SHA-256"),
])
def test_reject_invalid_subject_config(tmp_path: Path, subject: str, message: str) -> None:
    source = tmp_path / "run.toml"
    source.write_text(f"[run]\nrequests = 1\n{URL_TARGET}{subject}\n")

    with pytest.raises(ValueError, match=message):
        read_run_config(source)

@pytest.mark.parametrize(("run", "message"), [
    ("requests = 1\nrepeetz = 2", "Unknown fields in run"),
    ("requests = 1\nclients = true", "run.clients"),
    ("requests = 1\nclients = []", "run.clients"),
    ("requests = 1\nclients = [true]", "run.clients"),
    ("requests = 1\nclients = [1, 1]", "Duplicate concurrency"),
    ("requests = 1\nstands = \"uwsgi\"", "run.stands"),
    ("requests = 1\napps = [\"py\"]", "run.apps requires run.stands"),
    ("requests = 1\nbencher = 2", "run.bencher"),
    ("requests = 1\nrequest_timeout = nan", "timeout must be finite"),
    ("requests = 1\nduration = 0.1", "exactly one of requests or duration"),
    ("requests = 1\nrepeats = true", "Invalid repeats"),
    ("requests = 1\nbudget = 1", "reserve time"),
])
def test_reject_invalid_run_settings(tmp_path: Path, run: str, message: str) -> None:
    source = tmp_path / "run.toml"
    source.write_text(f"[run]\n{run}\n{URL_TARGET}")

    with pytest.raises(ValueError, match=message):
        read_run_config(source)

@pytest.mark.parametrize(("setting", "message"), [
    ("unknown = 1", "Unknown fields in request"),
    ("body = false", "request.body"),
    ("ca_file = 1", "request.ca_file"),
    ("headers = { Authorization = \"PRIVATE\" }", "array of name/value arrays"),
    ("headers = [\"PRIVATE\"]", "array of name/value arrays"),
    ("headers = [[\"X-Test\"]]", "name/value pairs"),
])
def test_reject_invalid_request_settings(tmp_path: Path, setting: str, message: str) -> None:
    source = tmp_path / "run.toml"
    source.write_text(f"[run]\nrequests = 1\n[request]\n{setting}\n{URL_TARGET}")

    with pytest.raises(ValueError, match=message) as raised:
        read_run_config(source)

    assert "PRIVATE" not in f"{raised.value}"

@pytest.mark.parametrize(("target", "message"), [
    ("url = \"http://example.test/\"", "requires name and url"),
    ("name = \"missing-url\"", "requires name and url"),
    ("name = \"external\"\nurl = 123", "URL must not contain"),
    ("name = \"external\"\nurl = \"http://example.test\"\ncwd = \".\"", "command settings require argv"),
    ("name = \"owned\"\nurl = \"http://127.0.0.1:8080\"\nargv = \"server\"", "array of strings"),
    ("name = \"owned\"\nurl = \"http://127.0.0.1:8080\"\nargv = []", "nonempty sequence"),
    ("name = \"owned\"\nurl = \"http://127.0.0.1:8080\"\nargv = [\"server\"]\nenv = []", "env must be a table"),
    ("name = \"owned\"\nurl = \"http://127.0.0.1:8080\"\nargv = [\"server\"]\nenv = {TOKEN = 1}", "Invalid command environment"),
])
def test_reject_invalid_target_settings(tmp_path: Path, target: str, message: str) -> None:
    source = tmp_path / "run.toml"
    source.write_text(f"[run]\nrequests = 1\n[[targets]]\n{target}\n")

    with pytest.raises(ValueError, match=message):
        read_run_config(source)

@pytest.mark.parametrize(("contents", "message"), [
    ("ignored = true\n[run]\nrequests = 1\n" + URL_TARGET, "Unknown fields in config"),
    ("run = []\n" + URL_TARGET, "run must be a table"),
    ("request = []\n[run]\nrequests = 1\n" + URL_TARGET, "request must be a table"),
    ("targets = {}\n[run]\nrequests = 1\n", "targets must be an array"),
    ("[run]\nrequests = 1\n[[targets]]\n", "requires name and url"),
    ("[run]\nrequests = 1\n", "Select at least one target"),
])
def test_reject_invalid_config_structure(tmp_path: Path, contents: str, message: str) -> None:
    source = tmp_path / "run.toml"
    source.write_text(contents)

    with pytest.raises(ValueError, match=message):
        read_run_config(source)

@pytest.mark.parametrize(("contents", "message"), [
    ("[run]\nrequests = 1\nbencher = \"missing\"\n" + URL_TARGET, "Unknown bencher"),
    ("[run]\nrequests = 1\nstands = [\"missing\"]\n", "Unknown stand"),
    ("[run]\nrequests = 1\n" + URL_TARGET + URL_TARGET, "Duplicate target"),
])
def test_delegate_target_validation_to_runner(tmp_path: Path, contents: str, message: str) -> None:
    source = tmp_path / "run.toml"
    source.write_text(contents)

    with pytest.raises(NarrowException, match=message):
        read_run_config(source)

@pytest.mark.parametrize("contents", [
    b"[run", b"\xff", b"password = \"PRIVATE", b"clients = " + b"[" * 1500 + b"]" * 1500,
])
def test_reject_invalid_toml_without_echoing_contents(tmp_path: Path, contents: bytes) -> None:
    source = tmp_path / "run.toml"
    source.write_bytes(contents)

    with pytest.raises(ValueError, match="Invalid run config TOML") as raised:
        read_run_config(source)

    assert "PRIVATE" not in f"{raised.value}"

def test_reject_oversized_config(tmp_path: Path) -> None:
    source = tmp_path / "run.toml"
    source.write_bytes(b" " * (1024 * 1024 + 1))

    with pytest.raises(ValueError, match="1 MiB size limit"):
        read_run_config(source)

def test_reject_fifo_without_waiting_for_a_writer(tmp_path: Path) -> None:
    source = tmp_path / "run.toml"
    os.mkfifo(source)

    with pytest.raises(ValueError, match="regular TOML file"):
        read_run_config(source)
