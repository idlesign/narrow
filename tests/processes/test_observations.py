import ctypes
import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


@pytest.fixture
def cpu_clock_system(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(identity={"pid": 12345, "start_ticks": 9})
    state.identities = Mock(return_value=state.identity)
    state.acquire = Mock(return_value=-123)
    state.coarse = Mock(return_value=1.0)
    state.time = SimpleNamespace(
        clock_getres=Mock(return_value=1e-9), clock_gettime_ns=Mock(return_value=1_000_000_000),
        monotonic=Mock(side_effect=[0, 0.001, 2, 2.001]),
    )
    monkeypatch.setattr(observations, "read_identity", state.identities)
    monkeypatch.setattr(observations, "read_clock_id", state.acquire)
    monkeypatch.setattr(observations, "read_cpu_seconds", state.coarse)
    monkeypatch.setattr(observations, "time", state.time)
    monkeypatch.setattr(observations.os, "sysconf", lambda name: 100)
    return state


def test_process_cpu_clock_binds_explicitly_without_constructor_syscalls(cpu_clock_system: SimpleNamespace) -> None:
    clock = observations.ProcessCpuClock(12345)
    cpu_clock_system.identities.assert_not_called()
    cpu_clock_system.acquire.assert_not_called()
    cpu_clock_system.time.clock_gettime_ns.assert_not_called()

    with pytest.raises(RuntimeError, match="has not been started"):
        clock.read_ns()

    assert clock.start() is clock and clock.read_ns() == 1_000_000_000
    assert clock.describe() == {
        **cpu_clock_system.identity, "clock_id": -123,
        "source": "clock_getcpuclockid + clock_gettime_ns; process-wide user + system",
        "reported_resolution_seconds": 1e-9, "resolution_is_not_accuracy": True,
    }

    with pytest.raises(RuntimeError, match="has already been started"):
        clock.start()


@pytest.mark.parametrize("pid", [0, -1, True, 1.5, 2**32 + 12345])
def test_cpu_clock_rejects_invalid_pid_before_any_syscall(pid: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observations.ctypes, "CDLL", lambda *args: pytest.fail("invalid PID reached libc"))

    with pytest.raises(ValueError, match="positive process ID"):
        observations.read_clock_id(pid)

    with pytest.raises(ValueError, match="positive process ID"):
        observations.ProcessCpuClock(pid)


@pytest.mark.parametrize("error", [0, errno.EPERM, errno.ESRCH])
def test_cpu_clock_libc_errors_are_return_values_not_errno(error: int, monkeypatch: pytest.MonkeyPatch) -> None:

    def acquire(pid: int, target: object) -> int:
        assert pid == 12345
        ctypes.cast(target, ctypes.POINTER(ctypes.c_int)).contents.value = -123
        return error

    monkeypatch.setattr(observations.ctypes, "CDLL", lambda name: SimpleNamespace(clock_getcpuclockid=acquire))

    if error:

        with pytest.raises(OSError, match="process 12345") as failure:
            observations.read_clock_id(12345)

        assert failure.value.errno == error

    else:
        assert observations.read_clock_id(12345) == -123


@pytest.mark.parametrize("identities,reads", [
    ([None], 0), ([{"pid": 12345, "start_ticks": 9}, None], 1),
    ([{"pid": 12345, "start_ticks": 9}, {"pid": 12345, "start_ticks": 10}], 1),
])
def test_cpu_clock_checks_identity_on_both_sides_of_the_counter_read(
    cpu_clock_system: SimpleNamespace, identities: list[dict | None], reads: int,
) -> None:
    clock = observations.ProcessCpuClock(12345).start()
    cpu_clock_system.identities.side_effect = identities
    cpu_clock_system.time.clock_gettime_ns.reset_mock()

    with pytest.raises(ProcessLookupError, match="exited or changed identity"):
        clock.read_ns()

    assert cpu_clock_system.time.clock_gettime_ns.call_count == reads


@pytest.mark.parametrize("resolution", [0, -1, float("nan"), float("inf")])
def test_cpu_clock_rejects_invalid_resolution_without_reading_counter(
    cpu_clock_system: SimpleNamespace, resolution: float,
) -> None:
    cpu_clock_system.time.clock_getres.return_value = resolution
    clock = observations.ProcessCpuClock(12345)

    with pytest.raises(ValueError, match="invalid resolution"):
        clock.start()

    cpu_clock_system.time.clock_gettime_ns.assert_not_called()


def test_cpu_clock_missing_process_does_not_acquire_a_clock(cpu_clock_system: SimpleNamespace) -> None:
    cpu_clock_system.identities.return_value = None

    with pytest.raises(ProcessLookupError, match="not alive"):
        observations.ProcessCpuClock(12345).start()

    cpu_clock_system.acquire.assert_not_called()


def test_cancelled_initial_clock_read_does_not_publish_started_state(cpu_clock_system: SimpleNamespace) -> None:
    cpu_clock_system.time.clock_gettime_ns.side_effect = KeyboardInterrupt("cancel CPU observation")
    clock = observations.ProcessCpuClock(12345)

    with pytest.raises(KeyboardInterrupt, match="cancel CPU observation"):
        clock.start()

    with pytest.raises(RuntimeError, match="has not been started"):
        clock.describe()


def test_cpu_window_keeps_coarse_and_high_resolution_counters_separate(cpu_clock_system: SimpleNamespace) -> None:
    clock = observations.ProcessCpuClock(12345).start()
    cpu_clock_system.coarse.side_effect = [1, 1.11]
    cpu_clock_system.time.clock_gettime_ns.side_effect = [1_000_000_000, 1_100_000_000]

    before = clock.read_observation()
    after = clock.read_observation()
    comparison = observations.compare_cpu_observations(before, after)

    assert before["identity"] == after["identity"] == cpu_clock_system.identity
    assert comparison["clock_seconds"] == 0.1
    assert comparison["proc_seconds"] == pytest.approx(0.11)
    assert comparison["agrees_within_tick_and_read_skew"]
    assert comparison["allowance_seconds"] == pytest.approx(0.022)


@pytest.mark.parametrize("error", [ProcessLookupError("gone"), PermissionError("denied")])
def test_cpu_window_does_not_replace_unreadable_proc_with_zero(
    cpu_clock_system: SimpleNamespace, error: Exception,
) -> None:
    clock = observations.ProcessCpuClock(12345).start()
    cpu_clock_system.coarse.side_effect = error

    with pytest.raises(type(error), match=f"{error}"):
        clock.read_observation()


@pytest.mark.parametrize("patch", [
    {"identity": {"pid": 12345, "start_ticks": 10}}, {"clock_ns": 99}, {"proc_cpu_seconds": 0},
    {"read_monotonic": [3, 2]}, {"proc_resolution_seconds": float("nan")}, {"read_monotonic": [0, 0.002]},
    {"identity": None}, {"read_monotonic": []}, {"clock_ns": True},
])
def test_cpu_window_rejects_corrupt_or_decreasing_observations(patch: dict) -> None:
    before = {
        "identity": {"pid": 12345, "start_ticks": 9}, "clock_ns": 100, "proc_cpu_seconds": 1,
        "proc_resolution_seconds": 0.01, "read_monotonic": [0, 0.001],
    }
    after = {**before, "clock_ns": 200, "proc_cpu_seconds": 2, "read_monotonic": [2, 2.001], **patch}

    with pytest.raises(ValueError, match="CPU"):
        observations.compare_cpu_observations(before, after)


def test_cpu_window_reports_clock_disagreement_without_switching_sources() -> None:
    before = {
        "identity": {"pid": 12345, "start_ticks": 9}, "clock_ns": 1_000_000_000, "proc_cpu_seconds": 1,
        "proc_resolution_seconds": 0.01, "read_monotonic": [0, 0.001],
    }
    after = {**before, "clock_ns": 1_100_000_000, "proc_cpu_seconds": 1.2, "read_monotonic": [2, 2.001]}

    result = observations.compare_cpu_observations(before, after)

    assert result["clock_seconds"] == 0.1 and not result["agrees_within_tick_and_read_skew"]
    assert result["proc_seconds"] == pytest.approx(0.2)
