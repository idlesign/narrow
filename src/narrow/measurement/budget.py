"""One persisted monotonic deadline shared by explicitly started run invocations."""

import json
import math
import os
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

MAX_RECORD_BYTES = 4096
RECORD_FIELDS = {"boot_id", "monotonic_start", "utc_start", "budget_seconds"}

def read_boot_id() -> str:
    """Identify the Linux boot whose monotonic clock the persisted budget uses."""
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()

    if not value or len(value) > 128:
        raise ValueError("invalid monotonic budget boot identity")

    return value

def _validate_seconds(value: object, *, positive: bool, name: str) -> float:
    valid = type(value) in (int, float)

    try:
        valid = valid and math.isfinite(value) and (value > 0 if positive else value >= 0)

    except OverflowError:
        valid = False

    if not valid:
        requirement = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} must be finite and {requirement}")

    return float(value)

def _collect_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)

    if len(result) != len(pairs):
        raise ValueError("duplicate saved budget field")

    return result

class Budget:
    """Continue spending a fixed same-boot budget, never implicitly resume workloads.

    Construction validates configuration only. open creates or validates one
    immutable record; elapsed time between invocations consumes the same budget.
    A reboot, damaged record or changed limit is an error, not fresh time.
    """

    def __init__(self, path: Path, seconds: float = 900) -> None:
        self.seconds = _validate_seconds(seconds, positive=True, name="budget seconds")
        self.path = Path(path).absolute()
        self.data: dict[str, object] | None = None
        self.deadline: float | None = None
        self._last_observed: float | None = None

    def open(self) -> Self:
        """Create once or reopen the same deadline; never overwrite saved evidence."""

        if self.data is not None:
            raise RuntimeError("budget is already open")

        boot = read_boot_id()
        now = _validate_seconds(time.monotonic(), positive=False, name="monotonic clock")

        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)

        except FileNotFoundError:
            data = {
                "boot_id": boot, "monotonic_start": now,
                "utc_start": datetime.now(UTC).isoformat(), "budget_seconds": self.seconds,
            }
            deadline = _validate_seconds(now + self.seconds, positive=True, name="budget deadline")
            self._create(data)

        else:

            try:
                status = os.fstat(descriptor)

                if not stat.S_ISREG(status.st_mode) or not 0 < status.st_size <= MAX_RECORD_BYTES:
                    raise ValueError("saved budget must be a bounded regular file")

                payload = os.read(descriptor, MAX_RECORD_BYTES + 1)

                if len(payload) != status.st_size:
                    raise ValueError("saved budget changed while reading")

            finally:
                os.close(descriptor)

            try:
                data = json.loads(payload, object_pairs_hook=_collect_pairs)

            except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
                raise ValueError("invalid saved budget JSON") from error

            if type(data) is not dict or set(data) != RECORD_FIELDS:
                raise ValueError("invalid saved budget fields")

            seconds = _validate_seconds(data["budget_seconds"], positive=True, name="saved budget seconds")

            if data["boot_id"] != boot or seconds != self.seconds:
                raise ValueError("cannot replace or restart a saved budget")

            start = _validate_seconds(data["monotonic_start"], positive=False, name="saved monotonic budget start")

            if start > now:
                raise ValueError("saved monotonic budget cannot start in the future")

            timestamp = data["utc_start"]

            if type(timestamp) is not str or not 1 <= len(timestamp) <= 64:
                raise ValueError("saved budget lacks a bounded UTC timestamp")

            try:
                offset = datetime.fromisoformat(timestamp).utcoffset()

            except ValueError as error:
                raise ValueError("saved budget lacks a valid UTC timestamp") from error

            if offset != timedelta(0):
                raise ValueError("saved budget timestamp must use UTC")

            deadline = _validate_seconds(start + seconds, positive=True, name="saved budget deadline")

        self.data = data
        self.deadline = deadline
        self._last_observed = now
        return self

    def _create(self, data: dict[str, object]) -> None:
        # Keep even an incomplete exclusive record on failure. Removing it would
        # allow another invocation to grant a new deadline after cancellation.
        payload = f"{json.dumps(data, indent=2, allow_nan=False)}\n"

        with self.path.open("x", encoding="utf-8") as output:

            if output.write(payload) != len(payload):
                raise OSError("short budget record write")

            output.flush()
            os.fsync(output.fileno())

    def remaining(self) -> float:
        """Return unspent seconds, including a negative value after expiry."""

        if self.deadline is None:
            raise RuntimeError("budget must be opened explicitly")

        now = _validate_seconds(time.monotonic(), positive=False, name="monotonic clock")

        if now < self._last_observed:
            raise ValueError("monotonic budget clock moved backwards")

        self._last_observed = now
        return self.deadline - now

    def require(self, *, reserve: float = 0, minimum: float = 0) -> float:
        """Require new-work time plus cleanup reserve without extending the deadline."""
        reserve = _validate_seconds(reserve, positive=False, name="budget reserve")
        minimum = _validate_seconds(minimum, positive=False, name="budget minimum")
        required = _validate_seconds(reserve + minimum, positive=False, name="combined budget reserve")

        if self.remaining() <= required:
            raise TimeoutError("campaign budget leaves no new work plus cleanup reserve")

        return self.deadline - reserve
