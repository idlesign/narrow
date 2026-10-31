import json
import math
import pickle
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest
from asv_runner.discovery import disc_benchmarks

from narrow import asv
from narrow.artifacts import stats_dump
from narrow.configuration import LoadSpec
from narrow.exceptions import NarrowException
from narrow.processes import ProcessHandle
from narrow.results import SampleResult

@pytest.fixture
def saved_run() -> dict:
    loads = [LoadSpec(clients=clients, requests=100) for clients in (1, 4)]
    attempts = []

    for load in loads:

        for repeat in (1, 2):
            rate = float(load.clients * repeat * 10)
            sample = SampleResult(
                load.clients, status="ok", rps=rate, responses=100, successful_responses=100,
                window_seconds=100 / rate, statuses={"200": 100},
                latency_ms={"p50": repeat * 10, "p95": repeat * 20, "p99": repeat * 30},
            )
            attempts.append({
                **sample.serialize(), "phase": "measurement", "stand": "uwsgi", "app": "py",
                "session": 0, "repeat": repeat, "load": asdict(load),
            })

    attempts.append({**attempts[0], "phase": "warmup", "rps": 9999})
    return {
        "status": "complete", "meta": {"options": {"loads": [asdict(load) for load in loads], "repeats": 2}},
        "attempts": attempts,
        "stands": [{"stand": "uwsgi", "app": "py", "session": 0, "processes": [{
            "cleanup_error": None, "output_truncated": False,
        }]}],
    }

def test_read_medians_from_checkpoint_or_directory(saved_run: dict, tmp_path: Path) -> None:
    stats_dump(saved_run, tmp_path / "run.json")

    result = asv.read_results(tmp_path, stand="uwsgi", app="py")

    assert result[1] == {
        "rps": 15, "p50": 0.015, "p95": 0.03, "p99": 0.045,
        "unsuccessful_fraction": 0, "failed_or_missing_repeats": 0,
    }
    assert result[4]["rps"] == 60
    assert asv.read_results(tmp_path / "run.json", stand="uwsgi", app="py") == result

@pytest.mark.parametrize(("stand", "app"), [("uwsgi", None), ("uwsgi", "flask"), ("unknown", "py")])
def test_require_explicit_saved_stand_and_application(saved_run: dict, tmp_path: Path, stand: str, app: str | None) -> None:
    stats_dump(saved_run, tmp_path / "run.json")

    with pytest.raises(NarrowException, match="No saved stand/application"):
        asv.read_results(tmp_path, stand=stand, app=app)

@pytest.mark.parametrize(("changes", "fraction", "latency"), [
    ({"responses": 90, "successful_responses": 80, "errors": {"timeout": 10}, "rps": 80}, 0.2, 0.03),
    ({"responses": 0, "successful_responses": 0, "errors": {"timeout": 100}, "rps": 0,
      "latency_ms": {"p50": None, "p95": None, "p99": None}}, 1.0, math.nan),
])
def test_http_and_transport_outcomes_remain_visible(
    saved_run: dict, tmp_path: Path, changes: dict, fraction: float, latency: float,
) -> None:

    for attempt in saved_run["attempts"][:2]:
        attempt.update(changes, status="http_error")

    saved_run["status"] = "failed"
    stats_dump(saved_run, tmp_path / "run.json")

    result = asv.read_results(tmp_path, stand="uwsgi", app="py")[1]

    assert result["rps"] == changes["rps"]
    assert result["unsuccessful_fraction"] == fraction
    assert result["failed_or_missing_repeats"] == 2
    assert result["p95"] == pytest.approx(latency, nan_ok=True)

@pytest.mark.parametrize("changes", [{"status": "error"}, {"cleanup_error": "generator still alive"}])
def test_failed_repeat_is_not_dropped_from_summary(saved_run: dict, tmp_path: Path, changes: dict) -> None:
    saved_run["attempts"][1].update(changes)
    saved_run["status"] = "failed"
    stats_dump(saved_run, tmp_path / "run.json")

    result = asv.read_results(tmp_path, stand="uwsgi", app="py")

    assert math.isnan(result[1]["rps"])
    assert math.isnan(result[1]["p99"])
    assert result[1]["failed_or_missing_repeats"] == 1
    assert result[4]["rps"] == 60

def test_missing_repeat_is_a_gap_not_a_partial_median(saved_run: dict, tmp_path: Path) -> None:
    saved_run["attempts"].pop(1)
    saved_run["status"] = "cancelled"
    stats_dump(saved_run, tmp_path / "run.json")

    result = asv.read_results(tmp_path, stand="uwsgi", app="py")[1]

    assert math.isnan(result["rps"])
    assert result["failed_or_missing_repeats"] == 1

@pytest.mark.parametrize("changes", [{"cleanup_error": "cannot stop"}, {"output_truncated": True}])
def test_failed_session_excludes_performance(saved_run: dict, tmp_path: Path, changes: dict) -> None:
    saved_run["stands"][0]["processes"][0].update(changes)
    stats_dump(saved_run, tmp_path / "run.json")

    result = asv.read_results(tmp_path, stand="uwsgi", app="py")[1]

    assert math.isnan(result["rps"])
    assert math.isnan(result["unsuccessful_fraction"])
    assert result["failed_or_missing_repeats"] == 2

@pytest.mark.parametrize(("field", "value", "message"), [
    ("rps", True, "Invalid saved metric"),
    ("rps", -1, "Invalid saved metric"),
    ("rps", math.inf, "Invalid saved metric"),
    ("rps", math.nan, "Invalid saved metric"),
    ("rps", 10**1000, "Malformed narrow checkpoint"),
    ("latency_ms", [], "Invalid saved latency"),
    ("latency_ms", {"p95": -1}, "Invalid saved metric"),
    ("successful_responses", 101, "Invalid saved outcome"),
    ("errors", {"timeout": True}, "Invalid saved outcome"),
    ("latency_population", "different clock", "Mixed saved latency"),
    ("repeat", 2, "Invalid or duplicate saved repeat"),
    ("session", 7, "Missing saved stand session"),
    ("load", asdict(LoadSpec(clients=1, requests=100, qps=10)), "declared load policy"),
])
def test_reject_malformed_or_mixed_attempts(
    saved_run: dict, tmp_path: Path, field: str, value: object, message: str,
) -> None:
    saved_run["attempts"][0][field] = value
    (tmp_path / "run.json").write_text(json.dumps(saved_run), encoding="utf-8")

    with pytest.raises(NarrowException, match=message):
        asv.read_results(tmp_path, stand="uwsgi", app="py")

def test_reject_multiple_policies_for_same_clients(saved_run: dict, tmp_path: Path) -> None:
    saved_run["meta"]["options"]["loads"].append(asdict(LoadSpec(clients=1, requests=100, qps=10)))
    stats_dump(saved_run, tmp_path / "run.json")

    with pytest.raises(NarrowException, match="Ambiguous saved concurrency"):
        asv.read_results(tmp_path, stand="uwsgi", app="py")

def test_reject_running_checkpoint(saved_run: dict, tmp_path: Path) -> None:
    saved_run["status"] = "running"
    stats_dump(saved_run, tmp_path / "run.json")

    with pytest.raises(NarrowException, match="terminal narrow checkpoint"):
        asv.read_results(tmp_path, stand="uwsgi", app="py")

@pytest.mark.parametrize(("ownership", "expected"), [("external", 15), ("owned", math.nan)])
def test_external_endpoint_does_not_require_fake_process_evidence(
    saved_run: dict, tmp_path: Path, ownership: str, expected: float,
) -> None:
    saved_run["stands"][0].update(ownership=ownership, processes=[])
    stats_dump(saved_run, tmp_path / "run.json")

    result = asv.read_results(tmp_path, stand="uwsgi", app="py")

    assert result[1]["rps"] == pytest.approx(expected, nan_ok=True)

def test_asv_rejects_mixed_request_bodies_or_methods(saved_run: dict, tmp_path: Path) -> None:
    saved_run["attempts"][0]["request"] = {"method": "POST", "body": {"sha256": "different"}}
    stats_dump(saved_run, tmp_path / "run.json")

    with pytest.raises(NarrowException, match="Mixed saved HTTP requests"):
        asv.read_results(tmp_path, stand="uwsgi", app="py")

def test_asv_discovers_tracks_and_reuses_one_report(
    saved_run: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, datafix_read: Callable[[str], str],
) -> None:
    suite = tmp_path / "narrow_asv_suite"
    suite.mkdir()
    (suite / "__init__.py").write_text(datafix_read("asv_suite.py"), encoding="utf-8")
    stats_dump(saved_run, suite / "run.json")
    monkeypatch.syspath_prepend(f"{tmp_path}")
    monkeypatch.chdir(tmp_path)
    read_report = Mock(wraps=asv.stats_load)
    monkeypatch.setattr(asv, "stats_load", read_report)
    start_process = Mock(side_effect=AssertionError("ASV tracks must not start processes"))
    monkeypatch.setattr(ProcessHandle, "start", start_process)

    benchmarks = list(disc_benchmarks(f"{suite}"))

    assert len(benchmarks) == 6
    assert len({benchmark.setup_cache_key for benchmark in benchmarks}) == 1
    assert {benchmark.pretty_name for benchmark in benchmarks} >= {
        "HTTP 2xx throughput (median of attempts)", "Latency p95 seconds (median of attempt p95; not pooled)",
    }
    read_report.assert_not_called()

    cache = pickle.loads(pickle.dumps(benchmarks[0].do_setup_cache()))
    observed = {}

    for benchmark in benchmarks:
        assert benchmark.type == "track"
        assert benchmark.param_names == ["clients"]
        benchmark.set_cache(cache)
        values = []

        for param_idx in (0, 1):
            benchmark.set_param_idx(param_idx)
            benchmark.do_setup()
            values.append(benchmark.do_run())
            benchmark.do_teardown()

        observed[benchmark.name] = (benchmark.unit, values)

    assert observed == {
        "Http.track_successful_responses_per_second": ("responses/s", [15, 60]),
        "Http.track_latency_p50_seconds": ("seconds", [0.015, 0.015]),
        "Http.track_latency_p95_seconds": ("seconds", [0.03, 0.03]),
        "Http.track_latency_p99_seconds": ("seconds", [0.045, 0.045]),
        "Http.track_unsuccessful_outcome_fraction": ("fraction", [0, 0]),
        "Http.track_failed_or_missing_repeats": ("attempts", [0, 0]),
    }
    read_report.assert_called_once_with(suite / "run.json")
    start_process.assert_not_called()
