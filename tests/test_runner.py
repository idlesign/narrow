from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow import APPS, NarrowException, SubjectSpec, runner, stats_load
from narrow.configuration import LoadSpec
from narrow.requests import RequestSpec
from narrow.processes.limits import ProcessLimits
from narrow.reports.results import SampleResult
from narrow.runner import Runner, RunOptions
from narrow.targets import UrlTarget
from narrow.reports.asv import read_results


@pytest.mark.parametrize("stand_alias", ["nginx_static", "nginx_ssl_static"])
def test_warmup_repeats_and_static_deduplication(
    stand_alias: str, tmp_path: Path, run_environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(APPS["flask"], "get_version", Mock(side_effect=AssertionError("unused application")))
    options = RunOptions(
        stands=(stand_alias,), loads=(LoadSpec(requests=2), LoadSpec(requests=2, clients=2)),
        apps=("py", "flask"), repeats=2, warmup_requests=3,
        generator_limits=ProcessLimits(cpus=(2,), address_space_bytes=1024**3),
    )

    result = Runner(options).run(tmp_path / "run")

    assert result["status"] == "complete"
    assert [attempt["phase"] for attempt in result["attempts"]] == [
        "warmup", "measurement", "measurement", "warmup", "measurement", "measurement",
    ]
    assert len(result["stands"]) == 1
    assert all(attempt["app"] is None for attempt in result["attempts"])
    assert result["meta"]["apps"] == {}
    assert run_environment.ready.call_count == 1
    assert len(stats_load(tmp_path / "run")["attempts"]) == 6
    run_environment.handles[0].stop.assert_called_once()
    assert result["meta"]["options"]["generator_limits"] == options.generator_limits.serialize()
    assert all(call.kwargs["limits"] is options.generator_limits for call in run_environment.bencher.run_once.call_args_list)


def test_interrupt_keeps_previous_and_cancelled_attempts(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    options = RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=2),), repeats=2)
    run_environment.bencher.run_once.side_effect = [
        SampleResult(1, status="ok", rps=10), SampleResult(1, status="cancelled", raw_stdout=b"partial"),
    ]

    with pytest.raises(KeyboardInterrupt):
        Runner(options).run(tmp_path / "run")

    loaded = stats_load(tmp_path / "run")
    assert loaded["status"] == "cancelled"
    assert len(loaded["attempts"]) == 2
    assert (tmp_path / "run/attempt-0002/stdout.bin").read_bytes() == b"partial"
    run_environment.handles[0].stop.assert_called_once()


def test_readiness_failure_is_checkpointed(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    options = RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=2),))
    run_environment.ready.side_effect = NarrowException("not ready")

    with pytest.raises(NarrowException, match="not ready"):
        Runner(options).run(tmp_path / "run")

    loaded = stats_load(tmp_path / "run")
    assert loaded["status"] == "failed"
    assert loaded["attempts"] == []
    assert len(loaded["stands"]) == 1
    run_environment.bencher.run_once.assert_not_called()
    run_environment.handles[0].stop.assert_called_once()


def test_cleanup_failure_stops_before_next_topology(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    options = RunOptions(stands=("nginx_static", "uwsgi"), loads=(LoadSpec(requests=2),))

    def fail_cleanup(url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits) -> SampleResult:
        run_environment.handles[0].stop.side_effect = NarrowException("cannot stop")
        return SampleResult(1, status="ok", rps=10)

    run_environment.bencher.run_once.side_effect = fail_cleanup

    with pytest.raises(NarrowException, match="cannot stop"):
        Runner(options).run(tmp_path / "run")

    loaded = stats_load(tmp_path / "run")
    assert len(run_environment.handles) == 1
    assert loaded["stands"][0]["processes"][0]["cleanup_error"] == "cannot stop"
    assert loaded["attempts"][0]["rps"] == 10


def test_unknown_alias_fails_before_creating_output(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    options = RunOptions(stands=("unknown",), loads=(LoadSpec(requests=2),))

    with pytest.raises(NarrowException, match="Unknown stand"):
        Runner(options).run(tmp_path / "run")

    assert not (tmp_path / "run").exists()
    assert run_environment.handles == []
    run_environment.bencher.prepare.assert_not_called()


def test_budget_exhaustion_does_not_start_next_attempt(
    tmp_path: Path, run_environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(runner, "monotonic", lambda: clock[0])
    options = RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=2),), repeats=2, budget=35)

    def use_budget(url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits) -> SampleResult:
        clock[0] += timeout
        return SampleResult(1, status="ok", rps=10)

    run_environment.bencher.run_once.side_effect = use_budget

    with pytest.raises(NarrowException, match="budget exhausted"):
        Runner(options).run(tmp_path / "run")

    assert len(stats_load(tmp_path / "run")["attempts"]) == 1
    assert run_environment.bencher.run_once.call_count == 1


@pytest.mark.parametrize("parameters", [
    {"repeats": 0}, {"budget": float("nan")}, {"attempt_timeout": -1}, {"warmup_requests": True},
    {"generator_limits": {}},
])
def test_invalid_run_options(parameters: dict) -> None:

    with pytest.raises(ValueError, match="Invalid|must be"):
        RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=2),), **parameters)

def test_external_url_uses_runner_and_asv_without_server_ownership(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    options = RunOptions(targets=(UrlTarget("https://example.test/api"),), loads=(LoadSpec(requests=2),))

    result = Runner(options).run(tmp_path / "run")

    assert result["status"] == "complete"
    assert result["stands"][0]["ownership"] == "external"
    assert result["stands"][0]["processes"] == []
    assert result["meta"]["servers"] == {} and result["meta"]["server_settings"] is None
    assert read_results(tmp_path / "run", stand="custom")[1]["rps"] == 10
    assert run_environment.handles == []
    run_environment.ready.assert_not_called()

def test_command_and_builtin_share_one_executor(
    tmp_path: Path, run_environment: SimpleNamespace, command_environment: SimpleNamespace,
) -> None:
    options = RunOptions(
        stands=("nginx_static",), targets=(command_environment.target,), loads=(LoadSpec(requests=2),),
    )

    result = Runner(options).run(tmp_path / "run")

    assert result["status"] == "complete"
    assert [attempt["stand"] for attempt in result["attempts"]] == ["nginx_static", "custom"]
    assert result["stands"][1]["processes"][0]["readiness"]["attempts"] == 1
    assert (tmp_path / "run/stand-0002/custom-stderr.bin").read_bytes() == b"[redacted]"
    command_environment.server.stop.assert_called_once()
    run_environment.handles[0].stop.assert_called_once()

def test_runner_freezes_one_body_for_all_repeats(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    source = tmp_path / "payload"
    source.write_bytes(b"original")
    received = []

    def measure(url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits) -> SampleResult:
        received.append(request.body.read_bytes())
        source.write_bytes(b"changed")
        return SampleResult(load.clients, status="ok")

    run_environment.bencher.run_once.side_effect = measure
    options = RunOptions(
        targets=(UrlTarget("http://example.test/"),), loads=(LoadSpec(requests=1),), repeats=2,
        request=RequestSpec(method="POST", body=source),
    )

    result = Runner(options).run(tmp_path / "run")

    assert received == [b"original", b"original"]
    assert result["meta"]["request"]["body"]["bytes"] == 8
    assert (tmp_path / "run/inputs/body.bin").read_bytes() == b"original"

def test_bad_input_is_checkpointed_before_any_target_starts(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    options = RunOptions(
        targets=(UrlTarget("http://example.test/"),), loads=(LoadSpec(requests=1),), request=RequestSpec(body=tmp_path / "missing"),
    )

    with pytest.raises(FileNotFoundError, match="missing"):
        Runner(options).run(tmp_path / "run")

    assert stats_load(tmp_path / "run")["status"] == "failed"
    run_environment.bencher.run_once.assert_not_called()
    assert run_environment.handles == []

def test_same_named_revisions_share_executor_but_never_pool_asv_metrics(
    tmp_path: Path, run_environment: SimpleNamespace,
) -> None:
    subjects = tuple(SubjectSpec("server", "unchanged-version", {"runtime": checksum * 64}) for checksum in ("a", "b"))
    options = RunOptions(
        targets=tuple(UrlTarget("http://example.test/", name="server", subject=subject) for subject in subjects),
        loads=(LoadSpec(requests=2),),
    )
    run_environment.bencher.run_once.side_effect = [
        SampleResult(1, status="ok", rps=rate, responses=2, successful_responses=2, latency_ms={"p99": rate})
        for rate in (10.0, 20.0)
    ]

    result = Runner(options).run(tmp_path / "run")

    assert result["status"] == "complete"
    assert [target["subject"] for target in result["meta"]["targets"]] == [subject.serialize() for subject in subjects]
    assert [target["subject"] for target in result["stands"]] == [subject.serialize() for subject in subjects]
    assert [attempt["subject"] for attempt in result["attempts"]] == [subject.serialize() for subject in subjects]

    with pytest.raises(NarrowException, match="Different saved subjects"):
        read_results(tmp_path / "run", stand="server")

    assert [
        read_results(tmp_path / "run", stand="server", subject=subject.serialize()["fingerprint"])[1]["rps"]
        for subject in subjects
    ] == [10, 20]
    assert run_environment.handles == []

def test_same_named_same_subject_is_rejected_before_starting(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    subject = SubjectSpec("server", "revision")
    options = RunOptions(
        targets=(UrlTarget("http://example.test/", subject=subject), UrlTarget("http://other.test/", subject=subject)),
        loads=(LoadSpec(requests=2),),
    )

    with pytest.raises(NarrowException, match="Duplicate target"):
        Runner(options).run(tmp_path / "run")

    run_environment.bencher.prepare.assert_not_called()
    assert not (tmp_path / "run").exists()
