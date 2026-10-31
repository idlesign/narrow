import io
import os
import signal
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow import NarrowException, processes
from narrow.processes import ProcessHandle, run_command

type ProcessFactory = Callable[..., tuple[Mock, Mock, Mock]]


@pytest.fixture
def process_factory(monkeypatch: pytest.MonkeyPatch) -> ProcessFactory:
    clock = [0.0]
    monkeypatch.setattr(processes, "monotonic", lambda: clock[0])
    monkeypatch.setattr(processes, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))

    def prepare(
        *, stdout: bytes = b"out", stderr: bytes = b"err", exit_code: int | None = 0, hangs: bool = False,
    ) -> tuple[Mock, Mock, Mock]:
        state = [exit_code]
        child = Mock(pid=12345, stdout=io.BytesIO(stdout), stderr=io.BytesIO(stderr))
        child.wait.side_effect = lambda **kwargs: state[0]
        spawn = Mock(return_value=child)

        def observe(*args: object) -> SimpleNamespace | None:

            if state[0] is None:
                return None

            return SimpleNamespace(si_status=abs(state[0]), si_code=os.CLD_EXITED if state[0] >= 0 else os.CLD_KILLED)

        def send_signal(group: int, signum: int) -> None:
            assert group == child.pid

            if signum == 0:
                raise ProcessLookupError

            if signum == signal.SIGKILL or not hangs:
                state[0] = state[0] if state[0] is not None else -signum

        signals = Mock(side_effect=send_signal)
        monkeypatch.setattr(processes.subprocess, "Popen", spawn)
        monkeypatch.setattr(processes.os, "waitid", observe)
        monkeypatch.setattr(processes.os, "killpg", signals)
        return spawn, signals, child

    return prepare


def test_capture_channels_without_shell(process_factory: ProcessFactory) -> None:
    spawn, signals, child = process_factory()
    argv = ["tool with spaces", "$(not-a-command); hello"]
    process = ProcessHandle(argv)

    process.start()
    result = process.wait(timeout=1)
    process.stop()

    assert result.stdout == b"out"
    assert result.stderr == b"err"
    assert result.returncode == 0
    assert result.argv == tuple(argv)
    assert spawn.call_args.kwargs["shell"] is False
    assert spawn.call_args.kwargs["start_new_session"] is True
    assert signals.call_args_list[0].args == (12345, signal.SIGTERM)
    child.poll.assert_not_called()
    child.wait.assert_called_once()


@pytest.mark.parametrize("exit_code", [0, 2])
def test_early_exit_is_not_a_running_stand(exit_code: int, process_factory: ProcessFactory) -> None:
    process_factory(exit_code=exit_code)
    process = ProcessHandle(["server"])
    process.start()

    with pytest.raises(NarrowException, match="exited before"):
        process.check_running()

    process.stop()


def test_timeout_escalates_and_keeps_output(process_factory: ProcessFactory) -> None:
    _, signals, _ = process_factory(exit_code=None, hangs=True)
    process = ProcessHandle(["generator"])
    process.start()

    result = process.wait(timeout=0.1)

    assert result.error == "Process timed out"
    assert result.returncode == -signal.SIGKILL
    assert result.stdout == b"out"
    assert signals.call_args_list[1].args == (12345, signal.SIGKILL)


def test_output_is_bounded_on_both_channels(process_factory: ProcessFactory) -> None:
    process_factory(stdout=b"x" * 50000, stderr=b"y" * 50000)
    process = ProcessHandle(["generator"], output_limit=100)
    process.start()

    result = process.wait(timeout=1)

    assert len(result.stdout) == len(result.stderr) == 100
    assert "truncated" in result.error


def test_metadata_command_rejects_nonzero_exit(process_factory: ProcessFactory) -> None:
    process_factory(exit_code=3)

    with pytest.raises(NarrowException, match="exit 3"):
        run_command(["tool", "--version"])


def test_cancel_preserves_primary_error_and_cleanup_failure(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory(exit_code=None)
    process = ProcessHandle(["generator"])
    process.start()
    monkeypatch.setattr(process, "get_returncode", Mock(side_effect=KeyboardInterrupt))
    monkeypatch.setattr(process, "stop", Mock(side_effect=NarrowException("cannot stop")))

    with pytest.raises(KeyboardInterrupt) as raised:
        process.wait(timeout=1)

    assert raised.value.__notes__ == ["Cleanup failed: cannot stop"]


def test_external_reaping_never_signals_a_stale_group(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, signals, _ = process_factory()
    process = ProcessHandle(["server"])
    process.start()
    monkeypatch.setattr(processes.os, "waitid", Mock(side_effect=ChildProcessError))

    with pytest.raises(NarrowException, match="Child reaped externally"):
        process.stop()

    process.stop()
    signals.assert_not_called()


def test_custom_child_reaping_is_rejected_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    spawn = Mock()
    monkeypatch.setattr(processes.subprocess, "Popen", spawn)
    monkeypatch.setattr(processes.signal, "getsignal", Mock(return_value=signal.SIG_IGN))

    with pytest.raises(NarrowException, match="default SIGCHLD"):
        ProcessHandle(["server"]).start()

    spawn.assert_not_called()
