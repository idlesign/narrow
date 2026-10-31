import json
from collections.abc import Callable
from unittest.mock import Mock
from pathlib import Path

import pytest

from narrow.benchers import oha
from narrow.benchers import _base as base
from narrow.benchers.oha import OhaBencher, parse_oha_report
from narrow.configuration import LoadSpec, get_ssl_ca_path
from narrow.exceptions import BencherException
from narrow.processes import CommandResult
from narrow.requests import RequestSpec


@pytest.mark.parametrize(("filename", "requests", "expected", "status"), [
    ("success.json", 10, 5.0, "ok"), ("mixed.json", 6, 2.0, "http_error"),
])
def test_parse_metrics(
    filename: str, requests: int, expected: float, status: str, datafix_readbin: Callable[[str], bytes],
) -> None:
    raw = datafix_readbin(f"oha/{filename}")

    result = parse_oha_report(raw, LoadSpec(requests=requests))

    assert result.rps == expected
    assert result.status == status
    assert result.latency_ms == {"p50": 10.0, "p95": 20.0, "p99": 30.0}
    assert result.window_seconds == 2.0


@pytest.mark.parametrize("raw", [
    b"", b"{", b"{\"summary\":{},\"summary\":{}}", b"{\"summary\":{\"total\":NaN}}",
    b"[]", b"null", b"\xff",
])
def test_reject_invalid_report(raw: bytes) -> None:

    with pytest.raises(BencherException, match="Invalid oha JSON"):
        parse_oha_report(raw, LoadSpec(requests=10))


def test_deadline_is_not_a_response() -> None:
    raw = json.dumps({
        "summary": {"successRate": None, "total": 1, "requestsPerSec": 2, "totalData": 0},
        "statusCodeDistribution": {}, "errorDistribution": {oha.DEADLINE_ERROR: 2},
        "latencyPercentiles": {"p50": None, "p95": None, "p99": None},
    }).encode()

    result = parse_oha_report(raw, LoadSpec(duration=1, drain=False))

    assert result.responses == 0
    assert result.deadline_outcomes == 2
    assert result.latency_ms["p95"] is None

    with pytest.raises(BencherException, match="drain"):
        parse_oha_report(raw, LoadSpec(duration=1))


def test_build_explicit_http_policy_without_io(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare = Mock(side_effect=AssertionError("unexpected I/O"))
    monkeypatch.setattr(OhaBencher, "prepare", prepare)
    bencher = OhaBencher()
    load = LoadSpec(duration=1.5, clients=2, qps=20, keepalive=False)

    argv = bencher.build_argv("https://127.0.0.1:4443/", load, request=RequestSpec(ca_file=Path(get_ssl_ca_path())))

    assert argv[argv.index("--http-version") + 1] == "1.1"
    assert argv[argv.index("-z") + 1] == "1.5s"
    assert "--latency-correction" in argv
    assert "--disable-keepalive" in argv
    assert argv[argv.index("--cacert") + 1] == get_ssl_ca_path()
    assert "--insecure" not in argv
    prepare.assert_not_called()


@pytest.mark.parametrize(("exit_code", "error", "expected_status"), [
    (0, None, "ok"), (2, None, "error"), (0, "timed out", "error"),
])
def test_keep_raw_evidence_on_process_failure(
    exit_code: int, error: str | None, expected_status: str, monkeypatch: pytest.MonkeyPatch,
    datafix_readbin: Callable[[str], bytes],
) -> None:
    stdout = datafix_readbin("oha/success.json")
    process = Mock(argv=("oha",), collect_output=Mock(return_value=(stdout, b"diagnostic")))
    process.wait.return_value = CommandResult(("oha",), exit_code, stdout, b"diagnostic", 1, error)
    monkeypatch.setattr(base, "ProcessHandle", Mock(return_value=process))
    bencher = OhaBencher()
    bencher.provenance = {"version": "oha 1.16.0"}

    result = bencher.run_once("http://127.0.0.1/", LoadSpec(requests=10), timeout=2)

    assert result.status == expected_status
    assert result.rps == 5
    assert result.raw_stdout == stdout
    assert result.raw_stderr == b"diagnostic"


def test_version_mismatch_is_rejected_before_workload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base.shutil, "which", Mock(return_value="/bin/oha"))
    monkeypatch.setattr(base, "run_command", Mock(return_value="oha 1.15.0"))

    with pytest.raises(BencherException, match="Expected oha 1.16.0"):
        OhaBencher().prepare()

def test_custom_request_argv_and_system_tls() -> None:
    request = RequestSpec(
        method="POST", path="/upload?mode=test", headers=(("Content-Type", "application/json"),), body=Path("body.bin"),
    )

    argv = OhaBencher().build_argv("https://example.test/", LoadSpec(requests=1), request=request)

    assert argv[argv.index("-m") + 1] == "POST"
    assert argv[argv.index("-H") + 1] == "Content-Type: application/json"
    assert argv[argv.index("-D") + 1] == "body.bin"
    assert argv[-1] == "https://example.test/upload?mode=test"
    assert "--cacert" not in argv and "--insecure" not in argv

@pytest.mark.parametrize(("request_spec", "reason"), [
    (RequestSpec(expected_status=201), "status"), (RequestSpec(expected_response_bytes=13), "body size"),
])
def test_wrong_response_is_not_a_success(
    request_spec: RequestSpec, reason: str, datafix_readbin: Callable[[str], bytes],
) -> None:
    sample = parse_oha_report(datafix_readbin("oha/success.json"), LoadSpec(requests=10), request=request_spec)

    assert sample.status == "response_error"
    assert reason in sample.reason
    assert sample.rps == sample.completed_rps == 5
    assert sample.response_body_bytes == 120

def test_run_snapshots_body_and_redacts_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, datafix_readbin: Callable[[str], bytes],
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    seen = []
    stdout = datafix_readbin("oha/success.json")

    def create(argv: list[str]) -> Mock:
        snapshot = Path(argv[argv.index("-D") + 1])
        seen.append(snapshot)
        assert snapshot.read_bytes() == b"payload"
        source.write_bytes(b"changed")
        return Mock(
            argv=tuple(argv), collect_output=Mock(return_value=(stdout, b"Bearer PRIVATE")),
            wait=Mock(return_value=CommandResult(tuple(argv), 0, stdout, b"Bearer PRIVATE", 1)),
        )

    monkeypatch.setattr(base, "ProcessHandle", create)
    bencher = OhaBencher()
    bencher.provenance = {"version": "oha 1.16.0"}
    request = RequestSpec(method="POST", body=source, headers=(("Authorization", "Bearer PRIVATE"),))

    result = bencher.run_once("https://example.test/", LoadSpec(requests=10), timeout=1, request=request)

    assert result.status == "ok"
    assert result.request["body"]["bytes"] == 7
    assert not seen[0].exists()
    assert result.artifacts_redacted
    assert result.raw_stderr == b"[redacted]"
    assert "PRIVATE" not in repr(result.serialize())
