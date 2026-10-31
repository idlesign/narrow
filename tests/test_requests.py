import hashlib
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from narrow.requests import RequestSpec, snapshot_request, validate_url
from narrow.benchers.h2load import H2loadBencher
from narrow.benchers.weighttp import WeighttpBencher
from narrow.configuration import LoadSpec
from narrow.exceptions import BencherException

@pytest.mark.parametrize(("parameters", "message"), [
    ({"method": "GET\r\nX"}, "HTTP token"),
    ({"path": "//other.test/x"}, "one slash"),
    ({"path": "/x#fragment"}, "without a fragment"),
    ({"headers": (("Bad Name", "value"),)}, "Invalid HTTP header"),
    ({"headers": (("X-Test", "a\nb"),)}, "control characters"),
    ({"body": "body.bin"}, "pathlib.Path"),
    ({"expected_status": True}, "HTTP status code"),
    ({"expected_response_bytes": -1}, "nonnegative integer"),
    ({"method": "HEAD", "expected_response_bytes": 1}, "do not have a body"),
    ({"max_body_bytes": 0}, "positive integer"),
])
def test_reject_invalid_request(parameters: dict, message: str) -> None:

    with pytest.raises(ValueError, match=message):
        RequestSpec(**parameters)

@pytest.mark.parametrize(("url", "message"), [
    ("ftp://example.test/", "HTTP/HTTPS URL"),
    ("http://user:secret@example.test/", "URL credentials"),
    ("http://host/x\n", "control characters"),
    ("http://host:0/", "port must be positive"),
])
def test_reject_invalid_url(url: str, message: str) -> None:

    with pytest.raises(ValueError, match=message):
        validate_url(url)

def test_build_request_without_external_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    open_file = Mock(side_effect=AssertionError("unexpected file access"))
    monkeypatch.setattr(os, "open", open_file)
    request = RequestSpec(method="POST", path="/api?key=value", body=Path("input.bin"))

    assert request.build_url("https://example.test/old?unused=yes") == "https://example.test/api?key=value"
    assert RequestSpec().build_url("http://example.test/api?key=value") == "http://example.test/api?key=value"
    open_file.assert_not_called()

def test_snapshot_freezes_inputs_and_limits_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"original")
    request = RequestSpec(body=source, ca_file=source, headers=(("Cookie", "PRIVATE"),))

    prepared, metadata = snapshot_request(request, tmp_path / "inputs")
    source.write_bytes(b"changed")

    assert prepared.body.read_bytes() == prepared.ca_file.read_bytes() == b"original"
    assert metadata["body"]["sha256"] == hashlib.sha256(b"original").hexdigest()
    assert metadata["headers"] == [["Cookie", "[redacted]"]]
    assert "PRIVATE" not in repr(metadata)

def test_reject_oversized_body(tmp_path: Path) -> None:
    source = tmp_path / "body"
    source.write_bytes(b"oversized")

    with pytest.raises(ValueError, match="size limit"):
        snapshot_request(RequestSpec(body=source, max_body_bytes=2), tmp_path / "inputs")

    assert not (tmp_path / "inputs").exists()

def test_fifo_does_not_block_input_preparation(tmp_path: Path) -> None:
    source = tmp_path / "fifo"
    os.mkfifo(source)

    with pytest.raises(ValueError, match="regular file"):
        snapshot_request(RequestSpec(body=source), tmp_path / "inputs")

def test_redaction_handles_encoded_query_values() -> None:
    url = "https://example.test/api?token=PRIVATE%2FKEY"
    request = RequestSpec(headers=(("X-API-Key", "PRIVATE-HEADER"),))

    redacted = request.redact(f"{url} PRIVATE/KEY PRIVATE-HEADER".encode(), url)

    assert b"PRIVATE" not in redacted
    assert "PRIVATE-HEADER" not in repr(request.serialize())

@pytest.mark.parametrize("bencher_class", [H2loadBencher, WeighttpBencher])
def test_legacy_generator_rejects_unsupported_request(bencher_class: type) -> None:

    with pytest.raises(BencherException, match="require oha"):
        bencher_class().validate_load("http://127.0.0.1/", LoadSpec(requests=1), request=RequestSpec(method="POST"))
