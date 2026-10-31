import json
import os
import resource
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow.processes import limits as process_limits
from narrow.processes.limits import ProcessLimits, execute_command


@pytest.fixture
def limit_environment(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    limits = {
        resource.RLIMIT_AS: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
        resource.RLIMIT_CORE: (1024, resource.RLIM_INFINITY),
        resource.RLIMIT_FSIZE: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    }
    affinity = {2, 3}
    set_limit = Mock(side_effect=lambda kind, values: limits.__setitem__(kind, values))
    set_affinity = Mock(side_effect=lambda pid, values: (affinity.clear(), affinity.update(values)))
    execute = Mock()
    monkeypatch.setattr(process_limits.resource, "getrlimit", lambda kind: limits[kind])
    monkeypatch.setattr(process_limits.resource, "setrlimit", set_limit)
    monkeypatch.setattr(process_limits.os, "sched_getaffinity", lambda pid: set(affinity))
    monkeypatch.setattr(process_limits.os, "sched_setaffinity", set_affinity)
    monkeypatch.setattr(process_limits.os, "execvpe", execute)
    return SimpleNamespace(limits=limits, affinity=affinity, set_limit=set_limit, set_affinity=set_affinity, execute=execute)


@pytest.mark.parametrize("parameters", [
    {"cpus": ()}, {"cpus": (True,)}, {"cpus": (-1,)}, {"cpus": (2, 2)}, {"cpus": "2"},
    {"cpus": (2**31,)}, {"address_space_bytes": 0}, {"address_space_bytes": True},
    {"address_space_bytes": 1.5}, {"address_space_bytes": 2**63},
    {"file_size_bytes": 0}, {"file_size_bytes": True},
])
def test_invalid_limits_do_not_touch_host(parameters: dict, limit_environment: SimpleNamespace) -> None:

    with pytest.raises(ValueError, match="cpus must|address_space_bytes must|file_size_bytes must"):
        ProcessLimits(**parameters)

    limit_environment.set_limit.assert_not_called()
    limit_environment.set_affinity.assert_not_called()
    limit_environment.execute.assert_not_called()


def test_configuration_copies_mask_and_builds_launcher_without_changes(limit_environment: SimpleNamespace) -> None:
    cpus = [3, 2]
    limits = ProcessLimits(cpus=cpus, address_space_bytes=1024**3)
    cpus.clear()

    argv = limits.build_argv(["/tool", "--argument"], report_fd=8)

    assert limits.serialize() == {"cpus": [2, 3], "address_space_bytes": 1024**3, "file_size_bytes": None}
    assert argv[-2:] == ["/tool", "--argument"]
    assert json.loads(argv[-3]) == {"limits": limits.serialize(), "report_fd": 8}
    limit_environment.set_limit.assert_not_called()
    limit_environment.set_affinity.assert_not_called()
    limit_environment.execute.assert_not_called()


@pytest.mark.parametrize("limits", [
    ProcessLimits(), ProcessLimits(cpus=(4,)), ProcessLimits(cpus=(2,), address_space_bytes=1024**3, file_size_bytes=8192),
])
def test_child_verifies_limits_before_exec(
    limits: ProcessLimits, tmp_path: Path, limit_environment: SimpleNamespace,
) -> None:
    report = tmp_path / "report"
    argv = ["/tool with spaces", "$(not a shell command)"]
    seen = []

    def check_exec(*args: object) -> None:
        seen.append(json.loads(report.read_bytes()))

    limit_environment.execute.side_effect = check_exec

    with report.open("w+b") as stream:
        os.set_inheritable(stream.fileno(), True)
        execute_command(argv, limits=limits.serialize(), report_fd=stream.fileno())
        assert not os.get_inheritable(stream.fileno())

    applied = seen[0]["applied"]
    assert seen[0]["error"] is None
    assert applied["core_dump_bytes"] == 0
    assert applied["cpus"] == list(limits.cpus or (2, 3))
    assert applied["address_space_bytes"] == applied["address_space_hard_bytes"] == limits.address_space_bytes
    assert applied["file_size_bytes"] == applied["file_size_hard_bytes"] == limits.file_size_bytes
    limit_environment.execute.assert_called_once_with(argv[0], argv, os.environ)


@pytest.mark.parametrize(("limits", "hard_limit", "message"), [
    (ProcessLimits(address_space_bytes=8192), 4096, "exceeds the inherited hard limit"),
])
def test_child_rejects_unavailable_limits_before_changes(
    limits: ProcessLimits, hard_limit: int, message: str, tmp_path: Path, limit_environment: SimpleNamespace,
) -> None:
    limit_environment.limits[resource.RLIMIT_AS] = (hard_limit, hard_limit)
    report = tmp_path / "report"

    with report.open("w+b") as stream, pytest.raises(SystemExit, match="could not apply process limits"):
        execute_command(["/tool"], limits=limits.serialize(), report_fd=stream.fileno())

    data = json.loads(report.read_bytes())
    assert data["applied"] is None
    assert message in data["error"]
    limit_environment.set_limit.assert_not_called()
    limit_environment.set_affinity.assert_not_called()
    limit_environment.execute.assert_not_called()


def test_child_rejects_unconfirmed_limit(tmp_path: Path, limit_environment: SimpleNamespace) -> None:
    limit_environment.set_limit.side_effect = None
    report = tmp_path / "report"

    with report.open("w+b") as stream, pytest.raises(SystemExit, match="could not apply process limits"):
        execute_command(["/tool"], limits=ProcessLimits().serialize(), report_fd=stream.fileno())

    assert "not applied as requested" in json.loads(report.read_bytes())["error"]
    limit_environment.execute.assert_not_called()


def test_file_guard_checks_inherited_limit_before_any_change(tmp_path: Path, limit_environment: SimpleNamespace) -> None:
    limit_environment.limits[resource.RLIMIT_FSIZE] = (4096, 4096)
    report = tmp_path / "report"

    with report.open("w+b") as stream, pytest.raises(SystemExit, match="could not apply process limits"):
        execute_command(["/tool"], limits=ProcessLimits(file_size_bytes=8192).serialize(), report_fd=stream.fileno())

    assert "Requested file size exceeds the inherited hard limit" in json.loads(report.read_bytes())["error"]
    limit_environment.set_limit.assert_not_called()
    limit_environment.execute.assert_not_called()


def test_child_keeps_system_error_when_applying_limits(tmp_path: Path, limit_environment: SimpleNamespace) -> None:
    limit_environment.set_affinity.side_effect = PermissionError("affinity denied")
    report = tmp_path / "report"

    with report.open("w+b") as stream, pytest.raises(SystemExit, match="could not apply process limits"):
        execute_command(["/tool"], limits=ProcessLimits(cpus=(2,)).serialize(), report_fd=stream.fileno())

    assert json.loads(report.read_bytes())["error"] == "affinity denied"
    limit_environment.execute.assert_not_called()


def test_exec_failure_keeps_applied_limits_and_error(tmp_path: Path, limit_environment: SimpleNamespace) -> None:
    limit_environment.execute.side_effect = FileNotFoundError("test executable unavailable")
    report = tmp_path / "report"

    with report.open("w+b") as stream, pytest.raises(SystemExit, match="could not apply process limits"):
        execute_command(["/tool"], limits=ProcessLimits().serialize(), report_fd=stream.fileno())

    data = json.loads(report.read_bytes())
    assert data["error"] == "test executable unavailable"
    assert data["applied"]["core_dump_bytes"] == 0


def test_kernel_reduced_affinity_is_not_reported_as_the_requested_mask(
    tmp_path: Path, limit_environment: SimpleNamespace,
) -> None:
    limit_environment.set_affinity.side_effect = None
    report = tmp_path / "report"

    with report.open("w+b") as stream, pytest.raises(SystemExit, match="could not apply process limits"):
        execute_command(["/tool"], limits=ProcessLimits(cpus=(2,)).serialize(), report_fd=stream.fileno())

    assert "CPU mask was not applied as requested" in json.loads(report.read_bytes())["error"]
    limit_environment.execute.assert_not_called()
