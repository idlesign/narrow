"""Linux CPU and sampled resource peaks, not kernel high-water or tree totals."""

import ctypes
import math
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from .tree import read_identity


def read_cpu_seconds(pid: int) -> float:
    """Read coarse user + system process CPU, excluding its waited children."""

    if type(pid) is not int or pid <= 0:
        raise ValueError("CPU observation requires a positive process ID")

    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    counters = (int(fields[11]), int(fields[12]))
    ticks = os.sysconf("SC_CLK_TCK")

    if min(counters) < 0 or ticks <= 0:
        raise ValueError("Invalid process CPU counter or tick resolution")

    return sum(counters) / ticks


def observe_processes(pids: Mapping[str, int]) -> dict:
    """A disappearing process/thread is reported, not silently assigned zero CPU."""
    result = {"monotonic": time.monotonic(), "processes": {}, "unavailable": []}
    ticks = os.sysconf("SC_CLK_TCK")

    for role, pid in pids.items():
        try:
            identity = read_identity(pid)

            if identity is None:
                raise ProcessLookupError("process identity not observed live")

            cpu = read_cpu_seconds(pid)
            status = Path(f"/proc/{pid}/status").read_text().splitlines()
            resources = {
                "rss_kib": int(next(line.split()[1] for line in status if line.startswith("VmRSS:"))),
                "threads": len(list(Path(f"/proc/{pid}/task").iterdir())),
                "fds": len(list(Path(f"/proc/{pid}/fd").iterdir())),
            }
            threads = {}

            for task in Path(f"/proc/{pid}/task").iterdir():
                try:
                    fields = (task / "stat").read_text().rsplit(")", 1)[1].split()
                    threads[task.name] = {
                        "start_ticks": int(fields[19]),
                        "cpu_seconds": (int(fields[11]) + int(fields[12])) / ticks,
                        "name": (task / "comm").read_text().strip(),
                        "affinity": sorted(os.sched_getaffinity(int(task.name))),
                    }

                except (FileNotFoundError, ProcessLookupError):
                    pass

            if read_identity(pid) != identity:
                raise ProcessLookupError("process identity changed during observation")
            result["processes"][role] = {
                "identity": identity,
                "cpu_seconds": cpu,
                "resources": resources,
                "threads": threads,
            }

        except (OSError, StopIteration, ValueError, IndexError) as error:
            result["unavailable"].append(role)
            result.setdefault("unavailable_reasons", {})[role] = f"{type(error).__name__}: {error}"
    return result


def calculate_cpu_delta(before: dict, after: dict) -> dict:
    """Keep missing/replaced processes and threads unavailable, never zero CPU."""
    process_delta, thread_delta, missing = {}, {}, []

    for role, row in before["processes"].items():
        end = after["processes"].get(role)

        if end is None or end["identity"] != row["identity"]:
            process_delta[role] = None
            missing.append(role)
            continue
        process_delta[role] = end["cpu_seconds"] - row["cpu_seconds"]
        thread_delta[role] = {}

        for tid, thread in row["threads"].items():
            last = end["threads"].get(tid)
            thread_delta[role][tid] = (
                None

                if last is None or last["start_ticks"] != thread["start_ticks"]
                else last["cpu_seconds"] - thread["cpu_seconds"]
            )
    return {
        "process_seconds": process_delta,
        "thread_seconds": thread_delta,
        "missing_or_replaced_processes": missing,
        "new_process_roles": sorted(set(after["processes"]) - set(before["processes"])),
        "new_threads": {
            role: sorted(
                set(end["threads"]) - set(before["processes"].get(role, {}).get("threads", {}))
            )

            for role, end in after["processes"].items()
        },
        "proc_resolution_seconds": 1 / os.sysconf("SC_CLK_TCK"),
    }


class ResourceObserver:
    """Accumulate sampled peaks separately for each role and process identity."""

    def __init__(self) -> None:
        self.peaks = {}
        self.samples = 0
        self.unavailable = {}

    def sample(self, pids: Mapping[str, int]) -> dict:
        """Observe supplied roles once, remembering missing samples and thread identities."""
        row = observe_processes(pids)
        self.samples += 1

        for role in row["unavailable"]:

            if role not in self.unavailable:
                self.unavailable[role] = {
                    "count": 0,
                    "first_reason": row.get("unavailable_reasons", {}).get(
                        role, "process identity not observed live"
                    ),
                }
            self.unavailable[role]["count"] += 1

        for role, process in row["processes"].items():
            identity = process["identity"]
            key = f"{role}:{identity['pid']}:{identity['start_ticks']}"

            if key not in self.peaks:
                self.peaks[key] = {
                    "role": role,
                    "identity": identity,
                    "observed_peak": {},
                    "thread_identities": {},
                }
            peak = self.peaks[key]

            for name, value in process["resources"].items():
                peak["observed_peak"][name] = max(peak["observed_peak"].get(name, 0), value)

            for tid, thread in process["threads"].items():
                peak["thread_identities"][f"{tid}:{thread['start_ticks']}"] = thread
        return row

    def get_report(self) -> dict:
        """Return sampled evidence; it does not include unobserved transient resources."""
        return {
            "samples": self.samples,
            "unavailable_samples_by_role": self.unavailable.copy(),
            "processes": list(self.peaks.values()),
            "peak_scope": "sampled_observed_peaks_not_kernel_high_water",
            "transient_threads_may_be_unobserved": True,
        }

def validate_affinity(
    row: dict, *, role: str, executable_identity: tuple[int, int], cpus: tuple[int, ...],
) -> None:
    """Check native samples by inode, re-reading a possible pre-exec mask once.

    A launcher sample may precede native exec while /proc/PID/exe already
    names the native inode. A stale inherited mask alone is not a violation.
    This observes masks; it never changes affinity during the measurement.
    """
    process = row["processes"].get(role)

    if process is None:
        return

    identity = process["identity"]
    executable = Path(f"/proc/{identity['pid']}/exe")

    try:
        status = executable.stat()

    except (FileNotFoundError, ProcessLookupError):
        return

    if (status.st_dev, status.st_ino) != executable_identity:
        return

    current_identity = read_identity(identity["pid"])

    if current_identity is None:
        return

    if current_identity != identity:
        raise RuntimeError("observed process identity changed during affinity observation")

    allowed = set(cpus)

    if all(thread["affinity"] and set(thread["affinity"]) <= allowed for thread in process["threads"].values()):
        return

    current = observe_processes({role: identity["pid"]})["processes"].get(role)

    if current is None:
        return

    if current["identity"] != identity:
        raise RuntimeError("observed process identity changed during affinity recheck")

    try:
        status = executable.stat()

    except (FileNotFoundError, ProcessLookupError):
        return

    if (status.st_dev, status.st_ino) != executable_identity:
        raise RuntimeError("observed process executable changed during affinity recheck")

    if any(not thread["affinity"] or not set(thread["affinity"]) <= allowed for thread in current["threads"].values()):
        raise ValueError("native process thread escaped its allowed CPU mask")


def read_clock_id(pid: int) -> int:
    """Ask libc for the process-wide CPU clock; errors are returned, not errno."""

    if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF:
        raise ValueError("CPU clock requires a positive process ID")

    function = ctypes.CDLL(None).clock_getcpuclockid
    function.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    function.restype = ctypes.c_int
    clock_id = ctypes.c_int()
    error = function(pid, ctypes.byref(clock_id))

    if error:
        raise OSError(error, os.strerror(error), f"process {pid}")

    return clock_id.value


class ProcessCpuClock:
    """Bind explicitly to one live process identity and its user + system CPU.

    The clock covers that process's threads, not children or a process tree.
    The caller chooses a window by subtracting observations of this same clock.
    """

    def __init__(self, pid: int) -> None:

        if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF:
            raise ValueError("CPU clock requires a positive process ID")

        self.pid = pid
        self.identity: dict[str, int] | None = None
        self.clock_id: int | None = None
        self.resolution: float | None = None

    def start(self) -> Self:
        """Observe identity and validate the kernel clock without changing the process."""

        if self.identity is not None:
            raise RuntimeError("CPU clock has already been started")

        identity = read_identity(self.pid)

        if identity is None:
            raise ProcessLookupError(f"CPU clock process {self.pid} is not alive")

        clock_id = read_clock_id(self.pid)
        resolution = time.clock_getres(clock_id)

        if not math.isfinite(resolution) or resolution <= 0:
            raise ValueError("Process CPU clock has invalid resolution")

        self.identity, self.clock_id, self.resolution = identity, clock_id, resolution

        try:
            self.read_ns()

        except BaseException:
            self.identity = self.clock_id = self.resolution = None
            raise

        return self

    def read_ns(self) -> int:
        """Read nanoseconds, not nanosecond accuracy; reject exit or PID reuse."""

        if self.identity is None or self.clock_id is None:
            raise RuntimeError("CPU clock has not been started")

        if read_identity(self.pid) != self.identity:
            raise ProcessLookupError(f"CPU clock process {self.pid} exited or changed identity")

        value = time.clock_gettime_ns(self.clock_id)

        if read_identity(self.pid) != self.identity:
            raise ProcessLookupError(f"CPU clock process {self.pid} exited or changed identity")

        if type(value) is not int or value < 0:
            raise ValueError("Process CPU clock returned an invalid value")

        return value

    def describe(self) -> dict:
        """Describe the bound clock and its reported resolution, never its accuracy."""

        if self.identity is None:
            raise RuntimeError("CPU clock has not been started")

        return {
            **self.identity, "clock_id": self.clock_id,
            "source": "clock_getcpuclockid + clock_gettime_ns; process-wide user + system",
            "reported_resolution_seconds": self.resolution, "resolution_is_not_accuracy": True,
        }

    def read_observation(self) -> dict:
        """Cross-check coarse /proc CPU against the clock, never as a fallback."""

        if self.identity is None:
            raise RuntimeError("CPU clock has not been started")

        began = time.monotonic()

        if read_identity(self.pid) != self.identity:
            raise ProcessLookupError(f"CPU clock process {self.pid} exited or changed identity")

        proc = read_cpu_seconds(self.pid)
        clock = self.read_ns()
        ended = time.monotonic()

        if type(proc) not in (int, float) or not math.isfinite(proc) or proc < 0:
            raise ValueError("Invalid coarse process CPU counter")

        ticks = os.sysconf("SC_CLK_TCK")

        if ticks <= 0:
            raise ValueError("Invalid process CPU tick resolution")

        return {
            "identity": self.identity.copy(), "clock_ns": clock, "proc_cpu_seconds": proc,
            "proc_resolution_seconds": 1 / ticks, "read_monotonic": [began, ended],
        }


def compare_cpu_observations(before: dict, after: dict) -> dict:
    """Compare two interfaces to kernel accounting, retaining clock disagreement."""

    if type(before) is not dict or type(after) is not dict:
        raise ValueError("Invalid CPU cross-check observation")

    for observation in (before, after):
        identity = observation.get("identity")
        interval = observation.get("read_monotonic")

        if (
            type(identity) is not dict or type(identity.get("pid")) is not int or identity["pid"] <= 0
            or type(identity.get("start_ticks")) is not int or identity["start_ticks"] < 0
            or type(interval) not in (list, tuple) or len(interval) != 2
        ):
            raise ValueError("Invalid CPU cross-check identity or read interval")

        began, ended = interval
        proc, resolution, clock_ns = (
            observation.get("proc_cpu_seconds"), observation.get("proc_resolution_seconds"), observation.get("clock_ns"),
        )
        values = (began, ended, proc, resolution)

        if (
            any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values)
            or ended < began or resolution <= 0 or type(clock_ns) is not int or clock_ns < 0
        ):
            raise ValueError("Invalid CPU cross-check observation")

    if before["identity"] != after["identity"]:
        raise ValueError("CPU observation process identity changed")

    if before["read_monotonic"][1] > after["read_monotonic"][0]:
        raise ValueError("CPU cross-check read intervals overlap or decreased")

    clock = (after["clock_ns"] - before["clock_ns"]) / 1e9
    proc = after["proc_cpu_seconds"] - before["proc_cpu_seconds"]

    if clock < 0 or proc < 0:
        raise ValueError("CPU cross-check counter decreased")

    skew = sum(observation["read_monotonic"][1] - observation["read_monotonic"][0] for observation in (before, after))
    allowance = 2 * max(before["proc_resolution_seconds"], after["proc_resolution_seconds"]) + skew
    return {
        "clock_seconds": clock, "proc_seconds": proc, "difference_seconds": clock - proc,
        "allowance_seconds": allowance, "agrees_within_tick_and_read_skew": abs(clock - proc) <= allowance,
        "scope": "two interfaces to kernel CPU accounting, not independent hardware measurements",
    }

