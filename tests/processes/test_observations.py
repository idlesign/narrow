from pathlib import Path

import pytest

from narrow.processes import observations

def observed(cpu_seconds=1, *, pid=12, threads=None, rss=10, fds=4):
    return {
        "monotonic": 0,
        "processes": {
            "core": {
                "identity": {"pid": pid, "start_ticks": 9},
                "cpu_seconds": cpu_seconds,
                "resources": {"rss_kib": rss, "fds": fds, "threads": len(threads or {})},
                "threads": threads or {},
            }
        },
        "unavailable": [],
    }


def test_cpu_deltas_and_observed_peaks_do_not_invent_disappeared_thread_cpu(monkeypatch):
    before = observed(
        1,
        threads={
            "12": {"start_ticks": 9, "cpu_seconds": 1},
            "13": {"start_ticks": 10, "cpu_seconds": 0.5},
        },
    )
    after = observed(
        2,
        threads={
            "12": {"start_ticks": 9, "cpu_seconds": 1.75},
            "14": {"start_ticks": 11, "cpu_seconds": 0.25},
        },
        rss=9,
        fds=7,
    )
    delta = observations.calculate_cpu_delta(before, after)
    assert delta["process_seconds"]["core"] == 1
    assert delta["thread_seconds"]["core"] == {"12": 0.75, "13": None}
    assert delta["new_threads"]["core"] == ["14"]
    assert observations.calculate_cpu_delta(before, observed(pid=13))["process_seconds"]["core"] is None
    assert observations.calculate_cpu_delta(before, {"processes": {}})["missing_or_replaced_processes"] == [
        "core"
    ]
    samples = iter([before, after])
    monkeypatch.setattr(observations, "observe_processes", lambda _: next(samples))
    observer = observations.ResourceObserver()
    observer.sample({})
    observer.sample({})
    report = observer.get_report()
    assert report["samples"] == 2
    assert report["processes"][0]["observed_peak"] == {"rss_kib": 10, "fds": 7, "threads": 2}
    assert report["transient_threads_may_be_unobserved"]
    assert set(report["processes"][0]["thread_identities"]) == {"12:9", "13:10", "14:11"}



@pytest.mark.parametrize("fault", ["permission", "gone", "transient_thread"])
def test_proc_races_remain_unavailable_not_zero_cpu(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    original_read = Path.read_text
    original_iterdir = Path.iterdir
    root = Path("/proc/123")
    fields = ["S", *["0"] * 18, "9"]
    fields[11] = "100"
    fields[12] = "25"

    def read(path: Path, *args: object, **kwargs: object) -> str:

        if path.is_relative_to(root):

            if fault == "gone" or path.parent == root / "task/124":
                raise FileNotFoundError("synthetic process/thread disappeared")

            if fault == "permission" and path.name == "status":
                raise PermissionError("synthetic resources unavailable during exit")

            if path.name == "stat":
                return f"123 (synthetic actor) {' '.join(fields)}"

            if path.name == "status":
                return "VmRSS: 10 kB\n"

            return "actor"

        return original_read(path, *args, **kwargs)

    def iterdir(path: Path) -> object:

        if path == root / "task":
            return iter([path / "123", path / "124"])

        if path == root / "fd":
            return iter([path / "1"])

        return original_iterdir(path)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(observations.os, "sysconf", lambda name: 100)
    monkeypatch.setattr(observations.os, "sched_getaffinity", lambda pid: {2})
    observer = observations.ResourceObserver()

    row = observer.sample({"external": 123})
    report = observer.get_report()

    if fault == "transient_thread":
        assert row["unavailable"] == []
        assert row["processes"]["external"]["cpu_seconds"] == 1.25
        assert list(row["processes"]["external"]["threads"]) == ["123"]
        assert report["processes"][0]["observed_peak"] == {"rss_kib": 10, "threads": 2, "fds": 1}

    else:
        assert row["processes"] == {} and row["unavailable"] == ["external"]
        assert report["unavailable_samples_by_role"]["external"]["count"] == 1
        assert ("PermissionError" if fault == "permission" else "ProcessLookupError") in row["unavailable_reasons"]["external"]
