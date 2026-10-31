"""Child-only Linux resource guards, independent of a particular load generator."""

import json
import os
import resource
import sys
from collections.abc import Sequence
from dataclasses import dataclass

REPORT_LIMIT = 64 * 1024
CHILD_CODE = (
    "import json, sys; from narrow.processes.limits import execute_command; "
    "execute_command(sys.argv[2:], **json.loads(sys.argv[1]))"
)


@dataclass(frozen=True)
class ProcessLimits:
    """Initial nonexclusive CPU affinity and per-process virtual memory, not RSS.

    None preserves the inherited mask or address-space limit. Core dumps are
    disabled in the child. Construction does not inspect or change host settings.
    """

    cpus: tuple[int, ...] | None = None
    address_space_bytes: int | None = None
    file_size_bytes: int | None = None

    def __post_init__(self) -> None:

        if self.cpus is not None:

            if (
                not isinstance(self.cpus, (tuple, list)) or not self.cpus
                or any(type(cpu) is not int or not 0 <= cpu <= 2**31 - 1 for cpu in self.cpus)
                or len(set(self.cpus)) != len(self.cpus)
            ):
                raise ValueError("cpus must contain distinct nonnegative CPU integers")

            object.__setattr__(self, "cpus", tuple(sorted(self.cpus)))

        for name in ("address_space_bytes", "file_size_bytes"):
            value = getattr(self, name)

            if value is not None and (type(value) is not int or not 0 < value <= 2**63 - 1):
                raise ValueError(f"{name} must be a positive signed 64-bit integer")

    def serialize(self) -> dict:
        """Return requested settings, not a claim that the OS has applied them."""
        return {
            "cpus": list(self.cpus) if self.cpus is not None else None,
            "address_space_bytes": self.address_space_bytes,
            "file_size_bytes": self.file_size_bytes,
        }

    def build_argv(self, argv: Sequence[str], *, report_fd: int) -> list[str]:
        """Select an isolated installed launcher without changing the caller's limits."""
        options = {"limits": self.serialize(), "report_fd": report_fd}
        return [sys.executable, "-I", "-c", CHILD_CODE, json.dumps(options), *argv]

    def apply_in_child(self) -> dict:
        """Apply and verify guards in a fresh child, before any tool threads or exec.

        Call only inside a launcher, never in the controller process. The returned
        mask describes initial placement, not continuous enforcement/reservation.
        An explicit mask may differ from the controller's inherited affinity;
        kernel cpuset/permission restrictions still apply. File size limits apply to each regular file, not pipes or total disk usage.
        """
        guards = [(resource.RLIMIT_CORE, 0, "core dump")]

        for kind, value, name in (
            (resource.RLIMIT_AS, self.address_space_bytes, "address space"),
            (resource.RLIMIT_FSIZE, self.file_size_bytes, "file size"),
        ):

            if value is not None:
                guards.append((kind, value, name))

        for kind, value, name in guards:
            _soft, hard = resource.getrlimit(kind)

            if hard != resource.RLIM_INFINITY and value > hard:
                raise ValueError(f"Requested {name} exceeds the inherited hard limit")

        for kind, value, _name in guards:
            resource.setrlimit(kind, (value, value))

            if resource.getrlimit(kind) != (value, value):
                raise ValueError("Process limits were not applied as requested")

        if self.cpus is not None:
            # A controller may be pinned to a different CPU. Let the kernel enforce
            # cpuset/permission restrictions, then reject any silently reduced mask.
            os.sched_setaffinity(0, self.cpus)

        actual_cpus = sorted(os.sched_getaffinity(0))

        if self.cpus is not None and actual_cpus != list(self.cpus):
            raise ValueError("Process CPU mask was not applied as requested")

        applied = {"cpus": actual_cpus, "core_dump_bytes": 0}

        for kind, name in ((resource.RLIMIT_AS, "address_space"), (resource.RLIMIT_FSIZE, "file_size")):
            soft, hard = resource.getrlimit(kind)
            applied[f"{name}_bytes"] = None if soft == resource.RLIM_INFINITY else soft
            applied[f"{name}_hard_bytes"] = None if hard == resource.RLIM_INFINITY else hard

        return applied


def write_report(descriptor: int, data: dict) -> None:
    """Replace bounded child metadata in an inherited, otherwise private file."""
    payload = json.dumps(data, allow_nan=False).encode()

    if len(payload) > REPORT_LIMIT:
        raise ValueError("Process limits report exceeded its size limit")

    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)

    while payload:
        written = os.write(descriptor, payload)

        if written == 0:
            raise OSError("Could not write process limits report")

        payload = payload[written:]


def execute_command(argv: Sequence[str], *, limits: dict, report_fd: int) -> None:
    """Child entry: verify guards, report them, then exec without leaking the report fd.

    This function replaces its interpreter; invoke it only through ProcessHandle.
    The parent never uses preexec_fn or changes its own affinity/resource limits.
    """
    os.set_inheritable(report_fd, False)
    applied = None

    try:
        settings = ProcessLimits(**limits)
        applied = settings.apply_in_child()
        write_report(report_fd, {"applied": applied, "error": None})
        os.execvpe(argv[0], list(argv), os.environ)

    except (OSError, ValueError, OverflowError) as error:
        write_report(report_fd, {"applied": applied, "error": f"{error}"})
        raise SystemExit("narrow could not apply process limits or execute the command") from None
