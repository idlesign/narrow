import ssl
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow import NarrowException, readiness
from narrow.configuration import get_ssl_ca_path, get_ssl_tuple

@pytest.mark.parametrize("ca_file", [None, Path("explicit-ca.pem")])
def test_custom_get_probe_does_not_read_unbounded_body(ca_file: Path | None, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock()
    connection.getresponse.return_value.status = 204
    context = Mock(return_value="tls-context")
    monkeypatch.setattr(readiness.ssl, "create_default_context", context)
    monkeypatch.setattr(readiness.http.client, "HTTPSConnection", Mock(return_value=connection))

    count = readiness.wait_http_ready("https://127.0.0.1:8080/health?check=yes", timeout=1, ca_file=ca_file, expected_status=204)

    assert count == 1
    context.assert_called_once_with(cafile=ca_file)
    connection.request.assert_called_once_with("GET", "/health?check=yes", headers={"Connection": "close"})
    connection.getresponse.return_value.read.assert_not_called()
    connection.close.assert_called_once()


@pytest.fixture(autouse=True)
def readiness_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness, "monotonic", lambda: 0.0)
    monkeypatch.setattr(readiness, "sleep", Mock())


@pytest.mark.parametrize("protocol", ["http", "https"])
def test_readiness_checks_response_and_processes(protocol: str, monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock()
    connection.getresponse.return_value = SimpleNamespace(status=200, read=Mock(return_value=b"Hello there!"))
    constructor = Mock(return_value=connection)
    monkeypatch.setattr(readiness.http.client, "HTTPConnection", constructor)
    monkeypatch.setattr(readiness.http.client, "HTTPSConnection", constructor)
    stand = SimpleNamespace(
        protocol=protocol, alias="example", address="127.0.0.1:8000", uses_app=False, check_running=Mock(),
    )

    readiness.wait_ready(stand, timeout=1)

    assert stand.check_running.call_count == 2
    connection.close.assert_called_once()

    if protocol == "https":
        context = constructor.call_args.kwargs["context"]
        assert context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.get_ca_certs(binary_form=True) == [ssl.PEM_cert_to_DER_cert(Path(get_ssl_ca_path()).read_text())]


def test_wrong_response_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock()
    connection.getresponse.return_value = SimpleNamespace(status=500, read=Mock(return_value=b"other service"))
    monkeypatch.setattr(readiness.http.client, "HTTPConnection", Mock(return_value=connection))
    monkeypatch.setattr(readiness, "sleep", Mock())
    stand = SimpleNamespace(
        protocol="http", alias="example", address="127.0.0.1:8000", uses_app=False, check_running=Mock(),
    )

    with pytest.raises(NarrowException, match="Unexpected readiness"):
        readiness.wait_ready(stand, timeout=1, attempts=2)

    assert connection.request.call_count == 2


def test_builtin_readiness_uses_frozen_ca_without_reopening_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Mock()
    source = Mock(side_effect=AssertionError("Reopened CA outside the frozen request"))
    monkeypatch.setattr(readiness, "wait_http_ready", probe)
    monkeypatch.setattr(readiness, "get_ssl_ca_path", source)
    stand = SimpleNamespace(protocol="https", address="127.0.0.1:8000", uses_app=False, check_running=Mock())
    frozen = Path("inputs/frozen-ca.pem")

    readiness.wait_ready(stand, timeout=1, ca_file=frozen)

    assert probe.call_args.kwargs["ca_file"] is frozen
    source.assert_not_called()


@pytest.mark.parametrize("occupied", (False, True))
def test_port_check_allows_reuse_but_rejects_listeners(occupied: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock(return_value=False)
    probe.bind.side_effect = OSError("address already in use") if occupied else None
    monkeypatch.setattr(readiness.socket, "socket", Mock(return_value=probe))

    with pytest.raises(NarrowException, match="unavailable") if occupied else nullcontext():
        readiness.check_available("127.0.0.1:8000")

    probe.setsockopt.assert_called_once_with(readiness.socket.SOL_SOCKET, readiness.socket.SO_REUSEADDR, 1)
    probe.bind.assert_called_once_with(("127.0.0.1", 8000))


def test_bundled_tls_uses_a_separate_trust_anchor() -> None:
    certificate, key = get_ssl_tuple()
    context = ssl.create_default_context(cafile=get_ssl_ca_path())

    context.load_cert_chain(certificate, key)

    assert context.cert_store_stats()["x509_ca"] == 1
    assert ssl.PEM_cert_to_DER_cert(Path(certificate).read_text()) not in context.get_ca_certs(binary_form=True)
