import json
import hashlib
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow import APPS, NarrowException, SubjectSpec, runner, stats_load
from narrow.configuration import LoadSpec
from narrow.requests import RequestSpec
from narrow.processes.limits import ProcessLimits
from narrow.reports.results import SampleResult
from narrow.reports.artifacts import RunStore
from narrow.runner import Runner, RunOptions
from narrow.targets import UrlTarget
from narrow.reports.asv import read_results
from narrow.measurement.conditions import MeasurementConditions, read_conditions, require_known_conditions


def test_prepared_series_shares_phases_repeats_and_checkpoints_without_target_acquisition(tmp_path: Path) -> None:
    conditions = MeasurementConditions(
        workload={"loads": [asdict(LoadSpec(requests=2, clients=clients)) for clients in (1, 2)]},
        tools={}, environment={}, policy={"repeats": 2, "warmup_requests": 3},
    ).serialize()
    store = RunStore(tmp_path / "prepared", {})
    store.prepare()
    clock = [0.0]
    calls = []

    def measure(load: LoadSpec, phase: str, repeat: int, timeout: float) -> SampleResult:
        calls.append((load.clients, phase, repeat, load.requests, timeout))
        clock[0] += 2
        return SampleResult(load.clients, status="ok", rps=10, raw_stdout=b"saved attempt")

    Runner.run_series(
        store, identity={"stand": "owned-by-caller", "app": None, "session": 0}, conditions=conditions,
        attempt_timeout=9, remaining=lambda: 18 - clock[0], measure=measure,
    )

    assert calls == [
        (1, "warmup", 0, 3, 9), (1, "measurement", 1, 2, 9), (1, "measurement", 2, 2, 9),
        (2, "warmup", 0, 3, 9), (2, "measurement", 1, 2, 9), (2, "measurement", 2, 2, 8),
    ]
    assert len(stats_load(store.path)["attempts"]) == 6
    assert (store.path / "attempt-0006/stdout.bin").read_bytes() == b"saved attempt"
    assert {attempt["conditions_fingerprint"] for attempt in store.data["attempts"]} == {conditions["fingerprint"]}


@pytest.mark.parametrize(("status", "error", "message"), [
    ("cancelled", KeyboardInterrupt, "interrupted"),
    ("error", NarrowException, "synthetic failure"),
    ("invalid", NarrowException, "synthetic failure"),
])
def test_prepared_series_keeps_failed_attempt_and_does_not_repeat(
    tmp_path: Path, status: str, error: type[BaseException], message: str,
) -> None:
    conditions = MeasurementConditions(
        workload={"loads": [asdict(LoadSpec(requests=1))]}, tools={}, environment={},
        policy={"repeats": 2, "warmup_requests": 0},
    ).serialize()
    store = RunStore(tmp_path / "prepared", {})
    store.prepare()
    measure = Mock(return_value=SampleResult(1, status=status, reason="synthetic failure", raw_stdout=b"partial"))

    with pytest.raises(error, match=message):
        Runner.run_series(
            store, identity={}, conditions=conditions, attempt_timeout=30, remaining=lambda: 30, measure=measure,
        )

    measure.assert_called_once()
    assert stats_load(store.path)["attempts"][0]["status"] == status
    assert (store.path / "attempt-0001/stdout.bin").read_bytes() == b"partial"


@pytest.mark.parametrize(("loads", "repeats", "warmup_requests", "timeout", "message"), [
    ([{"requests": 1}], 0, 0, 30, "repeat/warmup policy"),
    ([{"requests": 1}], True, 0, 30, "repeat/warmup policy"),
    ([{"requests": 1}], 1, -1, 30, "repeat/warmup policy"),
    ([], 1, 0, 30, "distinct concurrency points"),
    ([{"requests": 1}, {"requests": 2}], 1, 0, 30, "distinct concurrency points"),
    ([{}], 1, 0, 30, "exactly one"),
    ([{"requests": 1}], 1, 0, float("nan"), "finite and positive"),
    ([{"duration": 30}], 1, 0, 30, "cover duration"),
])
def test_prepared_series_refuses_invalid_plan_before_invoking_generator(
    tmp_path: Path, loads: list[dict], repeats: int, warmup_requests: int, timeout: float, message: str,
) -> None:
    conditions = MeasurementConditions(
        workload={"loads": loads}, tools={}, environment={},
        policy={"repeats": repeats, "warmup_requests": warmup_requests},
    ).serialize()
    measure = Mock()

    with pytest.raises(ValueError, match=message):
        Runner.run_series(
            RunStore(tmp_path / "not-created", {}), identity={}, conditions=conditions,
            attempt_timeout=timeout, remaining=lambda: 30, measure=measure,
        )

    measure.assert_not_called()
    assert not (tmp_path / "not-created").exists()


@pytest.mark.parametrize("stand_alias", ["nginx_static", "nginx_ssl_static"])
def test_warmup_repeats_and_static_deduplication(
    stand_alias: str, tmp_path: Path, run_environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(APPS["flask"], "get_version", Mock(side_effect=AssertionError("unused application")))
    options = RunOptions(
        stands=(stand_alias,), loads=(LoadSpec(requests=2), LoadSpec(requests=2, clients=2)),
        apps=("py", "flask"), repeats=2, warmup_requests=3, budget=35,
        generator_limits=ProcessLimits(cpus=(2,), address_space_bytes=1024**3),
    )
    measure = run_environment.bencher.run_once.side_effect

    def finish_cleanup() -> None:
        run_environment.clock[0] += 4

    def finish_readiness(*arguments: object, **parameters: object) -> None:
        run_environment.clock[0] += 3
        run_environment.handles[0].stop.side_effect = finish_cleanup

    def timed_sample(
        url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits,
    ) -> SampleResult:
        run_environment.clock[0] += 2
        return measure(url, load, timeout=timeout, request=request, limits=limits)

    run_environment.ready.side_effect = finish_readiness
    run_environment.bencher.run_once.side_effect = timed_sample

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
    assert [call.kwargs["timeout"] for call in run_environment.bencher.run_once.call_args_list] == [17, 15, 13, 11, 9, 7]
    assert result["meta"]["budget_elapsed_including_cleanup_seconds"] == 19
    assert json.loads((tmp_path / "run/budget.json").read_text()) == result["meta"]["budget"]


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


def test_checkpoint_io_error_does_not_hide_generator_cancellation_or_skip_cleanup(
    tmp_path: Path, run_environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=1),))
    run_environment.bencher.run_once.side_effect = [SampleResult(1, status="cancelled", raw_stdout=b"partial")]
    monkeypatch.setattr(RunStore, "save_attempt", Mock(side_effect=OSError("synthetic checkpoint failure")))

    with pytest.raises(KeyboardInterrupt, match="interrupted") as caught:
        Runner(options).run(tmp_path / "run")

    assert isinstance(caught.value.__cause__, OSError)
    assert "Cancelled attempt checkpoint could not be saved" in caught.value.__notes__
    saved = stats_load(tmp_path / "run")
    assert saved["status"] == "cancelled" and saved["attempts"] == []
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
    tmp_path: Path, run_environment: SimpleNamespace,
) -> None:
    clock = run_environment.clock
    options = RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=2),), repeats=2, budget=35)

    def use_budget(url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits) -> SampleResult:
        clock[0] += timeout
        return SampleResult(1, status="ok", rps=10)

    run_environment.bencher.run_once.side_effect = use_budget

    with pytest.raises(NarrowException, match="budget exhausted"):
        Runner(options).run(tmp_path / "run")

    assert len(stats_load(tmp_path / "run")["attempts"]) == 1
    assert run_environment.bencher.run_once.call_count == 1

def test_shared_budget_keeps_one_deadline_without_resuming_output(
    tmp_path: Path, run_environment: SimpleNamespace,
) -> None:
    shared = tmp_path / "shared-budget.json"
    options = RunOptions(
        targets=(UrlTarget("http://example.test/"),), loads=(LoadSpec(requests=1),), budget=50, budget_file=shared,
    )

    def timed_sample(
        url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits,
    ) -> SampleResult:
        run_environment.clock[0] += 4
        return SampleResult(1, status="ok", rps=10)

    run_environment.bencher.run_once.side_effect = timed_sample
    first = Runner(options).run(tmp_path / "first")
    original = shared.read_bytes()

    with pytest.raises(FileExistsError):
        Runner(options).run(tmp_path / "first")

    run_environment.clock[0] += 6
    second = Runner(options).run(tmp_path / "second")

    assert first["meta"]["budget"] == second["meta"]["budget"]
    assert shared.read_bytes() == original
    assert [call.kwargs["timeout"] for call in run_environment.bencher.run_once.call_args_list] == [30, 25]
    assert first["meta"]["budget_elapsed_including_cleanup_seconds"] == 4
    assert second["meta"]["budget_elapsed_including_cleanup_seconds"] == 14
    assert not (tmp_path / "first/budget.json").exists() and not (tmp_path / "second/budget.json").exists()

@pytest.mark.parametrize(("failure", "error", "message"), [
    ("expired", NarrowException, "Insufficient run budget"),
    ("malformed", ValueError, "invalid saved budget JSON"),
])
def test_shared_budget_fails_closed_before_starting_the_next_target(
    tmp_path: Path, run_environment: SimpleNamespace, failure: str, error: type[Exception], message: str,
) -> None:
    shared = tmp_path / "shared-budget.json"
    options = RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=1),), budget=35, budget_file=shared)
    Runner(options).run(tmp_path / "first")

    if failure == "expired":
        run_environment.clock[0] = 40

    else:
        shared.write_text("{", encoding="utf-8")

    original = shared.read_bytes()

    with pytest.raises(error, match=message):
        Runner(options).run(tmp_path / "second")

    assert shared.read_bytes() == original
    assert len(run_environment.handles) == run_environment.bencher.run_once.call_count == 1
    saved = stats_load(tmp_path / "second")
    assert saved["status"] == "failed" and saved["attempts"] == []

def test_budget_expiry_in_cleanup_preserves_attempt_but_fails_run(
    tmp_path: Path, run_environment: SimpleNamespace,
) -> None:
    options = RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=1),), budget=35)

    def expire_in_cleanup(
        url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits,
    ) -> SampleResult:

        def finish_cleanup() -> None:
            run_environment.clock[0] = 36

        run_environment.handles[0].stop.side_effect = finish_cleanup
        return SampleResult(1, status="ok", rps=10)

    run_environment.bencher.run_once.side_effect = expire_in_cleanup

    with pytest.raises(NarrowException, match="budget exhausted during cleanup"):
        Runner(options).run(tmp_path / "run")

    saved = stats_load(tmp_path / "run")
    assert saved["status"] == "failed" and saved["attempts"][0]["status"] == "ok"
    assert saved["meta"]["budget_elapsed_including_cleanup_seconds"] == 36
    run_environment.handles[0].stop.assert_called_once()

@pytest.mark.parametrize("parameters", [
    {"repeats": 0}, {"budget": float("nan")}, {"attempt_timeout": -1}, {"warmup_requests": True},
    {"generator_limits": {}}, {"budget_file": "not a Path"},
    {"workload_files": {"scenario": "not a Path"}}, {"target_settings": []},
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


def test_conditions_are_frozen_and_saved_before_any_workload(
    tmp_path: Path, run_environment: SimpleNamespace,
) -> None:
    scenario = tmp_path / "scenario.py"
    scenario.write_bytes(b"private independent workload bytes")
    subjects = (SubjectSpec("server", artifact_sha256={"runtime": "a" * 64}), SubjectSpec("server", artifact_sha256={"runtime": "b" * 64}))
    options = RunOptions(
        targets=tuple(UrlTarget(f"http://host-{index}.test/api?token=PRIVATE", name=f"server-{index}", subject=subject)
                      for index, subject in enumerate(subjects)),
        loads=(LoadSpec(requests=2),), workload_files={"scenario": scenario}, target_settings={"workers": 1},
    )
    measure = run_environment.bencher.run_once.side_effect
    preflight = []

    def record_preflight(
        url: str, load: LoadSpec, *, timeout: float, request: RequestSpec, limits: ProcessLimits,
    ) -> SampleResult:
        preflight.append(stats_load(tmp_path / "run")["meta"]["measurement"]["conditions"])
        scenario.write_bytes(b"later edits must not change saved conditions")
        return measure(url, load, timeout=timeout, request=request, limits=limits)

    run_environment.bencher.run_once.side_effect = record_preflight
    result = Runner(options).run(tmp_path / "run")
    conditions = [require_known_conditions(stand["conditions"]) for stand in result["stands"]]

    assert conditions[0] == conditions[1]
    assert preflight == [conditions, conditions]
    assert conditions[0]["workload"]["artifacts"] == {"scenario": hashlib.sha256(b"private independent workload bytes").hexdigest()}
    assert all(attempt["conditions_fingerprint"] == conditions[0]["fingerprint"] for attempt in result["attempts"])
    assert "PRIVATE" not in json.dumps(result) and "private independent workload bytes" not in json.dumps(result)
    assert not list((tmp_path / "run").rglob("scenario.py"))
    assert result["stands"][0]["host_observations"]["scope"].startswith("target_lifecycle")


def test_unknown_custom_workload_can_run_but_cannot_claim_comparability(
    tmp_path: Path, run_environment: SimpleNamespace,
) -> None:
    result = Runner(RunOptions(targets=(UrlTarget("http://example.test/"),), loads=(LoadSpec(requests=2),))).run(tmp_path / "run")
    saved = read_conditions(result["stands"][0]["conditions"])

    assert result["status"] == "complete" and saved["workload"]["artifacts"] == {}

    with pytest.raises(ValueError, match="Unknown"):
        require_known_conditions(saved)


def test_missing_workload_input_fails_before_starting_a_target(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    options = RunOptions(
        stands=("nginx_static",), loads=(LoadSpec(requests=2),), workload_files={"scenario": tmp_path / "missing"},
    )

    with pytest.raises(FileNotFoundError):
        Runner(options).run(tmp_path / "run")

    assert stats_load(tmp_path / "run")["status"] == "failed"
    assert run_environment.handles == []
    run_environment.bencher.run_once.assert_not_called()


def test_builtin_ca_is_frozen_before_budget_and_target_setup(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    result = Runner(RunOptions(stands=("nginx_ssl_static",), loads=(LoadSpec(requests=2),))).run(tmp_path / "run")
    conditions = require_known_conditions(result["stands"][0]["conditions"])
    frozen = tmp_path / "run/inputs/target-0001/ca.pem"

    assert conditions["workload"]["http"]["ca_file"]["sha256"] == hashlib.sha256(frozen.read_bytes()).hexdigest()
    assert run_environment.bencher.run_once.call_args.kwargs["request"].ca_file == frozen
    assert run_environment.ready.call_args.kwargs["ca_file"] == frozen


def test_builtin_settings_cannot_be_falsified_by_declarations(tmp_path: Path, run_environment: SimpleNamespace) -> None:
    options = RunOptions(stands=("nginx_static",), loads=(LoadSpec(requests=2),), target_settings={"workers": 99})

    with pytest.raises(ValueError, match="conflict with actual"):
        Runner(options).run(tmp_path / "run")

    assert run_environment.handles == []
    run_environment.bencher.run_once.assert_not_called()
