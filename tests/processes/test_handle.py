import io
import json
import os
import signal
from pathlib import Path
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow import NarrowException
from narrow.processes import handle as processes
from narrow.processes import ProcessHandle, run_command
from narrow.processes.limits import ProcessLimits

type ProcessFactory = Callable[..., tuple[Mock, Mock, Mock]]


@pytest.fixture
def process_factory(monkeypatch: pytest.MonkeyPatch) -> ProcessFactory:
    clock = [0.0]
    monkeypatch.setattr(processes, "monotonic", lambda: clock[0])
    monkeypatch.setattr(processes, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))

    def prepare(
        *, stdout: bytes = b"out", stderr: bytes = b"err", exit_code: int | None = 0, hangs: bool = False,
        report: bytes | None = None, exit_at: float | None = None,
    ) -> tuple[Mock, Mock, Mock]:
        state = [exit_code]
        input_pipe = io.BytesIO()
        input_pipe.fileno = lambda: 42
        child = Mock(pid=12345, stdin=input_pipe, stdout=io.BytesIO(stdout), stderr=io.BytesIO(stderr))

        def spawn_child(*args: object, **kwargs: object) -> Mock:

            if report is not None:
                os.write(kwargs["pass_fds"][0], report)

            return child

        spawn = Mock(side_effect=spawn_child)

        def observe(*args: object) -> SimpleNamespace | None:

            if exit_at is not None and clock[0] >= exit_at:
                state[0] = 0

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
        usage = SimpleNamespace(ru_utime=0.25, ru_stime=0.125, ru_maxrss=2048)
        reap = Mock(side_effect=lambda *args: (
            child.pid, state[0] << 8 if state[0] >= 0 else -state[0], usage,
        ))
        class Reader:
            def __init__(self, *, target: Callable, args: tuple, daemon: bool) -> None:
                self.target, self.args = target, args

            def start(self) -> None:
                self.target(*self.args)

            def join(self, *, timeout: float) -> None:
                pass

            def is_alive(self) -> bool:
                return False

        monkeypatch.setattr(processes, "Thread", Reader)
        tree = Mock()
        tree.has_live_descendants.return_value = False
        tree.get_remaining.return_value = []
        tree.signals = []
        monkeypatch.setattr(processes, "ProcessTree", Mock(return_value=tree))
        monkeypatch.setattr(processes.subprocess, "Popen", spawn)
        monkeypatch.setattr(processes.os, "waitid", observe)
        monkeypatch.setattr(processes.os, "wait4", reap)
        monkeypatch.setattr(processes.os, "killpg", signals)
        monkeypatch.setattr(processes.os, "set_blocking", Mock())
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
    assert spawn.call_args.kwargs["stdin"] == processes.subprocess.DEVNULL
    assert signals.call_args_list[0].args == (12345, signal.SIGTERM)
    child.poll.assert_not_called()
    child.wait.assert_not_called()
    assert child.returncode == 0
    assert result.execution["resources"] == {
        "user_seconds": 0.25, "system_seconds": 0.125, "max_rss_bytes": 2097152,
        "scope": "reaped_process_and_waited_children_entire_lifetime_including_launcher",
        "measured_window_only": False,
    }
    assert not result.execution["termination_requested"]

def test_process_receives_explicit_cwd_and_environment(process_factory: ProcessFactory, tmp_path: Path) -> None:
    spawn, _signals, _child = process_factory()
    environment = {"ONLY_THIS": "value"}
    process = ProcessHandle(["tool"], cwd=tmp_path, env=environment)
    environment["ONLY_THIS"] = "changed"

    process.start()
    process.wait(timeout=1)

    assert spawn.call_args.kwargs["cwd"] == tmp_path
    assert spawn.call_args.kwargs["env"] == {"ONLY_THIS": "value"}


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
    assert result.execution["termination_requested"] and result.execution["kill_escalated"]
    assert result.execution["timeout_seconds"] == 0.1
    assert result.execution["resources"]["user_seconds"] == 0.25


def test_output_is_bounded_on_both_channels(process_factory: ProcessFactory) -> None:
    process_factory(stdout=b"x" * 50000, stderr=b"y" * 50000)
    process = ProcessHandle(["generator"], output_limit=100)
    process.start()

    result = process.wait(timeout=1)

    assert len(result.stdout) == len(result.stderr) == 100
    assert "truncated" in result.error
    assert result.execution["output_truncated"]
    assert result.execution["output_limit_per_stream_bytes"] == 100


def test_limits_are_confirmed_separately_from_generator_output(process_factory: ProcessFactory) -> None:
    applied = {
        "cpus": [2], "address_space_bytes": 1024**3, "address_space_hard_bytes": 1024**3, "core_dump_bytes": 0,
        "file_size_bytes": None, "file_size_hard_bytes": None,
    }
    spawn, _signals, _child = process_factory(report=json.dumps({"applied": applied, "error": None}).encode())
    limits = ProcessLimits(cpus=(2,), address_space_bytes=1024**3)
    process = ProcessHandle(["generator", "argument"], limits=limits)

    process.start()
    result = process.wait(timeout=1)

    launch = spawn.call_args.args[0]
    descriptor = spawn.call_args.kwargs["pass_fds"][0]
    assert "-I" in launch and launch[-2:] == ("generator", "argument")
    assert json.loads(launch[-3]) == {"limits": limits.serialize(), "report_fd": descriptor}
    assert "preexec_fn" not in spawn.call_args.kwargs
    assert result.argv == ("generator", "argument")
    assert (result.stdout, result.stderr) == (b"out", b"err")
    assert result.error is None
    assert result.execution["limits"]["requested"] == limits.serialize()
    assert result.execution["limits"]["applied"] == applied
    assert result.execution["limits"]["error"] is None
    assert process.get_execution() == result.execution

    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptor)


def test_custom_launcher_receives_one_borrowed_report_fd(process_factory: ProcessFactory) -> None:
    applied = {
        "cpus": [2], "address_space_bytes": None, "address_space_hard_bytes": None,
        "core_dump_bytes": 0, "file_size_bytes": 8192, "file_size_hard_bytes": 8192,
    }
    spawn, _signals, child = process_factory(report=json.dumps({"applied": applied, "error": None}).encode())
    limits = ProcessLimits(cpus=(2,), file_size_bytes=8192)
    launcher = Mock(return_value=["authenticated-launcher", "--fd-exec"])
    process = ProcessHandle(["native-tool", "--flag"], limits=limits, launcher=launcher)
    assert process.pid is None

    process.start()
    result = process.wait(timeout=1)

    descriptor = spawn.call_args.kwargs["pass_fds"][0]
    launcher.assert_called_once_with(("native-tool", "--flag"), limits, descriptor)
    assert spawn.call_args.args[0] == ("authenticated-launcher", "--fd-exec")
    assert process.pid == child.pid
    assert result.error is None and result.argv == ("native-tool", "--flag")
    assert result.execution["limits"]["applied"]["file_size_bytes"] == 8192


def test_observation_failure_preserves_primary_and_uses_configured_stop(
    process_factory: ProcessFactory,
) -> None:
    _spawn, signals, _child = process_factory(exit_code=None)
    process = ProcessHandle(["generator"], stop_signal=signal.SIGINT, stop_grace=0.1)
    process.start()
    observe = Mock(side_effect=ValueError("native observation rejected"))

    with pytest.raises(ValueError, match="native observation rejected"):
        process.wait(timeout=1, observe=observe)

    observe.assert_called_once()
    assert signals.call_args_list[0].args == (12345, signal.SIGINT)
    assert process.get_returncode() == -signal.SIGINT
    assert process.get_execution()["resources"] is not None


def test_observation_also_runs_for_an_already_exited_child(process_factory: ProcessFactory) -> None:
    process_factory()
    process = ProcessHandle(["generator"])
    process.start()
    observe = Mock()

    process.wait(timeout=1, observe=observe)

    observe.assert_called_once()


@pytest.mark.parametrize("report", [
    b"", b"not JSON", b'{"applied":null,"error":null}', b'{"applied":{},"error":null}', b"x" * (65536 + 1),
    b"[" * 2000 + b"]" * 2000,
    b'{"applied":{"cpus":[3],"core_dump_bytes":0,"address_space_bytes":null,"address_space_hard_bytes":null},"error":null}',
])
def test_unconfirmed_limits_invalidate_even_zero_exit(report: bytes, process_factory: ProcessFactory) -> None:
    process_factory(report=report)
    process = ProcessHandle(["generator"], limits=ProcessLimits(cpus=(2,)))

    process.start()
    result = process.wait(timeout=1)

    assert result.returncode == 0
    assert "Process limits setup failed" in result.error
    assert result.execution["limits"]["applied"] is None
    assert result.execution["limits"]["error"]


def test_limits_error_survives_child_failure(process_factory: ProcessFactory) -> None:
    process_factory(exit_code=1, report=b'{"applied":null,"error":"CPU mask unavailable"}')
    process = ProcessHandle(["generator"], limits=ProcessLimits(cpus=(2,)))

    process.start()
    result = process.wait(timeout=1)

    assert result.error == "Process limits setup failed: CPU mask unavailable"
    assert result.execution["limits"]["error"] == "CPU mask unavailable"


def test_spawn_failure_closes_private_report(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn, _signals, _child = process_factory()
    spawn.side_effect = FileNotFoundError("test spawn failure")
    open_file = processes.TemporaryFile
    files = []

    def create_report(**kwargs: object) -> object:
        stream = open_file(**kwargs)
        files.append(stream)
        return stream

    monkeypatch.setattr(processes, "TemporaryFile", create_report)
    process = ProcessHandle(["generator"], limits=ProcessLimits())

    with pytest.raises(NarrowException, match="Cannot start generator"):
        process.start()

    assert len(files) == 1 and files[0].closed
    assert process.get_execution()["resources"] is None
    assert process.get_execution()["limits"]["applied"] is None


def test_cancel_keeps_lifetime_accounting(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory(exit_code=None)
    process = ProcessHandle(["generator"])
    process.start()
    monkeypatch.setattr(processes, "sleep", Mock(side_effect=KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        process.wait(timeout=1)

    execution = process.get_execution()
    assert execution["termination_requested"]
    assert execution["resources"]["max_rss_bytes"] == 2097152
    assert process.get_returncode() == -signal.SIGTERM


def test_reaping_has_a_deadline(process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    process_factory()
    monkeypatch.setattr(processes.os, "wait4", Mock(return_value=(0, 0, None)))
    process = ProcessHandle(["generator"])
    process.start()

    with pytest.raises(NarrowException, match="Could not reap generator after SIGKILL"):
        process.stop(grace=0.02)

    assert process.get_execution()["resources"] is None


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

def test_json_records_and_streamed_logs_share_the_bounded_capture(process_factory: ProcessFactory, tmp_path: Path) -> None:
    payload = b'{"kind":"ready"}\n{"kind":"final"}\n'
    process_factory(stdout=payload, stderr=b"")
    process = ProcessHandle(["external-backend"], log_directory=tmp_path)

    process.start()
    ready = process.read_record(timeout=1)
    result = process.wait(timeout=1)
    final = process.read_record(timeout=1)

    assert ready == {"kind": "ready"} and final == {"kind": "final"}
    assert process.stdout_offset == len(payload)
    assert result.stdout == payload and (tmp_path / "stdout.log").read_bytes() == payload
    assert (tmp_path / "stderr.log").read_bytes() == b""
    assert result.execution["cleanup_complete"] and result.execution["cleanup"]["root_reaped"]


@pytest.mark.parametrize("payload,message", [(b"null\n", "JSON object"), (b"{\n", "property name")])
def test_invalid_record_is_rejected_without_losing_output(
    process_factory: ProcessFactory, payload: bytes, message: str,
) -> None:
    process_factory(stdout=payload)
    process = ProcessHandle(["external-backend"])
    process.start()

    try:

        with pytest.raises(ValueError, match=message):
            process.read_record(timeout=1)

        assert process.collect_output()[0] == payload

    finally:
        process.stop()


def test_partial_record_has_a_fake_clock_deadline(process_factory: ProcessFactory) -> None:
    process_factory(stdout=b'{"partial":', exit_code=None)
    process = ProcessHandle(["external-backend"])
    process.start()

    try:

        with pytest.raises(TimeoutError, match="record deadline"):
            process.read_record(timeout=0.1)

    finally:
        process.stop()


def test_startup_cancellation_reaps_and_closes_its_tree(
    process_factory: ProcessFactory, tmp_path: Path,
) -> None:
    _spawn, _signals, child = process_factory(exit_code=None)
    process = ProcessHandle(["external-backend"], log_directory=tmp_path)
    process.tree.discover.side_effect = KeyboardInterrupt("cancel owned startup")

    with pytest.raises(KeyboardInterrupt, match="cancel owned startup"):
        process.start()

    assert child.returncode == -signal.SIGTERM and child.stdout.closed and child.stderr.closed
    process.tree.close.assert_called_once()
    assert process.get_execution()["resources"] is not None


@pytest.mark.parametrize("interrupt", [False, True])
def test_cleanup_attempts_every_log_pipe_and_pidfd_after_close_failure(
    process_factory: ProcessFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool,
) -> None:
    _spawn, _signals, child = process_factory()
    closed = []
    original_open = Path.open

    def open_log(path: Path, *args: object, **kwargs: object) -> object:

        if path.parent != tmp_path:
            return original_open(path, *args, **kwargs)

        def close() -> None:
            closed.append(path.name)

            if path.name == "stdout.log":
                raise OSError("synthetic log flush failure")

            if interrupt:
                raise KeyboardInterrupt("synthetic close cancellation")

        return SimpleNamespace(write=lambda data: len(data), flush=lambda: None, close=close)

    monkeypatch.setattr(Path, "open", open_log)
    process = ProcessHandle(["external-backend"], log_directory=tmp_path)
    process.start()
    exception = KeyboardInterrupt if interrupt else OSError
    message = "synthetic close cancellation" if interrupt else "synthetic log flush failure"

    with pytest.raises(exception, match=message) as caught:
        process.stop()

    assert closed == ["stdout.log", "stderr.log"] and child.stdout.closed and child.stderr.closed
    process.tree.close.assert_called_once()
    assert not process.get_execution()["cleanup_complete"]
    assert "synthetic log flush failure" in process.get_execution()["cleanup"]["wait_error"]

    if interrupt:
        assert "synthetic log flush failure" in caught.value.__notes__[0]

    outcome = process.wait(timeout=1)
    assert outcome.error == "Process cleanup was not confirmed" and len(closed) == 2


def test_root_only_graceful_stop_leaves_descendants_to_their_owner(process_factory: ProcessFactory) -> None:
    _, signals, _child = process_factory(exit_code=None)
    process = ProcessHandle(["orchestrator"], stop_scope="root", stop_signal=signal.SIGINT)
    process.start()

    # A pidfd graceful signal changes child state just as a real root signal would.
    process.tree.send_signal.side_effect = lambda signum, **kwargs: (
        signals(12345, signum) if signum == signal.SIGINT else None
    )
    process.stop()

    assert process.tree.send_signal.call_args_list[-1].kwargs == {"root_only": True}
    assert signals.call_args_list[0].args == (12345, signal.SIGINT)
    assert process.get_execution()["stop_scope"] == "root"

@pytest.mark.parametrize("phase", ["reap", "reader_join"])
def test_cleanup_cancellation_still_reaps_and_attempts_every_resource(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    _spawn, _signals, child = process_factory()
    process = ProcessHandle(["external-backend"])
    process.start()
    cancellation = KeyboardInterrupt("synthetic cleanup cancellation")

    if phase == "reap":
        reap = processes.os.wait4
        result = reap(child.pid, os.WNOHANG)
        monkeypatch.setattr(processes.os, "wait4", Mock(side_effect=[cancellation, result]))

    else:
        monkeypatch.setattr(processes.Thread, "join", Mock(side_effect=[cancellation, None]))

    with pytest.raises(KeyboardInterrupt, match="synthetic cleanup cancellation"):
        process.stop()

    assert process.get_execution()["resources"] is not None
    assert child.stdout.closed and child.stderr.closed
    process.tree.close.assert_called_once()
    assert not process.get_execution()["cleanup_complete"]


def test_post_signal_cleanup_uses_one_nonrenewable_deadline(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory()
    process = ProcessHandle(["external-backend"])
    process.start()
    process.tree.stop.side_effect = lambda *, grace: processes.sleep(grace)
    monkeypatch.setattr(processes.os, "wait4", Mock(return_value=(0, 0, None)))
    began = processes.monotonic()

    with pytest.raises(NarrowException, match="Could not reap"):
        process.stop(grace=0.1, kill_grace=0.2)

    assert processes.monotonic() - began <= 0.2
    assert process.get_execution()["resources"] is None


def test_bidirectional_records_handle_short_writes_and_backpressure(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn, _signals, child = process_factory(stdout=b'{"error":"application data"}\n{}\n', exit_code=None)
    record = {"op": "run", "value": "\u2603"}
    payload = (json.dumps(record) + "\n").encode("ascii")
    write = Mock(side_effect=[InterruptedError(), BlockingIOError(), 2, len(payload) - 2])
    monkeypatch.setattr(processes.os, "write", write)
    process = ProcessHandle(["external-actor"], input_limit=len(payload))
    process.start()

    try:
        process.write_record(record, timeout=0.1)
        assert process.read_record(timeout=0.1) == {"error": "application data"}
        assert process.read_record(timeout=0.1, limit=2) == {}

    finally:
        process.stop()

    assert [bytes(call.args[1]) for call in write.call_args_list] == [payload, payload, payload, payload[2:]]
    assert spawn.call_args.kwargs["stdin"] == processes.subprocess.PIPE
    processes.os.set_blocking.assert_called_once_with(42, False)
    assert child.stdin.closed and child.stdout.closed and child.stderr.closed


@pytest.mark.parametrize("value", [0, -1, True, "1024"])
def test_invalid_input_limit_is_rejected_before_start(value: object) -> None:

    with pytest.raises(ValueError, match="input_limit must be positive"):
        ProcessHandle(["external-actor"], input_limit=value)


@pytest.mark.parametrize("record,message", [
    ({}, "input record exceeded"), ({"nonfinite": float("nan")}, "Out of range float"),
    (["not an object"], "JSON object"),
])
def test_invalid_input_does_not_write_or_close_the_channel(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch, record: object, message: str,
) -> None:
    _spawn, _signals, child = process_factory(exit_code=None)
    write = Mock()
    monkeypatch.setattr(processes.os, "write", write)
    process = ProcessHandle(["external-actor"], input_limit=2)
    process.start()

    try:

        with pytest.raises(ValueError, match=message):
            process.write_record(record, timeout=1)

        write.assert_not_called()
        assert not child.stdin.closed and process.get_returncode() is None
        assert process.get_execution()["input_error"] is None

    finally:
        process.stop()


def test_input_requires_an_explicitly_enabled_open_pipe(process_factory: ProcessFactory) -> None:
    process_factory()
    process = ProcessHandle(["external-actor"], input_limit=10)

    with pytest.raises(NarrowException, match="input is not open"):
        process.write_record({}, timeout=1)

    process.start()
    process.close_input()
    process.close_input()

    with pytest.raises(NarrowException, match="input is not open"):
        process.write_record({}, timeout=1)

    process.stop()
    disabled = ProcessHandle(["external-actor"])

    with pytest.raises(NarrowException, match="input is not enabled"):
        disabled.write_record({}, timeout=1)


def test_input_deadline_aborts_the_actor_without_real_waits(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _spawn, _signals, child = process_factory(exit_code=None, hangs=True)
    monkeypatch.setattr(processes.os, "write", Mock(side_effect=BlockingIOError))
    process = ProcessHandle(["external-actor"], input_limit=1024, stop_grace=0.02)
    process.start()
    began = processes.monotonic()

    with pytest.raises(TimeoutError, match="input record deadline"):
        process.write_record({"op": "run"}, timeout=0.03)

    assert processes.monotonic() - began <= 0.051
    assert process.get_returncode() == -signal.SIGKILL and child.stdin.closed
    outcome = process.wait(timeout=1)
    assert "input failed" in outcome.error and "TimeoutError" in outcome.execution["input_error"]
    assert outcome.execution["cleanup_complete"]


@pytest.mark.parametrize("failure", [BrokenPipeError("closed input"), KeyboardInterrupt("cancel input")])
def test_partial_input_failure_preserves_the_error_and_closes_every_resource(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    _spawn, _signals, child = process_factory(exit_code=None)
    monkeypatch.setattr(processes.os, "write", Mock(side_effect=[2, failure]))
    process = ProcessHandle(["external-actor"], input_limit=1024)
    process.start()

    with pytest.raises(type(failure), match=f"{failure}") as caught:
        process.write_record({"op": "run"}, timeout=1)

    assert caught.value is failure
    assert child.stdin.closed and child.stdout.closed and child.stderr.closed
    assert process.get_execution()["cleanup_complete"]
    process.tree.close.assert_called_once()


def test_input_failure_is_not_replaced_by_cleanup_failure(
    process_factory: ProcessFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_factory(exit_code=None)
    monkeypatch.setattr(processes.os, "write", Mock(side_effect=BrokenPipeError("closed input")))
    process = ProcessHandle(["external-actor"], input_limit=1024)
    process.start()
    stop = process.stop
    monkeypatch.setattr(process, "stop", Mock(side_effect=OSError("cleanup failure")))

    try:

        with pytest.raises(BrokenPipeError, match="closed input") as caught:
            process.write_record({}, timeout=1)

        assert caught.value.__notes__ == ["Cleanup failed: cleanup failure"]

    finally:
        stop()


@pytest.mark.parametrize("payload", [b'{"long":', b'{"long":123}\n'])
def test_record_limit_covers_complete_and_partial_responses(
    process_factory: ProcessFactory, payload: bytes,
) -> None:
    process_factory(stdout=payload, exit_code=None)
    process = ProcessHandle(["external-actor"])
    process.start()

    try:

        with pytest.raises(NarrowException, match="output record exceeded its size limit"):
            process.read_record(timeout=1, limit=4)

    finally:
        process.stop()


def test_finish_waits_for_natural_eof_exit_with_a_fresh_deadline(process_factory: ProcessFactory) -> None:
    _spawn, _signals, child = process_factory(exit_code=None, exit_at=100.02)
    process = ProcessHandle(["external-actor"], input_limit=1024)
    process.start()
    processes.sleep(100)

    outcome = process.finish(timeout=0.1)

    assert outcome.returncode == 0 and outcome.error is None and outcome.elapsed >= 100.02
    assert outcome.execution["timeout_scope"] == "finish_call"
    assert not outcome.execution["termination_requested"] and not outcome.execution["kill_escalated"]
    assert child.stdin.closed and outcome.execution["cleanup_complete"]


def test_finish_uses_a_separate_bounded_termination_grace(process_factory: ProcessFactory) -> None:
    _spawn, signals, child = process_factory(exit_code=None, hangs=True)
    process = ProcessHandle(["external-actor"], input_limit=1024)
    process.start()

    outcome = process.finish(timeout=0.1, grace=0.03)

    assert outcome.error == "Process timed out" and outcome.returncode == -signal.SIGKILL
    assert processes.monotonic() <= 0.131 and child.stdin.closed
    assert [call.args[1] for call in signals.call_args_list][:2] == [signal.SIGTERM, signal.SIGKILL]


def test_finish_cancellation_while_closing_input_still_reaps_and_closes_the_tree(
    process_factory: ProcessFactory,
) -> None:
    _spawn, _signals, child = process_factory(exit_code=None)
    process = ProcessHandle(["external-actor"], input_limit=1024)
    process.start()
    close = child.stdin.close
    actions = iter([KeyboardInterrupt("cancel EOF"), None, None])

    def close_input() -> None:
        failure = next(actions)

        if failure is not None:
            raise failure

        close()

    child.stdin.close = close_input

    try:

        with pytest.raises(KeyboardInterrupt, match="cancel EOF"):
            process.finish(timeout=1)

        assert child.stdin.closed and child.stdout.closed and child.stderr.closed
        assert process.get_execution()["resources"] is not None
        process.tree.close.assert_called_once()

    finally:
        close()


def test_consumed_records_reuse_buffer_space_but_logs_remain_bounded(
    process_factory: ProcessFactory, tmp_path: Path,
) -> None:
    _spawn, _signals, child = process_factory(stdout=b"{}\n" * 20, stderr=b"")
    process = ProcessHandle(["external-actor"], output_limit=3, log_directory=tmp_path)
    read = child.stdout.read
    received = []
    calls = 0

    def read_chunk(size: int) -> bytes:
        nonlocal calls

        if calls:
            received.append(process.read_record(timeout=1, limit=2, consume=True))

        calls += 1
        return read(3)

    child.stdout.read = read_chunk
    process.start()
    result = process.wait(timeout=1)

    assert received == [{}] * 20 and result.stdout == b""
    assert result.error is None and not result.execution["output_truncated"]
    assert result.execution["stdout_capture_scope"] == "unconsumed_records"
    assert result.execution["logs_truncated"] and (tmp_path / "stdout.log").read_bytes() == b"{}\n"


def test_consuming_a_reply_keeps_the_next_pending_reply(process_factory: ProcessFactory) -> None:
    process_factory(stdout=b'{}\n{"next":1}\n{"tail":2}\n')
    process = ProcessHandle(["external-actor"])
    process.start()

    assert process.read_record(timeout=1) == {}
    assert process.read_record(timeout=1, consume=True) == {"next": 1}
    assert process.collect_output()[0] == b'{"tail":2}\n'
    assert process.read_record(timeout=1, consume=True) == {"tail": 2}
    assert process.wait(timeout=1).stdout == b""


def test_eof_before_a_complete_record_keeps_diagnostics(process_factory: ProcessFactory) -> None:
    process_factory(stdout=b'{"partial":', stderr=b"diagnostic-tail", exit_code=7)
    process = ProcessHandle(["external-actor"])
    process.start()

    try:

        with pytest.raises(NarrowException, match="exited before record: code=7"):
            process.read_record(timeout=1)

        assert process.collect_output() == (b'{"partial":', b"diagnostic-tail")

    finally:
        process.stop()
