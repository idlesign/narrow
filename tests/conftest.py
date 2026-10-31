from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow import STANDS, runner
from narrow.benchers.oha import OhaBencher
from narrow.configuration import LoadSpec
from narrow.requests import RequestSpec
from narrow.targets import command as target_commands
from narrow.processes import CommandResult
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
def run_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    binary = tmp_path / "fake-server"
    binary.write_bytes(b"unit-test executable identity; never executed")
    handles = []

    def create(argv: list[str]) -> Mock:
        handle = Mock(
            argv=tuple(argv), collect_output=Mock(return_value=(b"server out", b"server err")), output_truncated=False,
        )
        handles.append(handle)
        return handle

    def measure(url: str, load: LoadSpec, *, timeout: float, request: RequestSpec) -> SampleResult:
        return SampleResult(
            load.clients, status="ok", rps=10.0, responses=load.requests,
            successful_responses=load.requests, raw_stdout=b"generator JSON", raw_stderr=b"diagnostics",
            request=request.serialize(),
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
    monkeypatch.setattr(bencher, "prepare", Mock(return_value={"version": "oha 1.16.0", "sha256": "test"}))
    monkeypatch.setattr(bencher, "run_once", Mock(side_effect=measure))
    monkeypatch.setattr(runner, "BENCHERS", {"oha": lambda: bencher})
    monkeypatch.setattr(runner, "monotonic", lambda: 0.0)
    return SimpleNamespace(bencher=bencher, handles=handles, ready=ready)

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
