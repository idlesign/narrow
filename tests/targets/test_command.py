import json
from types import SimpleNamespace

import pytest

from narrow.exceptions import NarrowException
from narrow.processes import CommandResult
from narrow.targets import CommandTarget

def test_command_preserves_argv_cwd_and_private_environment(command_environment: SimpleNamespace) -> None:
    environment = command_environment
    target = environment.target

    metadata = target.prepare()

    with target.setup(timeout=2):
        target.check_running()

    assert metadata["binary"]["path"] == f"{environment.binary}"
    assert "PRIVATE" not in repr(metadata)
    arguments = environment.create.call_args_list[0]
    assert arguments.args[0] == (f"{environment.binary}", "--flag")
    assert arguments.kwargs["cwd"] == environment.binary.parent
    assert arguments.kwargs["env"]["SECRET"] == "PRIVATE"
    assert target.get_evidence()[0]["files"]["stderr.bin"] == b"[redacted]"
    assert target.get_evidence()[0]["readiness"] == {"status": "ok", "attempts": 1, "maximum_attempts": 8}
    assert target.process is None
    environment.server.stop.assert_called_once()
    environment.probe.wait.assert_called_once_with(timeout=2)

@pytest.mark.parametrize(("switch", "value"), [
    ("PYTHONDONTWRITEBYTECODE", "1"), ("PYTHONNOUSERSITE", "1"), ("PYTHONUNBUFFERED", "1"),
    ("PYTHONSAFEPATH", "1"), ("PYTHONUTF8", "0"),
])
def test_public_env_switches_preserve_command_and_diagnostics(
    command_environment: SimpleNamespace, switch: str, value: str,
) -> None:
    environment = command_environment
    argv = ("./server", "--http11-socket", "127.0.0.1:8011", "--workers", "1")
    output = {"pid": 11201, "start_ticks": 1171271, "address": "127.0.0.1:8011"}
    environment.server.collect_output.return_value = (json.dumps(output).encode(), b"PRIVATE")
    target = CommandTarget(
        "http://127.0.0.1:8011/", argv, cwd=environment.binary.parent,
        env={switch: value, "SECRET": "PRIVATE"},
    )

    metadata = target.prepare()

    with target.setup(timeout=1):
        pass

    evidence = target.get_evidence()[0]
    assert metadata["argv"] == [f"{environment.binary}", *argv[1:]]
    assert evidence["argv"] == metadata["argv"]
    assert json.loads(evidence["files"]["stdout.bin"]) == output
    assert json.loads(evidence["files"]["ready-stdout.bin"]) == {"attempts": 1}
    assert evidence["files"]["stderr.bin"] == b"[redacted]"
    assert "PRIVATE" not in repr(metadata) + repr(evidence)

@pytest.mark.parametrize(("overrides", "private_value"), [
    ({"TOKEN": "1", "PYTHONNOUSERSITE": "1"}, "1"),
    ({"TOKEN": "0", "PYTHONUTF8": "0"}, "0"),
    ({"CUSTOM": "x"}, "x"),
    ({"PYTHONUNBUFFERED": "PRIVATE"}, "PRIVATE"),
])
def test_short_and_nonstandard_env_values_remain_private(
    command_environment: SimpleNamespace, overrides: dict[str, str], private_value: str,
) -> None:
    environment = command_environment
    environment.server.collect_output.return_value = (private_value.encode(), private_value.encode())
    target = CommandTarget(
        "http://127.0.0.1:8080/", ("./server", f"before{private_value}after"),
        cwd=environment.binary.parent, env=overrides,
    )

    metadata = target.prepare()

    with target.setup(timeout=1):
        pass

    evidence = target.get_evidence()[0]
    assert metadata["argv"][-1] == "before[redacted]after"
    assert evidence["argv"] == metadata["argv"]
    assert evidence["files"]["stdout.bin"] == evidence["files"]["stderr.bin"] == b"[redacted]"

@pytest.mark.parametrize("executable", ["./venv/bin/python", "python"])
def test_command_preserves_interpreter_symlink(
    command_environment: SimpleNamespace, executable: str,
) -> None:
    environment = command_environment
    launcher = environment.binary.parent / "venv/bin/python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(environment.binary)
    target = CommandTarget(
        "http://127.0.0.1:8080/", (executable, "--flag"), cwd=environment.binary.parent,
        env={"PATH": f"{launcher.parent}"},
    )

    metadata = target.prepare()

    with target.setup(timeout=1):
        pass

    assert metadata["binary"]["path"] == f"{environment.binary}"
    assert environment.create.call_args_list[0].args[0] == (f"{launcher}", "--flag")

@pytest.mark.parametrize(("returncode", "error"), [(1, None), (0, "timed out")])
def test_readiness_failure_stops_only_owned_server(
    command_environment: SimpleNamespace, returncode: int, error: str | None,
) -> None:
    environment = command_environment
    environment.probe.wait.return_value = CommandResult((), returncode, b"", b"bad readiness", 1, error)

    with pytest.raises(NarrowException, match="did not become ready"):

        with environment.target.setup(timeout=1):
            pytest.fail("target was not ready")

    environment.server.stop.assert_called_once()
    assert environment.target.get_evidence()[0]["readiness"]["status"] == "failed"

def test_probe_cancellation_preserves_primary_and_cleanup_evidence(command_environment: SimpleNamespace) -> None:
    environment = command_environment
    environment.probe.wait.side_effect = KeyboardInterrupt
    environment.server.stop.side_effect = NarrowException("cannot confirm cleanup")

    with pytest.raises(KeyboardInterrupt) as captured:

        with environment.target.setup(timeout=1):
            pytest.fail("probe was interrupted")

    assert "cannot confirm cleanup" in " ".join(captured.value.__notes__)
    assert environment.target.get_evidence()[0]["cleanup_error"] == "cannot confirm cleanup"

def test_early_exit_does_not_start_readiness_probe(command_environment: SimpleNamespace) -> None:
    environment = command_environment
    environment.server.check_running.side_effect = NarrowException("server exited")

    with pytest.raises(NarrowException, match="server exited"):

        with environment.target.setup(timeout=1):
            pytest.fail("server exited")

    assert environment.create.call_count == 1
    environment.server.stop.assert_called_once()

def test_busy_listener_prevents_command_launch(command_environment: SimpleNamespace) -> None:
    environment = command_environment
    environment.available.side_effect = NarrowException("unavailable")

    with pytest.raises(NarrowException, match="unavailable"):

        with environment.target.setup(timeout=1):
            pytest.fail("listener is occupied")

    environment.create.assert_not_called()

def test_command_cleanup_failure_is_visible(command_environment: SimpleNamespace) -> None:
    environment = command_environment
    environment.server.stop.side_effect = NarrowException("cannot stop server")

    with pytest.raises(NarrowException, match="cannot stop server"):

        with environment.target.setup(timeout=1):
            pass

    assert environment.target.get_evidence()[0]["cleanup_error"] == "cannot stop server"

def test_invalid_probe_result_still_cleans_up(command_environment: SimpleNamespace) -> None:
    environment = command_environment
    environment.probe.wait.return_value = CommandResult((), 0, b'{"attempts":true}', b"", 1)

    with pytest.raises(NarrowException, match="Malformed readiness"):

        with environment.target.setup(timeout=1):
            pytest.fail("invalid probe result")

    environment.server.stop.assert_called_once()

def test_command_start_failure_keeps_evidence(command_environment: SimpleNamespace) -> None:
    environment = command_environment
    environment.server.start.side_effect = NarrowException("failed to exec")

    with pytest.raises(NarrowException, match="failed to exec"):

        with environment.target.setup(timeout=1):
            pytest.fail("failed start")

    environment.server.stop.assert_called_once()
    assert environment.create.call_count == 1

def test_command_rejects_nonlocal_listener_and_bad_environment() -> None:

    with pytest.raises(NarrowException, match="loopback"):
        CommandTarget("http://192.0.2.1:8080/", ("server",))

    with pytest.raises(ValueError, match="environment"):
        CommandTarget("http://127.0.0.1:8080/", ("server",), env={"A=B": "value"})
