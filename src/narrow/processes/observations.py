"""Linux CPU and sampled resource peaks, not kernel high-water or tree totals."""

import os
import time
from collections.abc import Mapping
from pathlib import Path

from .tree import read_identity

def observe_processes(pids: Mapping[str, int]) -> dict:
    """A disappearing process/thread is reported, not silently assigned zero CPU."""
    result = {"monotonic": time.monotonic(), "processes": {}, "unavailable": []}
    ticks = os.sysconf("SC_CLK_TCK")

    for role, pid in pids.items():
        try:
            identity = read_identity(pid)

            if identity is None:
                raise ProcessLookupError("process identity not observed live")

            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            cpu = (int(fields[11]) + int(fields[12])) / ticks
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

        except (FileNotFoundError, ProcessLookupError, StopIteration, PermissionError) as error:
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

