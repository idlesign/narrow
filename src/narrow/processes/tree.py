"""Bounded lineage discovery; pidfds prevent signals from following PID reuse.

Only children of already anchored live parents are inspected. Discovery cannot
prove it saw short-lived daemon forks; callers needing that guarantee must use
an external fresh subreaper or cgroup. No global PID or environment scans.
"""

import math
import os
import select
import signal
import time
from itertools import islice
from pathlib import Path
from typing import Self

MAX_IDENTITIES = 256
MAX_THREADS = 128
OWNED_SIGNALS = (signal.SIGINT, signal.SIGCONT, signal.SIGTERM, signal.SIGKILL)


def validate_duration(value: float) -> float:

    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("finite positive lifecycle duration required")

    return value


def read_start_ticks(pid: int) -> int:
    """Read the kernel start-time fingerprint, failing if the process disappeared."""
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def read_identity(pid: int) -> dict[str, int] | None:
    """Return a live identity; a zombie is not a live owner."""

    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else {"pid": pid, "start_ticks": int(fields[19])}

    except (FileNotFoundError, ProcessLookupError):
        return None


def identity_exists(identity: dict[str, int]) -> bool:
    """Include zombies: exit readiness alone does not confirm reap."""

    try:
        return read_start_ticks(identity["pid"]) == identity["start_ticks"]

    except (FileNotFoundError, ProcessLookupError):
        return False


class PidfdProcess:
    """Identity-checked descriptor used only by the owning process tree."""

    def __init__(self, pid: int, *, start_ticks: int, allowed_signals: tuple[int, ...]) -> None:
        self.pid = pid
        self.start_ticks = start_ticks
        self.allowed_signals = allowed_signals
        self._descriptor: int | None = None
        self._creator = os.getpid()
        self._started = False

    def open(self) -> Self:
        """Open once and recheck identity before retaining the descriptor."""

        if self._started or self._creator != os.getpid():
            raise RuntimeError("Process pidfd cannot be reopened or used after fork")

        self._started = True

        try:
            self._descriptor = os.pidfd_open(self.pid, 0)

            if read_start_ticks(self.pid) != self.start_ticks:
                raise ProcessLookupError("Process identity changed while opening pidfd")

        except BaseException as primary:

            try:
                self.close()

            except BaseException as error:
                primary.add_note(f"Process pidfd cleanup failed: {error}")

            raise

        return self

    def has_exited(self) -> bool:
        """Observe exit readiness without reaping a child or releasing its PID."""
        descriptor = self._require_open()
        observer = select.poll()
        observer.register(descriptor, select.POLLIN)
        flags = 0

        for _descriptor, ready in observer.poll(0):
            flags |= ready

        if flags & (select.POLLNVAL | select.POLLERR):
            raise OSError("Process pidfd observation failed")

        return bool(flags & (select.POLLIN | select.POLLHUP))

    def send_signal(self, signum: int) -> None:
        """Signal the descriptor's process, not a possibly reused numeric PID."""

        if signum not in self.allowed_signals:
            raise ValueError("Signal is not allowed for this process pidfd")

        signal.pidfd_send_signal(self._require_open(), signum, None, 0)

    def _require_open(self) -> int:

        if self._descriptor is None or self._creator != os.getpid():
            raise RuntimeError("Process pidfd is not open or belongs to another process")

        return self._descriptor

    def close(self) -> None:
        """Release once; never retry a descriptor number after a close error."""
        descriptor, self._descriptor = self._descriptor, None

        if descriptor is not None:
            os.close(descriptor)


class ProcessTree:
    """Retain pidfds for proved descendants; never spawn, capture or reap a process."""

    def __init__(self) -> None:
        self.identities = {}
        self.anchors = {}
        self.identity = None
        self.signals = []
        self._closed = False

    def open_root(self, pid: int) -> None:
        """Anchor a child while its executor still owns its unreaped PID."""

        if self.identity is not None or self._closed:
            raise RuntimeError("Process tree root may only be opened once")

        self.identity = {"pid": pid, "start_ticks": read_start_ticks(pid)}
        key = (pid, self.identity["start_ticks"])
        self.anchors[key] = PidfdProcess(
            pid, start_ticks=self.identity["start_ticks"], allowed_signals=OWNED_SIGNALS,
        ).open()
        self.identities[key] = self.identity

    def discover(self) -> None:
        """Anchor descendants only while both their parent and relation are proved."""

        if self._closed:
            return

        parents = list(self.identities.values())
        visited = set()

        while parents:
            parent = parents.pop()
            parent_key = (parent["pid"], parent["start_ticks"])
            anchor = self.anchors.get(parent_key)

            if parent_key in visited or anchor is None or anchor.has_exited():
                continue

            visited.add(parent_key)

            try:

                if not identity_exists(parent):
                    continue

                tasks = list(islice(Path(f"/proc/{parent['pid']}/task").iterdir(), MAX_THREADS + 1))

                if len(tasks) > MAX_THREADS:
                    raise ValueError("owned process thread discovery bound exceeded")

                children = set()

                for task in tasks:

                    try:

                        with (task / "children").open("r", encoding="ascii") as source:
                            payload = source.read(65537)

                    except (FileNotFoundError, ProcessLookupError):
                        continue

                    if len(payload) > 65536:
                        raise ValueError("owned process child listing bound exceeded")

                    children.update(int(value) for value in payload.split())

                    if len(children) > MAX_IDENTITIES or any(pid <= 0 for pid in children):
                        raise ValueError("owned process child discovery bound exceeded")

                for pid in sorted(children):
                    candidate = None

                    try:
                        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                        identity = {"pid": pid, "start_ticks": int(fields[19])}

                        if int(fields[1]) != parent["pid"] or anchor.has_exited() or not identity_exists(parent):
                            continue

                        key = (pid, identity["start_ticks"])

                        if key in self.anchors:
                            parents.append(identity)
                            continue

                        if len(self.identities) >= MAX_IDENTITIES:
                            raise ValueError("owned process identity discovery bound exceeded")

                        candidate = PidfdProcess(
                            pid, start_ticks=identity["start_ticks"], allowed_signals=OWNED_SIGNALS,
                        ).open()
                        confirmed = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()

                        if (int(confirmed[19]) != identity["start_ticks"] or int(confirmed[1]) != parent["pid"]
                                or anchor.has_exited() or not identity_exists(parent)):
                            continue

                        self.identities[key] = identity
                        self.anchors[key] = candidate
                        candidate = None
                        parents.append(identity)

                    except (FileNotFoundError, ProcessLookupError):
                        continue

                    except BaseException as error:

                        if candidate is not None:

                            try:
                                candidate.close()

                            except BaseException as cleanup_error:
                                error.add_note(f"Discarded owned pidfd cleanup failed: {cleanup_error}")

                            candidate = None

                        raise

                    finally:

                        if candidate is not None:
                            candidate.close()

            except (FileNotFoundError, ProcessLookupError):
                continue

    def send_signal(self, signum: int, *, root_only: bool = False) -> None:
        """Signal only retained live pidfds, never an unconfirmed numeric PID."""

        for key, anchor in self.anchors.items():

            if root_only and self.identities[key] != self.identity:
                continue

            try:

                if not anchor.has_exited():
                    anchor.send_signal(signum)
                    self.signals.append({"identity": self.identities[key], "signal": int(signum)})

            except ProcessLookupError:
                pass

    def has_live_descendants(self) -> bool:
        """Observe known descendants even after the root exited or was reparented."""
        return any(
            self.identities[key] != self.identity and not anchor.has_exited()
            for key, anchor in self.anchors.items()
        )

    def get_remaining(self) -> list[dict[str, int]]:
        """Return retained identities that still exist, including unreaped zombies."""
        return [identity for identity in self.identities.values() if identity_exists(identity)]

    def stop(self, *, grace: float = 2.0) -> dict:
        """Kill only known live identities, including escaped children; never reap the root."""
        validate_duration(grace)
        failures = []

        try:
            self.discover()

        except (Exception, KeyboardInterrupt) as error:
            failures.append(error)

        for key, anchor in self.anchors.items():

            for signum in (signal.SIGCONT, signal.SIGKILL):

                try:

                    if not anchor.has_exited():
                        anchor.send_signal(signum)
                        self.signals.append({"identity": self.identities[key], "signal": int(signum)})

                except ProcessLookupError:
                    pass

                except (Exception, KeyboardInterrupt) as error:
                    failures.append(error)

        deadline = time.monotonic() + grace

        while any(not anchor.has_exited() for anchor in self.anchors.values()) and time.monotonic() < deadline:
            time.sleep(0.01)

        if failures:
            primary = next((error for error in failures if isinstance(error, KeyboardInterrupt)), failures[0])

            for failure in failures:

                if failure is not primary:
                    primary.add_note(f"Descendant cleanup failed: {failure}")

            raise primary

        return {
            "signals": self.signals.copy(),
            "remaining_identities_including_zombies": [
                identity for identity in self.identities.values() if identity_exists(identity)
            ],
        }

    def close(self) -> None:
        """Attempt every pidfd close, even if another close fails or is cancelled."""
        if self._closed:
            return

        self._closed = True
        failures = []

        for anchor in self.anchors.values():

            try:
                anchor.close()

            except (Exception, KeyboardInterrupt) as error:
                failures.append(error)

        if failures:
            primary = next((error for error in failures if isinstance(error, KeyboardInterrupt)), failures[0])

            for failure in failures:

                if failure is not primary:
                    primary.add_note(f"Additional pidfd close failure: {failure}")

            raise primary

