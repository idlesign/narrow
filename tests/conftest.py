from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from copy import deepcopy
import hashlib

import pytest

from narrow import STANDS, runner
from narrow.benchers.oha import OhaBencher
from narrow.configuration import LoadSpec
from narrow.measurement import budget as budgets
from narrow.requests import RequestSpec
from narrow.targets import command as target_commands
from narrow.processes import CommandResult
from narrow.processes.limits import ProcessLimits
from narrow.measurement.conditions import build_http_workload, describe_http_endpoint
from narrow.targets import CommandTarget
from narrow.reports.results import SampleResult
from narrow.stands import _base, granian, nginx, uwsgi
from narrow.stands._base import Stand


@pytest.fixture
def prepared_stand() -> Iterator[Callable[[str], Stand]]:
    stands = []

    def prepare(alias: str) -> Stand:
        stand = STANDS[alias]()
        stand.resources.prepare()
        stands.append(stand)
        return stand

    yield prepare

    for stand in stands:
        stand.resources.close()


@pytest.fixture
def datafix_dir() -> Path:
    return Path(__file__).parent / "datafixtures"


@pytest.fixture
def measurement_sections() -> dict:
    machine = {
        "kernel": "test-kernel", "architecture": "test-arch", "affinity": [1], "cpu_roles": {"generator": 1},
        "processors": {"1": {"vendor": "test", "model": "test-cpu", "microcode": "0x1"}},
        "topology": {"1": {
            "package": "0", "core": "1", "siblings": "1", "max_frequency_khz": "2400000",
            "governor": "performance", "unavailable": {},
        }},
        "governors": {"1": "performance"}, "clock_ticks": 100, "page_size": 4096, "unavailable": {},
    }
    return {
        "workload": build_http_workload(
            "http://example.test/api", RequestSpec(), {"body": None, "ca_file": None},
            (LoadSpec(requests=2),), {"scenario": "a" * 64},
        ),
        "tools": {
            "controller": {
                "narrow": {"payload_sha256": {"narrow/runner.py": "b" * 64}},
                "python": {
                    "version": "3.12.10", "implementation": "cpython", "abi": "cpython-312-test",
                    "gil_enabled": True, "sha256": "c" * 64,
                },
            },
            "generator": {"name": "oha", "version": "oha 1.16.0", "sha256": "d" * 64},
        },
        "environment": {"machine": machine, "generator_limits": ProcessLimits().serialize()},
        "policy": {"repeats": 1, "warmup_requests": 0, "target_settings": {"workers": 1}},
    }

@pytest.fixture
def run_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, measurement_sections: dict) -> SimpleNamespace:
    binary = tmp_path / "fake-server"
    binary.write_bytes(b"unit-test executable identity; never executed")
    handles = []

    def create(argv: list[str]) -> Mock:
        handle = Mock(
            argv=tuple(argv), collect_output=Mock(return_value=(b"server out", b"server err")), output_truncated=False,
        )
        handles.append(handle)
        return handle

    def measure(url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits) -> SampleResult:
        metadata = {**request.serialize(), **describe_http_endpoint(request.build_url(url))}

        for name in ("body", "ca_file"):

            if (path := getattr(request, name)) is not None:
                data = path.read_bytes()
                metadata[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "name": path.name}

        return SampleResult(
            load.clients, status="ok", rps=10.0, responses=load.requests,
            successful_responses=load.requests, raw_stdout=b"generator JSON", raw_stderr=b"diagnostics",
            request=metadata,
        )

    monkeypatch.setattr(_base.shutil, "which", Mock(return_value=f"{binary}"))
    monkeypatch.setattr(_base, "ProcessHandle", create)
    monkeypatch.setattr(_base, "check_available", Mock())
    ready = Mock()
    monkeypatch.setattr(_base, "wait_ready", ready)
    monkeypatch.setattr(nginx, "run_command", Mock(return_value="nginx test"))
    monkeypatch.setattr(granian, "run_command", Mock(return_value="granian test"))
    monkeypatch.setattr(uwsgi, "run_command", Mock(return_value="uwsgi test"))
    bencher = OhaBencher()
    monkeypatch.setattr(bencher, "prepare", Mock(return_value={"version": "oha 1.16.0", "sha256": "d" * 64}))
    monkeypatch.setattr(bencher, "run_once", Mock(side_effect=measure))
    monkeypatch.setattr(runner, "BENCHERS", {"oha": lambda: bencher})
    clock = [0.0]
    monkeypatch.setattr(budgets, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(budgets, "read_boot_id", lambda: "synthetic-runner-boot")
    monkeypatch.setattr(runner, "collect_controller_identity", lambda: deepcopy(measurement_sections["tools"]["controller"]))
    monkeypatch.setattr(runner, "collect_machine", lambda roles: deepcopy(measurement_sections["environment"]["machine"]))
    monkeypatch.setattr(runner, "observe_host", lambda **options: {"monotonic": clock[0], "unavailable": {}})
    monkeypatch.setattr(runner, "read_cpu_roles", lambda limits: {"generator": 1})
    monkeypatch.setattr(runner, "bootstrap", lambda: {"ulimit_soft": 1024, "ulimit_hard": 4096, "max_conn": 8192, "tcp_backlog": 1024})
    return SimpleNamespace(bencher=bencher, handles=handles, ready=ready, clock=clock)

@pytest.fixture
def command_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    binary = tmp_path / "server"
    binary.write_bytes(b"never executed")
    binary.chmod(0o700)
    server = Mock(collect_output=Mock(return_value=(b"server output", b"PRIVATE")), output_truncated=False)
    probe = Mock(
        wait=Mock(return_value=CommandResult((), 0, b'{"attempts":1}', b"", 0.1)),
        collect_output=Mock(return_value=(b'{"attempts":1}', b"")), output_truncated=False,
    )
    create = Mock(side_effect=[server, probe])
    available = Mock()
    monkeypatch.setattr(target_commands, "ProcessHandle", create)
    monkeypatch.setattr(target_commands, "check_available", available)
    target = CommandTarget("http://127.0.0.1:8080/api", ("./server", "--flag"), cwd=tmp_path, env={"SECRET": "PRIVATE"})
    return SimpleNamespace(target=target, server=server, probe=probe, create=create, available=available, binary=binary)
