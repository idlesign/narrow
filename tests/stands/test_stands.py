from pathlib import Path

import pytest

from narrow import APPS, STANDS, Settings
from narrow.configuration import get_ssl_tuple


def test_static_nginx_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Settings, "WORKERS", 3)
    monkeypatch.setattr(Settings, "MAX_CONNECTIONS", 1024)

    config = STANDS["nginx_static"].get_config()

    assert "worker_processes 3;" in config
    assert "worker_connections 1024;" in config
    assert "listen 8001 backlog=1024;" in config
    assert "return 200 'Hello there!';" in config


def test_tls_proxy_config_uses_packaged_certificate() -> None:
    cert, key = get_ssl_tuple()

    config = STANDS["nginx_ssl_tcp_uwsgi"].get_config()

    assert Path(cert).is_file()
    assert Path(key).is_file()
    assert f"ssl_certificate {cert};" in config
    assert f"ssl_certificate_key {key};" in config
    assert "listen 4443 ssl backlog=" in config
    assert "uwsgi_pass 127.0.0.1:8003;" in config


def test_uwsgi_config_points_to_packaged_app(monkeypatch: pytest.MonkeyPatch) -> None:
    stand = STANDS["uwsgi"]
    app = APPS["py"]()
    monkeypatch.setattr(stand, "app", app)

    section = stand.generate_section(stand.socket)
    configuration = section.as_configuration().format()

    assert app.get_entrypoint_path() in configuration
    assert "127.0.0.1:8002" in configuration
