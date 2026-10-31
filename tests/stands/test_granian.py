from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow import APPS, STANDS, NarrowException, Settings
from narrow.configuration import get_ssl_tuple
from narrow.stands import _base, granian
from narrow.stands._base import Stand
from narrow.targets import StandTarget


@pytest.mark.parametrize("app_alias", APPS)
def test_granian_serves_selected_wsgi_app(
    app_alias: str, tmp_path: Path, run_environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Settings, "WORKERS", 2)
    monkeypatch.setattr(Settings, "THREADS", 3)
    monkeypatch.setattr(Settings, "LISTEN_BACKLOG", 256)
    monkeypatch.setattr(Settings, "MAX_CONNECTIONS", 64)
    target = StandTarget("granian", app_alias)

    metadata = target.prepare()

    with target.setup(timeout=1):
        directory = target.stand.resources.path
        target.check_running()

    evidence = target.get_evidence()[0]
    assert evidence["argv"] == (
        f"{tmp_path / "fake-server"}", "--interface", "wsgi", "--http", "1",
        "--host", "127.0.0.1", "--port", "8004", "--workers", "2", "--blocking-threads", "3",
        "--runtime-threads", "1", "--backlog", "256", "--backpressure", "64", "--no-access-log",
        "--working-dir", f"{directory}", f"{APPS[app_alias]().get_entrypoint_path()}:application",
    )
    assert target.url == "http://127.0.0.1:8004/" and target.ca_file is None
    assert metadata["servers"]["granian"]["version"] == "granian test"
    assert metadata["apps"][app_alias]["entrypoint_sha256"]
    granian.run_command.assert_called_once_with([f"{tmp_path / "fake-server"}", "--version"])
    run_environment.ready.assert_called_once_with(target.stand, timeout=1, ca_file=None)
    run_environment.handles[0].stop.assert_called_once()
    assert evidence["files"]["stdout.bin"] == b"server out"
    assert target.stand.prc is target.stand.app is target.stand.resources.path is None
    assert not directory.exists()


def test_granian_https_and_access_logging(run_environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Settings, "LOG_WRITE", True)
    target = StandTarget("granian_ssl", "py")
    certificate, key = get_ssl_tuple()

    target.prepare()

    with target.setup(timeout=1):
        pass

    argv = target.get_evidence()[0]["argv"]
    assert target.url == "https://127.0.0.1:4444/" and target.ca_file.is_file()
    assert "--access-log" in argv and "--no-access-log" not in argv
    assert argv[argv.index("--port") + 1] == "4444"
    assert argv[-5:] == (
        "--ssl-certificate", certificate, "--ssl-keyfile", key, f"{target.app.get_entrypoint_path()}:application",
    )
    assert target.stand.protocol == "https"
    run_environment.handles[0].stop.assert_called_once()


@pytest.mark.parametrize("alias", ["granian", "granian_ssl"])
def test_missing_granian_does_not_start_or_allocate(
    alias: str, run_environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_base.shutil, "which", Mock(return_value=None))
    stand = STANDS[alias]()

    with pytest.raises(NarrowException, match="Server granian is not installed"):

        with stand.setup(APPS["py"]()):
            pytest.fail("missing server started")

    assert stand.resources.path is None
    assert run_environment.handles == []


def test_granian_finds_optional_dependency_without_path(
    tmp_path: Path, prepared_stand: Callable[[str], Stand], monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "granian"
    binary.write_bytes(b"not executed")
    binary.chmod(0o700)
    monkeypatch.setattr(granian.sys, "executable", f"{tmp_path / "python"}")
    monkeypatch.setenv("PATH", "")
    version = Mock(return_value="granian test")
    monkeypatch.setattr(granian, "run_command", version)
    stand = prepared_stand("granian")
    stand.app = APPS["py"]()
    start = Mock()
    monkeypatch.setattr(stand, "start_process", start)

    stand.check_tools()
    reported_version = stand.get_version()
    stand.bootstrap()

    assert reported_version == "granian test"
    version.assert_called_once_with([f"{binary}", "--version"])
    assert start.call_args.args[0][0] == f"{binary}"


def test_granian_rejects_unsupported_backlog_before_start(
    run_environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Settings, "LISTEN_BACKLOG", 127)
    stand = STANDS["granian"]()

    with pytest.raises(NarrowException, match="Granian requires LISTEN_BACKLOG >= 128"):

        with stand.setup(APPS["py"]()):
            pytest.fail("unsupported backlog was used")

    assert stand.resources.path is None
    assert run_environment.handles == []
