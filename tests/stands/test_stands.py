import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from narrow import APPS, STANDS, Settings
from narrow.configuration import get_ssl_tuple
from narrow.stands._base import Stand


def test_static_nginx_config(prepared_stand: Callable[[str], Stand], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Settings, "WORKERS", 3)
    monkeypatch.setattr(Settings, "MAX_CONNECTIONS", 1024)
    monkeypatch.setattr(Settings, "LISTEN_BACKLOG", 256)

    config = prepared_stand("nginx_static").get_config()

    assert "worker_processes 3;" in config
    assert "worker_connections 1024;" in config
    assert "listen 127.0.0.1:8001 backlog=256;" in config
    assert "return 200 'Hello there!';" in config


@pytest.mark.parametrize("module", ("proxy", "fastcgi", "scgi"))
def test_nginx_temp_paths_are_private(prepared_stand: Callable[[str], Stand], module: str) -> None:
    stand = prepared_stand("nginx_static")

    config = stand.get_config()

    assert f"{module}_temp_path \"{stand.resources.path}/{module}-temp\";" in config


def test_tls_proxy_config_uses_packaged_certificate(prepared_stand: Callable[[str], Stand]) -> None:
    cert, key = get_ssl_tuple()

    config = prepared_stand("nginx_ssl_tcp_uwsgi").get_config()

    assert Path(cert).is_file()
    assert Path(key).is_file()
    assert f"ssl_certificate \"{cert}\";" in config
    assert f"ssl_certificate_key \"{key}\";" in config
    assert "listen 127.0.0.1:4443 ssl backlog=" in config
    assert "uwsgi_pass \"127.0.0.1:8003\";" in config


def test_uwsgi_config_points_to_packaged_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Settings, "LISTEN_BACKLOG", 256)
    stand = STANDS["uwsgi"]()
    app = APPS["py"]()
    monkeypatch.setattr(stand, "app", app)

    section = stand.generate_section(stand.socket)
    configuration = section.as_configuration().format()

    assert app.get_entrypoint_path() in configuration
    assert "127.0.0.1:8002" in configuration
    assert sys.prefix in configuration
    assert "disable-logging = true" in configuration
    assert "listen = 256" in configuration


def test_static_tls_is_really_https(prepared_stand: Callable[[str], Stand]) -> None:
    stand = prepared_stand("nginx_ssl_static")
    config = stand.get_config()

    assert stand.protocol == "https"
    assert "listen 127.0.0.1:4443 ssl backlog=" in config
    assert "ssl_certificate_key" in config
    assert f"{stand.resources.path}/nginx.pid" in config
    assert not stand.uses_app


def test_instances_have_separate_resource_directories(prepared_stand: Callable[[str], Stand]) -> None:
    first = prepared_stand("nginx_static")
    second = prepared_stand("nginx_static")

    assert first.resources.path != second.resources.path
    assert first is not second
