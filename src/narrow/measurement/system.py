"""Read-only machine evidence and explicit affinity of caller-owned processes."""

import os
import platform
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from ..processes.tree import read_identity


def _describe_unavailable(error: Exception) -> str:
    reason = error.strerror if isinstance(error, OSError) else f"{error}"
    return f"{type(error).__name__}: {reason or 'observation failed'}"


def read_topology(cpus: Iterable[int], sysfs: Path = Path("/sys/devices/system/cpu")) -> dict[str, dict]:
    """Read physical CPU facts; missing optional facts are not guessed.

    Package and core IDs are required to detect SMT siblings. Frequency and
    governor files may be absent in containers or on platforms without cpufreq.
    The result describes hardware, not exclusive access or equal performance.
    """
    result = {}

    for cpu in cpus:

        if type(cpu) is not int or cpu < 0:
            raise ValueError("CPU identifiers must be nonnegative integers")

        if f"{cpu}" in result:
            continue

        directory = sysfs / f"cpu{cpu}"
        row = {}
        unavailable = {}

        for name, filename in (
            ("package", "topology/physical_package_id"), ("core", "topology/core_id"),
            ("siblings", "topology/thread_siblings_list"), ("max_frequency_khz", "cpufreq/cpuinfo_max_freq"),
            ("governor", "cpufreq/scaling_governor"),
        ):

            try:
                value = (directory / filename).read_text().strip()

                if not value or name in ("package", "core", "max_frequency_khz") and (
                    int(value) < (1 if name == "max_frequency_khz" else 0)
                ):
                    raise ValueError("invalid CPU topology value")

                row[name] = value

            except (OSError, ValueError) as error:

                if name in ("package", "core"):
                    raise ValueError(f"CPU {cpu} topology is unavailable: {name}") from error

                row[name] = None
                unavailable[name] = _describe_unavailable(error)

        row["unavailable"] = unavailable
        result[f"{cpu}"] = row

    return result


def read_thread_affinities(pid: int) -> dict[str, list[int]]:
    """Read each currently visible thread; disappearing threads may be omitted."""

    if type(pid) is not int or pid <= 0:
        raise ValueError("Affinity requires a positive process ID")

    result = {}

    for task in Path(f"/proc/{pid}/task").iterdir():

        try:
            result[task.name] = sorted(os.sched_getaffinity(int(task.name)))

        except (FileNotFoundError, ProcessLookupError):
            pass

    if not result:
        raise ProcessLookupError(f"Process {pid} has no readable live threads")

    return result


def pin_process(pid: int, cpu: int) -> dict[str, list[int]]:
    """Explicitly pin a caller-owned live process, not global machine settings.

    Existing threads are updated in bounded passes; future threads inherit a
    parent's mask. Keep the owning process alive during this operation: Linux
    has no atomic pidfd-based affinity syscall to eliminate every TID race.
    This does not reserve a CPU or prevent the process changing its mask later.
    Failure can leave earlier threads pinned; this is not a rollback transaction.
    """

    if type(pid) is not int or pid <= 0 or type(cpu) is not int or cpu < 0:
        raise ValueError("Affinity requires a positive process ID and nonnegative CPU")

    identity = read_identity(pid)

    if identity is None:
        raise ProcessLookupError(f"Affinity process {pid} is not alive")

    for _attempt in range(4):

        for tid in read_thread_affinities(pid):

            if read_identity(pid) != identity:
                raise ProcessLookupError(f"Affinity process {pid} exited or changed identity")

            try:
                os.sched_setaffinity(int(tid), {cpu})

            except ProcessLookupError:
                pass

        masks = read_thread_affinities(pid)

        if read_identity(pid) != identity:
            raise ProcessLookupError(f"Affinity process {pid} exited or changed identity")

        if all(mask == [cpu] for mask in masks.values()):
            return masks

    raise RuntimeError(f"Process {pid} affinity did not settle on CPU {cpu}")


class HostObservation:
    """Observe named CPU roles without reading or changing the machine at creation."""

    def __init__(self, layout: Mapping[str, int]) -> None:

        if not isinstance(layout, Mapping) or not layout or any(
            not isinstance(role, str) or not role or type(cpu) is not int or cpu < 0
            for role, cpu in layout.items()
        ):
            raise ValueError("CPU roles require nonempty names and nonnegative CPU identifiers")

        self.layout = dict(layout)
        self.hardware: dict[str, dict] | None = None

    def start(self) -> Self:
        """Read required topology explicitly before observing a measurement window."""

        if self.hardware is not None:
            raise RuntimeError("Host observation has already been started")

        self.hardware = read_topology(self.layout.values())
        return self

    def observe(self) -> dict:
        """Read selected CPU activity and frequencies, including unrelated work.

        busy_seconds is cumulative kernel CPU accounting, not process CPU or an
        interference diagnosis by itself. Unreadable counters remain unavailable.
        """

        if self.hardware is None:
            raise RuntimeError("Host observation has not been started")

        cpus = set(self.layout.values())
        busy = dict.fromkeys((f"{cpu}" for cpu in cpus))
        unavailable = {}

        try:
            ticks = os.sysconf("SC_CLK_TCK")

            if ticks <= 0:
                raise ValueError("invalid process CPU tick resolution")

            for line in Path("/proc/stat").read_text().splitlines():
                name, *fields = line.split()

                if not name.startswith("cpu") or not name[3:].isdigit() or int(name[3:]) not in cpus:
                    continue

                try:
                    # guest is already included in user/nice; idle and iowait are not busy.
                    values = [int(fields[index]) for index in (0, 1, 2, 5, 6, 7)]

                    if min(values) < 0:
                        raise ValueError("negative CPU activity counter")

                    busy[name[3:]] = sum(values) / ticks

                except (ValueError, IndexError) as error:
                    unavailable[f"busy_seconds.{name[3:]}"] = _describe_unavailable(error)

        except (OSError, ValueError) as error:
            unavailable["busy_seconds"] = _describe_unavailable(error)

        for cpu, value in busy.items():

            if value is None:
                unavailable.setdefault(f"busy_seconds.{cpu}", "CPU activity was not observed")

        frequency = {}

        for cpu in cpus:

            try:
                value = int(Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq").read_text())

                if value <= 0:
                    raise ValueError("invalid CPU frequency")

                frequency[f"{cpu}"] = value

            except (OSError, ValueError) as error:
                frequency[f"{cpu}"] = None
                unavailable[f"frequency_khz.{cpu}"] = _describe_unavailable(error)

        return {
            "monotonic": time.monotonic(), "busy_seconds": busy, "frequency_khz": frequency,
            "unavailable": unavailable,
        }


def observe_host(*, filesystem: Path | None = None) -> dict:
    """Read raw host evidence, not secrets, exclusive access or benchmark validity."""
    result = {"monotonic": time.monotonic(), "utc": datetime.now(UTC).isoformat(), "unavailable": {}}

    for name, filename in {
        "loadavg": "/proc/loadavg", "cpu_stat": "/proc/stat", "cpu_pressure": "/proc/pressure/cpu",
        "memory_pressure": "/proc/pressure/memory", "io_pressure": "/proc/pressure/io",
    }.items():

        try:
            data = Path(filename).read_text()

            if name == "cpu_stat":
                data = "\n".join(line for line in data.splitlines() if line.startswith("cpu"))

            result[name] = data.strip()

        except OSError as error:
            result[name] = None
            result["unavailable"][name] = _describe_unavailable(error)

    for name, read in (
        ("affinity", lambda: sorted(os.sched_getaffinity(0))), ("cpu_count", os.cpu_count),
        ("proc_cpu_resolution_seconds", lambda: 1 / os.sysconf("SC_CLK_TCK")),
        ("filesystem_available_sz", lambda: os.statvfs(filesystem if filesystem is not None else Path.cwd())),
        ("shm_available_sz", lambda: os.statvfs("/dev/shm")),
    ):

        try:
            value = read()

            if name.endswith("available_sz"):
                value = value.f_bavail * value.f_frsize

            if value is None or name == "proc_cpu_resolution_seconds" and value <= 0:
                raise ValueError("host fact is unavailable")

            result[name] = value

        except (OSError, AttributeError, ValueError, ZeroDivisionError) as error:
            result[name] = None
            result["unavailable"][name] = _describe_unavailable(error)

    return result


def collect_machine(cpu_roles: Mapping[str, int]) -> dict:
    """Collect stable comparison facts, separate from transient host observations.

    Selected CPUs need not belong to the controller's current affinity. Reading
    existing CPU facts proves neither placement permissions nor CPU reservation.
    Missing optional identity facts are explicit; comparison must not equate
    unknown observations merely because both contain None or the same error.
    """

    if not isinstance(cpu_roles, Mapping) or not cpu_roles or any(
        not isinstance(role, str) or not role or type(cpu) is not int or cpu < 0 for role, cpu in cpu_roles.items()
    ):
        raise ValueError("CPU roles require nonempty names and nonnegative CPU identifiers")

    cpus = set(cpu_roles.values())
    hardware = read_topology(sorted(cpus))
    processors = {}
    unavailable = {}

    for block in Path("/proc/cpuinfo").read_text().split("\n\n"):
        fields = {
            name.strip(): value.strip() for line in block.splitlines() if ":" in line
            for name, value in [line.split(":", 1)]
        }
        number = fields.get("processor", "")

        if not number.isdigit() or int(number) not in cpus:
            continue

        if "vendor_id" in fields:
            names = ("vendor_id", "model name", "cpu family", "model", "stepping", "microcode")

        else:
            names = tuple(name for name in (
                "model name", "Processor", "CPU implementer", "CPU architecture", "CPU variant",
                "CPU part", "CPU revision", "isa", "uarch", "mmu",
            ) if name in fields)

        if not names:
            raise ValueError(f"Selected CPU {number} identity is unavailable")

        processors[number] = {name: fields.get(name) or None for name in names}

        for name, value in processors[number].items():

            if value is None:
                unavailable[f"processors.{number}.{name}"] = "CPU identity field was not reported"

    if set(processors) != {f"{cpu}" for cpu in cpus}:
        raise ValueError("Selected CPU identity observations are incomplete")

    governors = {}

    for cpu, row in hardware.items():
        governors[cpu] = row["governor"]

        for name, reason in row["unavailable"].items():
            unavailable[f"topology.{cpu}.{name}"] = reason

    result = {
        "kernel": platform.release(), "architecture": platform.machine(), "cpu_roles": dict(cpu_roles),
        "processors": processors, "topology": hardware, "governors": governors,
        "reservation": "not_exclusive; no host settings changed", "unavailable": unavailable,
    }

    for name, read in (
        ("affinity", lambda: sorted(os.sched_getaffinity(0))), ("clock_ticks", lambda: os.sysconf("SC_CLK_TCK")),
        ("page_size", lambda: os.sysconf("SC_PAGE_SIZE")),
    ):

        try:
            value = read()

            if not value or name != "affinity" and value <= 0:
                raise ValueError("Machine fact is unavailable")

            result[name] = value

        except (OSError, AttributeError, ValueError) as error:
            result[name] = None
            unavailable[name] = _describe_unavailable(error)

    for name in ("kernel", "architecture"):

        if not result[name]:
            unavailable[name] = "Machine fact was not reported"

    return result
