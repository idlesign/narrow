import errno
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow.measurement import system


def test_topology_reports_missing_optional_facts_without_guessing(tmp_path: Path) -> None:
    directory = tmp_path / "cpu6/topology"
    directory.mkdir(parents=True)
    (directory / "physical_package_id").write_text("0\n")
    (directory / "core_id").write_text("3\n")

    result = system.read_topology([6, 6], tmp_path)

    assert set(result) == {"6"}
    assert result["6"]["package"] == "0" and result["6"]["core"] == "3"
    assert result["6"]["max_frequency_khz"] is None
    assert set(result["6"]["unavailable"]) == {"siblings", "max_frequency_khz", "governor"}

    with pytest.raises(ValueError, match="CPU 7 topology is unavailable"):
        system.read_topology([7], tmp_path)


@pytest.mark.parametrize("cpu", [-1, True, "6"])
def test_topology_rejects_invalid_cpu_before_read(cpu: object, tmp_path: Path) -> None:

    with pytest.raises(ValueError, match="nonnegative integers"):
        system.read_topology([cpu], tmp_path)


def test_thread_masks_skip_only_disappearing_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "iterdir", lambda path: iter([path / "123", path / "124"]))
    masks = Mock(side_effect=[{6}, ProcessLookupError("gone")])
    monkeypatch.setattr(system.os, "sched_getaffinity", masks)

    assert system.read_thread_affinities(123) == {"123": [6]}

    masks.side_effect = PermissionError("denied")

    with pytest.raises(PermissionError, match="denied"):
        system.read_thread_affinities(123)


def test_explicit_pinning_handles_existing_threads_and_verifies_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"123": [1], "124": [1]}
    identity = {"pid": 123, "start_ticks": 9}
    monkeypatch.setattr(system, "read_identity", lambda pid: identity)
    monkeypatch.setattr(system, "read_thread_affinities", lambda pid: deepcopy(state))
    applied = []

    def pin(tid: int, cpus: set[int]) -> None:
        applied.append((tid, cpus))
        state[f"{tid}"] = sorted(cpus)

    monkeypatch.setattr(system.os, "sched_setaffinity", pin)

    assert system.pin_process(123, 6) == {"123": [6], "124": [6]}
    assert applied == [(123, {6}), (124, {6})]


def test_pinning_has_bounded_passes_and_refuses_reused_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = {"pid": 123, "start_ticks": 9}
    identities = Mock(return_value=identity)
    monkeypatch.setattr(system, "read_identity", identities)
    monkeypatch.setattr(system, "read_thread_affinities", lambda pid: {"123": [1]})
    pin = Mock()
    monkeypatch.setattr(system.os, "sched_setaffinity", pin)

    with pytest.raises(RuntimeError, match="did not settle"):
        system.pin_process(123, 6)

    assert pin.call_count == 4
    pin.reset_mock()
    identities.side_effect = [identity, {**identity, "start_ticks": 10}]

    with pytest.raises(ProcessLookupError, match="changed identity"):
        system.pin_process(123, 6)

    pin.assert_not_called()


def test_host_observer_starts_explicitly_and_keeps_missing_cpu_counters_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = Mock(return_value={"6": {"core": "3"}, "7": {"core": "4"}})
    monkeypatch.setattr(system, "read_topology", topology)
    observer = system.HostObservation({"server": 6, "generator": 7})
    topology.assert_not_called()

    with pytest.raises(RuntimeError, match="has not been started"):
        observer.observe()

    assert observer.start() is observer
    topology.assert_called_once()

    def read(path: Path) -> str:

        if path == Path("/proc/stat"):
            return "cpu 100 0 0 200 0 0 0 0\ncpu6 2 3 5 7 11 13 17 19 0 0\n"

        if "cpu6" in f"{path}":
            return "2400000\n"

        raise FileNotFoundError(errno.ENOENT, "missing CPU fact", f"{path}")

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(system.os, "sysconf", lambda name: 100)
    monkeypatch.setattr(system, "time", SimpleNamespace(monotonic=lambda: 12.5))
    result = observer.observe()

    assert result["monotonic"] == 12.5 and result["busy_seconds"] == {"6": 0.59, "7": None}
    assert result["frequency_khz"] == {"6": 2400000, "7": None}
    assert set(result["unavailable"]) == {"busy_seconds.7", "frequency_khz.7"}


def test_unreadable_host_facts_are_not_zero_or_environment_dumps(monkeypatch: pytest.MonkeyPatch) -> None:

    def read(path: Path) -> str:

        if path == Path("/proc/stat"):
            return "cpu 1 2 3 4\nintr 99\ncpu6 1 2 3 4\n"

        if path == Path("/proc/loadavg"):
            return "1.0 2.0 3.0 1/20 123\n"

        raise PermissionError(errno.EACCES, "denied", f"{path}")

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(system.os, "sched_getaffinity", Mock(side_effect=PermissionError(errno.EACCES, "denied")))
    monkeypatch.setattr(system.os, "cpu_count", lambda: None)
    monkeypatch.setattr(system.os, "sysconf", lambda name: 100)

    def space(path: Path | str) -> SimpleNamespace:

        if f"{path}" == "/dev/shm":
            return SimpleNamespace(f_bavail=2, f_frsize=4096)

        raise FileNotFoundError(errno.ENOENT, "missing filesystem", f"{path}")

    monkeypatch.setattr(system.os, "statvfs", space)
    result = system.observe_host(filesystem=Path("/private/user/worktree"))

    assert result["cpu_stat"] == "cpu 1 2 3 4\ncpu6 1 2 3 4"
    assert result["affinity"] is None and result["cpu_count"] is None
    assert result["filesystem_available_sz"] is None and result["shm_available_sz"] == 8192
    assert set(result["unavailable"]) == {
        "cpu_pressure", "memory_pressure", "io_pressure", "affinity", "cpu_count", "filesystem_available_sz",
    }
    assert "/private/user" not in f"{result}" and "environment" not in result


@pytest.fixture
def machine_facts(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    hardware = {
        f"{cpu}": {
            "package": "0", "core": f"{cpu}", "siblings": f"{cpu}", "max_frequency_khz": "2400000",
            "governor": "performance", "unavailable": {},
        } for cpu in (6, 7)
    }
    cpuinfo = "\n\n".join(
        f"processor: {cpu}\nvendor_id: Vendor\nmodel name: Model\ncpu family: 6\nmodel: 1\nstepping: 2\nmicrocode: 0x3"
        for cpu in (6, 7)
    )
    state = SimpleNamespace(hardware=hardware, cpuinfo=cpuinfo)
    monkeypatch.setattr(system, "read_topology", lambda cpus: deepcopy(state.hardware))
    monkeypatch.setattr(Path, "read_text", lambda path: state.cpuinfo)
    monkeypatch.setattr(system.platform, "release", lambda: "synthetic-kernel")
    monkeypatch.setattr(system.platform, "machine", lambda: "synthetic-architecture")
    monkeypatch.setattr(system.os, "sched_getaffinity", lambda pid: {99})
    monkeypatch.setattr(system.os, "sysconf", lambda name: 100 if name == "SC_CLK_TCK" else 4096)
    monkeypatch.setattr(system, "time", SimpleNamespace(monotonic=Mock(side_effect=AssertionError("volatile clock read"))))
    return state


def test_machine_identity_keeps_stable_facts_without_inherited_affinity_subset_check(
    machine_facts: SimpleNamespace,
) -> None:
    roles = {"server": 6, "generator": 7}

    before = system.collect_machine(roles)
    after = system.collect_machine(roles)

    assert before == after and before["unavailable"] == {}
    assert before["affinity"] == [99] and before["cpu_roles"] == roles
    assert before["topology"]["6"]["governor"] == "performance"
    assert before["processors"]["6"]["microcode"] == "0x3"
    assert not {"utc", "monotonic", "loadavg", "frequency_khz", "hostname"}.intersection(before)


def test_machine_identity_marks_unknown_microcode_and_refuses_missing_cpu(machine_facts: SimpleNamespace) -> None:
    machine_facts.cpuinfo = machine_facts.cpuinfo.replace("\nmicrocode: 0x3", "")

    result = system.collect_machine({"server": 6, "generator": 7})

    assert result["processors"]["6"]["microcode"] is None
    assert "processors.6.microcode" in result["unavailable"]

    with pytest.raises(ValueError, match="identity observations are incomplete"):
        system.collect_machine({"server": 6, "generator": 8})


def test_machine_identity_accepts_arm_cpuinfo_without_inventing_x86_fields(machine_facts: SimpleNamespace) -> None:
    machine_facts.cpuinfo = "\n\n".join(
        f"processor: {cpu}\nCPU implementer: 0x41\nCPU architecture: 8\nCPU variant: 0x0\nCPU part: 0xd0b\nCPU revision: 1"
        for cpu in (6, 7)
    )

    result = system.collect_machine({"server": 6, "generator": 7})

    assert result["processors"]["6"]["CPU part"] == "0xd0b" and result["unavailable"] == {}
    assert "vendor_id" not in result["processors"]["6"]
