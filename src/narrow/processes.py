"""Bounded foreground processes owned by one narrow invocation (Linux/POSIX)."""

import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Thread
from time import monotonic, sleep
from typing import BinaryIO

from .exceptions import NarrowException


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    elapsed: float
    error: str | None = None


class ProcessHandle:
    """Own a foreground process group; never signal a reaped PID or process name."""

    def __init__(self, argv: Sequence[str], *, output_limit: int = 2 * 1024 * 1024) -> None:

        if not argv or isinstance(argv, str) or any(not isinstance(arg, str) for arg in argv):
            raise ValueError("argv must be a nonempty sequence of strings")

        if output_limit <= 0:
            raise ValueError("output_limit must be positive")

        self.argv = tuple(argv)
        self.output_limit = output_limit
        self._process: subprocess.Popen | None = None
        self._threads: list[Thread] = []
        self._buffers = [bytearray(), bytearray()]
        self._overflow = False
        self._closed = False
        self._began = 0.0
        self._returncode = None

    def start(self) -> None:
        """Start once, draining both pipes even if the child fills them."""

        if self._process is not None or self._closed:
            raise NarrowException("Process handle cannot be started twice")

        if not hasattr(os, "WNOWAIT"):
            raise NarrowException("narrow process ownership requires POSIX waitid/WNOWAIT")

        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise NarrowException("narrow requires the default SIGCHLD disposition and exclusive child reaping")

        self._began = monotonic()

        try:
            self._process = subprocess.Popen(
                self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False, start_new_session=True,
            )

        except OSError as error:
            raise NarrowException(f"Cannot start {self.argv[0]}: {error}") from error

        try:

            for stream, buffer in zip((self._process.stdout, self._process.stderr), self._buffers, strict=True):
                reader = Thread(target=self._read_output, args=(stream, buffer), daemon=True)
                reader.start()
                self._threads.append(reader)

        except BaseException as primary:

            try:
                self.stop()

            except (OSError, NarrowException) as cleanup_error:
                primary.add_note(f"Cleanup failed: {cleanup_error}")

            raise

    def _read_output(self, stream: BinaryIO, buffer: bytearray) -> None:

        with stream:

            while chunk := stream.read(8192):
                remaining = self.output_limit - len(buffer)
                buffer.extend(chunk[:remaining])

                if len(chunk) > remaining:
                    self._overflow = True

    def get_returncode(self) -> int | None:
        """Observe exit without reaping: the leader reserves its process-group ID."""

        if self._closed or self._process is None:
            return self._returncode

        try:
            status = os.waitid(os.P_PID, self._process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)

        except ChildProcessError as error:
            self._closed = True
            raise NarrowException("Child reaped externally; cannot safely signal its former process group") from error

        if status is None:
            return None

        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status

    def collect_output(self) -> tuple[bytes, bytes]:
        """Return captured bytes, including partial output after failure."""
        return bytes(self._buffers[0]), bytes(self._buffers[1])

    @property
    def output_truncated(self) -> bool:
        """Whether either channel exceeded its capture limit."""
        return self._overflow

    def check_running(self) -> None:
        """Reject early exit (including code zero) and excessive output."""

        if self.get_returncode() is not None:
            raise NarrowException(f"{self.argv[0]} exited before the stand was stopped")

        if self._overflow:
            raise NarrowException(f"{self.argv[0]} exceeded the output limit")

    def _signal_group(self, signum: int) -> None:

        try:
            os.killpg(self._process.pid, signum)

        except ProcessLookupError:
            pass

    def stop(self, *, grace: float = 1.0) -> None:
        """Terminate the owned group before reaping its leader; cleanup is idempotent."""

        if self._closed or self._process is None:
            return

        self.get_returncode()
        self._signal_group(signal.SIGTERM)
        deadline = monotonic() + grace

        while self.get_returncode() is None and monotonic() < deadline:
            sleep(0.01)

        # The unreaped leader still reserves this ID, even if it exited early.
        self._signal_group(signal.SIGKILL)

        try:
            self._returncode = self._process.wait(timeout=grace)

        except subprocess.TimeoutExpired as error:
            raise NarrowException(f"Could not reap {self.argv[0]} after SIGKILL") from error

        self._closed = True

        for reader in self._threads:
            reader.join(timeout=grace)

        if any(reader.is_alive() for reader in self._threads):
            raise NarrowException(f"Output pipes of {self.argv[0]} remain open after cleanup")

        self._process.stdout.close()
        self._process.stderr.close()
        deadline = monotonic() + grace

        while True:

            try:
                os.killpg(self._process.pid, 0)

            except ProcessLookupError:
                return

            if monotonic() >= deadline:
                raise NarrowException(f"Cannot confirm cleanup of {self.argv[0]} process group")

            sleep(0.01)

    def wait(self, *, timeout: float) -> CommandResult:
        """Wait within a deadline and retain partial evidence on timeout/overflow."""

        if self._process is None:
            raise NarrowException("Process has not been started")

        error = None

        try:

            while self.get_returncode() is None:

                if self._overflow:
                    error = "Process exceeded the output limit (output truncated)"
                    break

                if monotonic() - self._began >= timeout:
                    error = "Process timed out"
                    break

                sleep(0.01)

        except BaseException as primary:

            try:
                self.stop()

            except (OSError, NarrowException) as cleanup_error:
                primary.add_note(f"Cleanup failed: {cleanup_error}")

            raise

        self.stop()

        if self._overflow:
            error = "Process exceeded the output limit (output truncated)"

        stdout, stderr = self.collect_output()
        return CommandResult(self.argv, self._returncode, stdout, stderr, monotonic() - self._began, error)


def run_command(argv: list[str], *, timeout: float = 5.0) -> str:
    """Run a short metadata command, returning text without mixing its channels."""
    process = ProcessHandle(argv)
    process.start()
    result = process.wait(timeout=timeout)

    if result.error or result.returncode:
        reason = result.error or f"exit {result.returncode}"
        raise NarrowException(f"{argv[0]} failed: {reason}")

    return (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
