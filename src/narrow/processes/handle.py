"""One bounded executor for stands, load generators and external benchmark actors."""

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryFile
from threading import Lock, Thread
from time import monotonic, sleep
from typing import BinaryIO

from ..exceptions import NarrowException
from .limits import REPORT_LIMIT, ProcessLimits
from .tree import ProcessTree, validate_duration

type ProcessLauncher = Callable[[tuple[str, ...], ProcessLimits, int], Sequence[str]]


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    elapsed: float
    error: str | None = None
    execution: dict = field(default_factory=dict)


class ProcessHandle:
    """Own a process group and proved descendants; never signal a reaped group ID.

    Construction has no external effects. Start explicitly; wait/stop reap the
    immediate child and release output/log/pidfd resources, also on cancellation.
    Escaped descendants are tracked by bounded lineage discovery, not global scans.
    """

    def __init__(
        self, argv: Sequence[str], *, output_limit: int = 2 * 1024 * 1024,
        cwd: Path | None = None, env: Mapping[str, str] | None = None,
        limits: ProcessLimits | None = None, launcher: ProcessLauncher | None = None,
        stop_signal: int = signal.SIGTERM, stop_grace: float = 1.0, poll_interval: float = 0.01,
        log_directory: Path | None = None, stop_scope: str = "group", input_limit: int | None = None,
    ) -> None:

        if not argv or isinstance(argv, str) or any(not isinstance(arg, str) for arg in argv):
            raise ValueError("argv must be a nonempty sequence of strings")

        if type(output_limit) is not int or output_limit <= 0:
            raise ValueError("output_limit must be positive")

        if input_limit is not None and (type(input_limit) is not int or input_limit <= 0):
            raise ValueError("input_limit must be positive or None")

        if limits is not None and not isinstance(limits, ProcessLimits):
            raise ValueError("limits must be a ProcessLimits instance")

        if launcher is not None and (not callable(launcher) or limits is None):
            raise ValueError("launcher must be callable and requires explicit ProcessLimits")

        if stop_signal not in (signal.SIGINT, signal.SIGTERM):
            raise ValueError("stop_signal must be SIGINT or SIGTERM")

        if stop_scope not in ("group", "root"):
            raise ValueError("stop_scope must be group or root")

        validate_duration(stop_grace)
        validate_duration(poll_interval)
        self.argv = tuple(argv)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.output_limit = output_limit
        self.input_limit = input_limit
        self.limits = limits
        self.launcher = launcher
        self.stop_signal = stop_signal
        self.stop_scope = stop_scope
        self.stop_grace = stop_grace
        self.poll_interval = poll_interval
        self.log_directory = log_directory
        self.tree = ProcessTree()
        self.stdout_offset = 0
        self.executed_argv = self.argv
        self._process: subprocess.Popen | None = None
        self._threads: list[Thread] = []
        self._output_lock = Lock()
        self._buffers = [bytearray(), bytearray()]
        self._logs: list[BinaryIO] = []
        self._logged = [0, 0]
        self._logs_truncated = False
        self._consumes_records = False
        self._read_errors: list[BaseException] = []
        self._overflow = False
        self._closed = False
        self._began = 0.0
        self._returncode = None
        self._limit_report: BinaryIO | None = None
        self._applied_limits: dict | None = None
        self._limit_error = "Process has not confirmed its limits" if limits is not None else None
        self._resources: dict | None = None
        self._timeout: float | None = None
        self._timeout_scope: str | None = None
        self._input_error: str | None = None
        self._termination_requested = False
        self._kill_escalated = False
        self._cleanup_complete = False
        self._cleanup: dict | None = None

    def start(self) -> None:
        """Start once with unbuffered pipes; readers retain and log bounded output."""

        if self._process is not None or self._closed or self._cleanup is not None:
            raise NarrowException("Process handle cannot be started twice")

        if sys.platform != "linux" or not hasattr(os, "WNOWAIT") or not hasattr(os, "wait4"):
            raise NarrowException("narrow process ownership and accounting require Linux waitid/WNOWAIT and wait4")

        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise NarrowException("narrow requires the default SIGCHLD disposition and exclusive child reaping")

        self._began = monotonic()

        try:

            if self.log_directory is not None:

                for name in ("stdout", "stderr"):
                    self._logs.append((self.log_directory / f"{name}.log").open("xb"))

            launch_options = {}

            if self.limits is not None:
                self._limit_report = TemporaryFile(mode="w+b")
                descriptor = self._limit_report.fileno()
                self.executed_argv = tuple(
                    self.launcher(self.argv, self.limits, descriptor) if self.launcher is not None
                    else self.limits.build_argv(self.argv, report_fd=descriptor)
                )
                launch_options["pass_fds"] = (descriptor,)

            try:
                self._process = subprocess.Popen(
                    self.executed_argv, stdin=subprocess.PIPE if self.input_limit is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, bufsize=0, shell=False, start_new_session=True,
                    cwd=self.cwd, env=self.env, **launch_options,
                )

            except OSError as error:
                raise NarrowException(f"Cannot start {self.argv[0]}: {error}") from error

            self.tree.open_root(self.pid)

            if self.input_limit is not None:
                os.set_blocking(self._process.stdin.fileno(), False)

            for index, stream in enumerate((self._process.stdout, self._process.stderr)):
                reader = Thread(target=self._read_output, args=(stream, index), daemon=True)
                reader.start()
                self._threads.append(reader)

            self.tree.discover()

        except BaseException as primary:

            try:
                self.stop(grace=0.2)

            except BaseException as cleanup_error:
                primary.add_note(f"Cleanup failed: {cleanup_error}")

            raise

    def _read_output(self, stream: BinaryIO, index: int) -> None:
        buffer = self._buffers[index]
        log = self._logs[index] if self._logs else None

        try:

            with stream:

                while chunk := stream.read(8192):

                    with self._output_lock:
                        remaining = self.output_limit - len(buffer)
                        buffer.extend(chunk[:remaining])

                        if len(chunk) > remaining:
                            self._overflow = True

                    if log is not None:
                        retained = chunk[:self.output_limit - self._logged[index]]

                        if log.write(retained) != len(retained):
                            raise OSError("Short process log write")

                        self._logged[index] += len(retained)
                        log.flush()

                        if len(retained) < len(chunk):
                            self._logs_truncated = True

        except BaseException as error:
            self._read_errors.append(error)

    def get_returncode(self) -> int | None:
        """Observe exit without reaping: the leader reserves its process-group ID."""

        if self._closed or self._process is None:
            return self._returncode

        try:
            status = os.waitid(os.P_PID, self._process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)

        except ChildProcessError as error:
            self._closed = True
            self._collect_limits()
            raise NarrowException("Child reaped externally; cannot safely signal its former process group") from error

        if status is None:
            return None

        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status

    def collect_output(self) -> tuple[bytes, bytes]:
        """Return retained bytes; explicitly consumed stdout records are excluded."""

        with self._output_lock:
            return bytes(self._buffers[0]), bytes(self._buffers[1])

    @property
    def pid(self) -> int | None:
        """Return the recorded child PID without polling or reaping it."""
        return None if self._process is None else self._process.pid

    def poll_output(self, timeout: float = 0) -> None:
        """Check asynchronous capture errors and overflow while a stand is running."""

        if self._read_errors:
            raise self._read_errors[0]

        if self._overflow:
            raise NarrowException("Process exceeded the output limit (output truncated)")

        if timeout:
            sleep(timeout)

    def write_record(self, record: dict, *, timeout: float) -> None:
        """Send one JSON line within input_limit and a deadline, without buffering.

        Calls must be serialized by the controller. Failed or cancelled writes
        stop the process: a partial record must never be followed by a new one.
        Validation errors happen before any bytes are written.
        """
        deadline = monotonic() + validate_duration(timeout)

        if type(record) is not dict:
            raise ValueError("Process record must be a JSON object")

        payload = (json.dumps(record, allow_nan=False) + "\n").encode("ascii")

        if self.input_limit is None:
            raise NarrowException("Process input is not enabled")

        if len(payload) > self.input_limit:
            raise ValueError("Process input record exceeded its size limit")

        if self._process is None or self._closed or self._process.stdin.closed:
            raise NarrowException("Process input is not open")

        pending = memoryview(payload)

        try:

            while pending:
                self.check_running()

                if monotonic() >= deadline:
                    raise TimeoutError("Process input record deadline exceeded")

                try:
                    written = os.write(self._process.stdin.fileno(), pending)

                except (BlockingIOError, InterruptedError):
                    sleep(min(self.poll_interval, max(0, deadline - monotonic())))
                    continue

                if written <= 0:
                    raise NarrowException("Process input write made no progress")

                pending = pending[written:]

        except BaseException as primary:
            self._input_error = f"{type(primary).__name__}: {primary}"

            try:
                self.stop()

            except BaseException as cleanup_error:
                primary.add_note(f"Cleanup failed: {cleanup_error}")

            raise

    def close_input(self) -> None:
        """Send EOF without waiting for exit; closing an already closed pipe is safe."""

        if self._process is not None and self.input_limit is not None:
            self._process.stdin.close()

    def read_record(self, *, timeout: float, limit: int | None = None, consume: bool = False) -> dict:
        """Read one JSON line, optionally consuming it to bound long conversations.

        By default records remain in capture for protocol audits. consume frees
        their buffer space; logs still retain only a bounded lifetime prefix.
        Reads and writes must be serialized by the controller.
        """
        deadline = monotonic() + validate_duration(timeout)
        limit = self.output_limit if limit is None else limit

        if type(limit) is not int or limit <= 0:
            raise ValueError("record limit must be positive")

        while True:
            self.poll_output()

            with self._output_lock:
                buffer = self._buffers[0]
                end = buffer.find(b"\n", self.stdout_offset)
                size = (len(buffer) if end == -1 else end) - self.stdout_offset

                if size > limit:
                    raise NarrowException("Process output record exceeded its size limit")

                if end != -1:
                    line = bytes(buffer[self.stdout_offset:end])
                    self.stdout_offset = end + 1

            if end != -1:
                record = json.loads(line)

                if type(record) is not dict:
                    raise ValueError("Process record must be a JSON object")

                if consume:

                    with self._output_lock:
                        del buffer[:self.stdout_offset]
                        self.stdout_offset = 0
                        self._consumes_records = True

                return record

            if self.get_returncode() is not None and not any(reader.is_alive() for reader in self._threads):
                raise NarrowException(f"Process exited before record: code={self.get_returncode()}")

            if monotonic() >= deadline:
                raise TimeoutError("Process readiness/record deadline exceeded")

            self.tree.discover()
            sleep(min(self.poll_interval, max(0, deadline - monotonic())))

    def _collect_limits(self) -> None:

        if self._limit_report is None:
            return

        stream, self._limit_report = self._limit_report, None

        with stream:

            try:
                stream.seek(0)
                raw = stream.read(REPORT_LIMIT + 1)

                if len(raw) > REPORT_LIMIT:
                    raise ValueError("Process limits report exceeded its size limit")

                report = json.loads(raw)

                if (
                    not isinstance(report, dict) or set(report) != {"applied", "error"}
                    or report["error"] is not None and not isinstance(report["error"], str)
                    or report["applied"] is None and not report["error"]
                ):
                    raise ValueError("Malformed process limits report")

                applied = report["applied"]

                if applied is not None:

                    if (
                        not isinstance(applied, dict) or set(applied) != {
                            "cpus", "core_dump_bytes", "address_space_bytes", "address_space_hard_bytes",
                            "file_size_bytes", "file_size_hard_bytes",
                        } or type(applied["core_dump_bytes"]) is not int or applied["core_dump_bytes"] != 0
                        or applied["cpus"] is None
                    ):
                        raise ValueError("Malformed applied process limits")

                    actual = ProcessLimits(
                        cpus=applied["cpus"], address_space_bytes=applied["address_space_bytes"],
                        file_size_bytes=applied["file_size_bytes"],
                    )
                    ProcessLimits(
                        address_space_bytes=applied["address_space_hard_bytes"], file_size_bytes=applied["file_size_hard_bytes"],
                    )

                    if (
                        self.limits.cpus is not None and actual.cpus != self.limits.cpus
                        or self.limits.address_space_bytes is not None and (
                            actual.address_space_bytes != self.limits.address_space_bytes
                            or applied["address_space_hard_bytes"] != self.limits.address_space_bytes
                        )
                        or self.limits.file_size_bytes is not None and (
                            actual.file_size_bytes != self.limits.file_size_bytes
                            or applied["file_size_hard_bytes"] != self.limits.file_size_bytes
                        )
                    ):
                        raise ValueError("Confirmed limits differ from requested limits")

                self._applied_limits = applied
                self._limit_error = report["error"]

            except (OSError, ValueError, UnicodeError, RecursionError):
                self._limit_error = "Child did not return a complete process limits report"

    def get_execution(self) -> dict:
        """Describe confirmed limits and lifetime accounting, also after cancellation.

        wait4 covers the reaped process and its waited children, not unjoined
        descendants. CPU and peak RSS include launcher/startup/drain/reporting,
        not only the generator's HTTP window; peak RSS is not a tree RSS sum.
        """
        return {
            "limits": {
                "requested": self.limits.serialize(), "applied": deepcopy(self._applied_limits),
                "error": self._limit_error,
                "address_space_scope": "per_process_virtual_address_space_not_RSS",
                "cpu_scope": "initial_affinity_inherited_by_threads_not_exclusive_reservation",
                "file_size_scope": "per_regular_file_not_pipes_or_total_disk_usage",
            } if self.limits is not None else None,
            "resources": dict(self._resources) if self._resources is not None else None,
            "timeout_seconds": self._timeout, "timeout_scope": self._timeout_scope,
            "input_limit_per_record_bytes": self.input_limit, "input_error": self._input_error,
            "output_limit_per_stream_bytes": self.output_limit,
            "stdout_capture_scope": "unconsumed_records" if self._consumes_records else "process_lifetime",
            "logs_truncated": self._logs_truncated,
            "output_truncated": self.output_truncated,
            "termination_requested": self._termination_requested, "kill_escalated": self._kill_escalated,
            "stop_signal": int(self.stop_signal), "stop_scope": self.stop_scope, "stop_grace_seconds": self.stop_grace,
            "poll_interval_seconds": self.poll_interval, "cleanup_complete": self._cleanup_complete,
            "cleanup": deepcopy(self._cleanup),
        }

    @property
    def output_truncated(self) -> bool:
        """Whether either channel exceeded its capture limit."""
        return self._overflow

    def check_running(self) -> None:
        """Reject early exit (including code zero) and excessive output."""
        self.poll_output()

        if self.get_returncode() is not None:
            raise NarrowException(f"{self.argv[0]} exited before the stand was stopped")

        self.tree.discover()

    def _signal_group(self, signum: int) -> None:

        if self._closed or self._process is None:
            return

        # Verify exclusive ownership before every group signal, never after reap.
        self.get_returncode()

        try:
            os.killpg(self.pid, signum)

        except ProcessLookupError:
            pass

    def stop(self, *, grace: float | None = None, kill_grace: float | None = None) -> dict:
        """Stop, kill survivors, reap and close every resource, preserving failures.

        The group leader is not reaped until after the last group signal. pidfds
        also cover discovered descendants which changed sessions or were reparented.
        A root reap alone is not evidence that its descendants were reaped.
        """
        grace = self.stop_grace if grace is None else validate_duration(grace)
        kill_grace = min(grace, 2.0) if kill_grace is None else validate_duration(kill_grace)

        if self._cleanup is not None:
            return deepcopy(self._cleanup)

        failures = []

        try:
            self.close_input()

        except BaseException as error:
            failures.append(error)

        try:

            if self._process is not None and not self._closed:
                self._termination_requested = self.get_returncode() is None
                self.tree.send_signal(signal.SIGCONT)

                if self.stop_scope == "root" and self.tree.identity is not None:
                    self.tree.send_signal(self.stop_signal, root_only=True)

                else:
                    self._signal_group(self.stop_signal)

                deadline = monotonic() + grace

                while self.get_returncode() is None and monotonic() < deadline:
                    self.tree.discover()
                    sleep(min(self.poll_interval, max(0, deadline - monotonic())))

        except BaseException as error:
            failures.append(error)

        kill_deadline = monotonic() + kill_grace

        try:

            if self._process is not None and not self._closed:
                self._kill_escalated = self.get_returncode() is None or self.tree.has_live_descendants()

        except BaseException as error:
            failures.append(error)

        try:
            self._signal_group(signal.SIGKILL)

        except BaseException as error:
            failures.append(error)

        try:
            self.tree.stop(grace=max(0.001, kill_deadline - monotonic()))

        except BaseException as error:
            failures.append(error)

        try:

            if self._process is not None and not self._closed:
                deadline = kill_deadline

                while True:

                    try:
                        pid, status, usage = os.wait4(self.pid, os.WNOHANG)

                    except (InterruptedError, KeyboardInterrupt) as error:
                        failures.append(error)

                        if monotonic() >= deadline:
                            raise NarrowException(f"Could not reap {self.argv[0]} after SIGKILL") from error

                        continue

                    if pid:
                        self._closed = True
                        self._returncode = os.waitstatus_to_exitcode(status)
                        self._process.returncode = self._returncode
                        self._resources = {
                            "user_seconds": usage.ru_utime, "system_seconds": usage.ru_stime,
                            "max_rss_bytes": usage.ru_maxrss * 1024,
                            "scope": "reaped_process_and_waited_children_entire_lifetime_including_launcher",
                            "measured_window_only": False,
                        }
                        break

                    if monotonic() >= deadline:
                        raise NarrowException(f"Could not reap {self.argv[0]} after SIGKILL")

                    sleep(min(self.poll_interval, max(0, deadline - monotonic())))

        except BaseException as error:
            failures.append(error)

        try:
            self._collect_limits()

        except BaseException as error:
            failures.append(error)

        deadline = kill_deadline

        for reader in self._threads:

            try:
                reader.join(timeout=max(0, deadline - monotonic()))

            except BaseException as error:
                failures.append(error)

        readers_alive = any(reader.is_alive() for reader in self._threads)

        if readers_alive:
            failures.append(NarrowException(f"Output pipes of {self.argv[0]} remain open after cleanup"))

        failures.extend(self._read_errors)
        resources = [self.tree, *self._logs]

        if self._process is not None and self.input_limit is not None:
            resources.append(self._process.stdin)

        if self._process is not None and not readers_alive:
            resources.extend((self._process.stdout, self._process.stderr))

        for stream in resources:

            try:
                stream.close()

            except BaseException as error:
                failures.append(error)

        remaining = []

        try:
            remaining = self.tree.get_remaining()

            if self._resources is not None:
                deadline = kill_deadline

                while True:

                    try:
                        # Observation only: group signals are forbidden after reap.
                        os.killpg(self.pid, 0)

                    except ProcessLookupError:
                        break

                    if monotonic() >= deadline:
                        raise NarrowException(f"Cannot confirm cleanup of {self.argv[0]} process group")

                    sleep(min(self.poll_interval, max(0, deadline - monotonic())))

        except BaseException as error:
            failures.append(error)

        self._cleanup_complete = not failures and not remaining and (self._process is None or self._resources is not None)
        self._cleanup = {
            "root_reaped": self._resources is not None, "forced": self._kill_escalated,
            "signals": self.tree.signals.copy(), "remaining_identities_including_zombies": remaining,
            "wait_error": "; ".join(f"{type(error).__name__}: {error}" for error in failures) or None,
        }

        if failures:
            primary = next((error for error in failures if isinstance(error, KeyboardInterrupt)), failures[0])

            for failure in failures:

                if failure is not primary:
                    primary.add_note(f"Additional process cleanup failure: {failure}")

            raise primary

        if not self._cleanup_complete:
            raise NarrowException(f"Cannot confirm cleanup of {self.argv[0]} descendants")

        return deepcopy(self._cleanup)

    def wait(self, *, timeout: float, observe: Callable[[], None] | None = None) -> CommandResult:
        """Wait within a deadline; observation failures trigger cleanup and propagate.

        Callbacks may inspect resources but must never reap or signal the child.
        Timing includes process startup, not only the workload window.
        """
        return self._wait(timeout=timeout, observe=observe, after_eof=False, grace=None)

    def finish(self, *, timeout: float, grace: float | None = None) -> CommandResult:
        """Close stdin and wait for natural exit, then use the existing bounded stop.

        Unlike wait, the exit timeout starts at this call, not at process startup.
        grace bounds SIGTERM handling after that timeout; total elapsed still
        includes the full process lifetime. Check error and returncode as usual.
        """
        return self._wait(timeout=timeout, observe=None, after_eof=True, grace=grace)

    def _wait(
        self, *, timeout: float, observe: Callable[[], None] | None, after_eof: bool, grace: float | None,
    ) -> CommandResult:
        validate_duration(timeout)

        if grace is not None:
            validate_duration(grace)

        if self._process is None:
            raise NarrowException("Process has not been started")

        began = monotonic() if after_eof else self._began
        self._timeout = timeout
        self._timeout_scope = "finish_call" if after_eof else "process_lifetime"
        error = None

        try:

            if after_eof:
                self.close_input()

            while True:
                self.tree.discover()

                if observe is not None:
                    observe()

                if self._read_errors:
                    raise self._read_errors[0]

                if self.get_returncode() is not None:
                    break

                if self._overflow:
                    error = "Process exceeded the output limit (output truncated)"
                    break

                if monotonic() - began >= timeout:
                    error = "Process timed out"
                    break

                sleep(min(self.poll_interval, max(0, timeout - (monotonic() - began))))

        except BaseException as primary:

            try:
                self.stop(grace=grace)

            except BaseException as cleanup_error:
                primary.add_note(f"Cleanup failed: {cleanup_error}")

            raise

        self.stop(grace=grace)

        if self._overflow:
            error = "Process exceeded the output limit (output truncated)"

        if error is None and self._input_error is not None:
            error = f"Process input failed: {self._input_error}"

        if error is None and not self._cleanup_complete:
            error = "Process cleanup was not confirmed"

        if error is None and self._limit_error is not None:
            error = f"Process limits setup failed: {self._limit_error}"

        stdout, stderr = self.collect_output()
        return CommandResult(
            self.argv, self._returncode, stdout, stderr, monotonic() - self._began, error, self.get_execution(),
        )


def run_command(argv: list[str], *, timeout: float = 5.0) -> str:
    """Run a short metadata command, returning text without mixing its channels."""
    process = ProcessHandle(argv)
    process.start()
    result = process.wait(timeout=timeout)

    if result.error or result.returncode:
        reason = result.error or f"exit {result.returncode}"
        raise NarrowException(f"{argv[0]} failed: {reason}")

    return (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
