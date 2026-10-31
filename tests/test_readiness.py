import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow import NarrowException, readiness
from narrow.configuration import get_ssl_tuple


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


def test_occupied_port_is_not_a_ready_stand(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock(return_value=False)
    probe.bind.side_effect = OSError("address already in use")
    monkeypatch.setattr(readiness.socket, "socket", Mock(return_value=probe))

    with pytest.raises(NarrowException, match="unavailable"):
        readiness.check_available("127.0.0.1:8000")


def test_bundled_certificate_covers_loopback_names() -> None:
    context = ssl.create_default_context(cafile=get_ssl_tuple()[0])
    certificate = context.get_ca_certs()[0]

    assert ("DNS", "localhost") in certificate["subjectAltName"]
    assert ("IP Address", "127.0.0.1") in certificate["subjectAltName"]
