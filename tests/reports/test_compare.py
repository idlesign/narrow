import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from narrow import NarrowException, SubjectSpec, cli
from narrow.configuration import LoadSpec
from narrow.measurement import identity, system
from narrow.measurement.conditions import MeasurementConditions
from narrow.processes import ProcessHandle
from narrow.reports import asv
from narrow.reports.artifacts import stats_dump
from narrow.reports.compare import RunSelection, compare_runs
from narrow.reports.results import SampleResult
from narrow.requests import RequestSpec

@pytest.fixture
def saved_comparison(tmp_path: Path, measurement_sections: dict) -> Callable:
    template = deepcopy(measurement_sections)
    template["workload"]["loads"] = [asdict(LoadSpec(clients=clients, requests=2)) for clients in (1, 4)]
    template["policy"].update(repeats=2, warmup_requests=3)

    def save(
        name: str, *, scale: float = 1, platform: str = "server", checksum: str = "e", sections: dict | None = None,
    ) -> tuple[Path, dict]:
        conditions = MeasurementConditions(**(sections or template)).serialize()
        subject = SubjectSpec(platform, "unchanged-label", {"runtime": checksum * 64}).serialize()
        request = {
            **RequestSpec().serialize(), "url": f"http://{name}.test/api", "scheme": "http",
            "path_query_sha256": conditions["workload"]["http"]["path_query_sha256"],
        }
        execution = {"limits": {
            "requested": conditions["environment"]["generator_limits"], "error": None,
            "applied": {
                "cpus": [1], "core_dump_bytes": 0, "address_space_bytes": None, "address_space_hard_bytes": None,
                "file_size_bytes": None, "file_size_hard_bytes": None,
            },
        }, "resources": {"scope": "generator_process_lifetime", "cpu_seconds": 9}}
        attempts = []

        for values in conditions["workload"]["loads"]:
            load = LoadSpec(**values)

            for repeat in range(1, conditions["policy"]["repeats"] + 1):
                sample = SampleResult(
                    load.clients, status="ok", rps=10 * load.clients * repeat * scale,
                    responses=2, successful_responses=2, statuses={"200": 2}, request=deepcopy(request),
                    latency_ms={"p50": 10 * repeat / scale, "p95": 20 * repeat / scale, "p99": 30 * repeat / scale},
                    execution=deepcopy(execution),
                )
                attempts.append({
                    **sample.serialize(), "phase": "measurement", "stand": "server", "app": "py", "session": 0,
                    "repeat": repeat, "load": values, "subject": subject, "conditions_fingerprint": conditions["fingerprint"],
                    "artifacts": f"{name}/raw-{load.clients}-{repeat}",
                })

        attempts.append({**attempts[0], "phase": "warmup", "rps": 9999, "load": asdict(LoadSpec(requests=3))})
        data = {
            "status": "complete", "meta": {
                "options": {
                    "loads": conditions["workload"]["loads"], "repeats": conditions["policy"]["repeats"],
                    "warmup_requests": 3, "bencher": "oha", "generator_limits": conditions["environment"]["generator_limits"],
                },
                "generator": {key: conditions["tools"]["generator"][key] for key in ("version", "sha256")},
                "measurement": {
                    "controller": conditions["tools"]["controller"], "machine": conditions["environment"]["machine"],
                    "conditions": [conditions],
                },
            },
            "stands": [{
                "stand": "server", "app": "py", "session": 0, "subject": subject, "conditions": conditions,
                "processes": [{"cleanup_error": None, "output_truncated": False}],
            }], "attempts": attempts,
        }
        source = tmp_path / name
        source.mkdir()
        stats_dump(data, source / "run.json")
        return source, data

    return save

def test_compare_revisions_by_bytes_without_pooling_independent_runs(
    saved_comparison: Callable, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, first_data = saved_comparison("first")
    second, _ = saved_comparison("second", scale=2, checksum="f")
    third, _ = saved_comparison("third", scale=4, checksum="f")
    original = first.joinpath("run.json").read_bytes()
    reread = Mock(side_effect=AssertionError("ASV aggregation must use the selected in-memory snapshot"))
    monkeypatch.setattr(asv, "stats_load", reread)

    report = compare_runs([first, second, third], factor="revision")
    data = report.serialize()
    figure = report.create_figure()

    assert data["baseline"] == "R1" and len(data["series"]) == 3
    assert data["series"][0]["subject"]["revision"] == data["series"][1]["subject"]["revision"] == "unchanged-label"
    assert data["series"][1]["subject"] == data["series"][2]["subject"]
    assert [trace.y for trace in figure.data if trace.showlegend] == [(15, 60), (30, 120), (60, 240)]
    assert figure.data[0].customdata == ([2, 2], [2, 2])
    assert figure.data[12].y == (15, 15)
    row = next(row for row in data["rows"] if (row["series"], row["clients"], row["metric"]) == ("R2", 1, "rps"))
    assert row["baseline"] == row["delta"] == 15 and row["delta_percent"] == 100 and row["change"] == "improved"
    assert row["higher_is_better"] is True and "higher is better" in row["label"]
    assert all(row["higher_is_better"] is False for row in data["rows"] if row["metric"] == "p95")
    assert first.joinpath("run.json").read_bytes() == original
    assert data["series"][0]["attempts"] == first_data["attempts"]
    assert "server CPU" not in {row["label"] for row in data["rows"]}
    reread.assert_not_called()

def test_platform_selection_does_not_compare_aliases_origins_or_local_paths(saved_comparison: Callable) -> None:
    first, _ = saved_comparison("first", platform="alpha")
    second, data = saved_comparison("second", platform="beta", checksum="f")
    data["stands"][0]["stand"] = "different-alias"

    for attempt in data["attempts"]:
        attempt["stand"] = "different-alias"

    stats_dump(data, second / "run.json")

    report = compare_runs([
        RunSelection(first, stand="server", app="py"), RunSelection(second, stand="different-alias"),
    ], factor="platform")

    assert len(report.serialize()["rows"]) == 24

    with pytest.raises(NarrowException, match="one platform"):
        compare_runs([first, second], factor="revision")

@pytest.mark.parametrize(("path", "value", "message"), [
    (("workload", "artifacts"), {"scenario": "0" * 64}, "workload.artifacts"),
    (("tools", "controller", "narrow", "payload_sha256"), {"narrow/runner.py": "0" * 64}, "tools.controller"),
    (("tools", "generator", "sha256"), "0" * 64, "tools.generator"),
    (("environment", "machine", "kernel"), "different-kernel", "environment.machine"),
    (("policy", "target_settings"), {"workers": 2}, "policy.target_settings"),
    (("policy", "repeats"), 3, "policy.repeats"),
])
def test_changed_conditions_refuse_comparison_before_writing(
    saved_comparison: Callable, path: tuple[str, ...], value: object, message: str,
) -> None:
    first, first_data = saved_comparison("first")
    sections = {key: deepcopy(value_) for key, value_ in first_data["stands"][0]["conditions"].items() if key != "fingerprint"}
    selected = sections

    for name in path[:-1]:
        selected = selected[name]

    selected[path[-1]] = value
    second, _ = saved_comparison("second", checksum="f", sections=sections)

    with pytest.raises(NarrowException, match=message):
        compare_runs([first, second], factor="revision")

@pytest.mark.parametrize(("path", "value", "message"), [
    (("stands", 0, "conditions"), None, "measurement conditions"),
    (("attempts", 0, "conditions_fingerprint"), "0" * 64, "session measurement conditions"),
    (("attempts", 0, "request", "method"), "POST", "Saved request differs"),
    (("attempts", 0, "request", "path_query_sha256"), "0" * 64, "Saved request differs"),
    (("meta", "options", "repeats"), 3, "Saved run policy differs"),
    (("meta", "generator", "sha256"), "0" * 64, "Saved generator differs"),
    (("attempts", 0, "repeat"), 2, "duplicate saved repeat"),
    (("status",), "running", "terminal narrow checkpoint"),
])
def test_internally_inconsistent_saved_evidence_is_not_comparable(
    saved_comparison: Callable, path: tuple, value: object, message: str,
) -> None:
    first, _ = saved_comparison("first")
    second, data = saved_comparison("second", checksum="f")
    selected = data

    for name in path[:-1]:
        selected = selected[name]

    selected[path[-1]] = value
    stats_dump(data, second / "run.json")

    with pytest.raises(NarrowException, match=message):
        compare_runs([first, second], factor="revision")

@pytest.mark.parametrize(("remove", "attempt_changes", "process_changes", "failed"), [
    ((1,), {}, {}, 1),
    ((1, 0), {}, {}, 2),
    ((), {"status": "error", "reason": "generator timeout"}, {}, 1),
    ((), {"cleanup_error": "generator still alive"}, {}, 1),
    ((), {}, {"cleanup_error": "server still alive"}, 2),
    ((), {}, {"output_truncated": True}, 2),
    ((), {"execution": {}}, {}, 1),
])
def test_missing_failed_or_unconfirmed_repeats_are_gaps_not_partial_medians(
    saved_comparison: Callable, remove: tuple[int, ...], attempt_changes: dict, process_changes: dict, failed: int,
) -> None:
    first, _ = saved_comparison("first")
    second, data = saved_comparison("second", checksum="f")
    data["attempts"][0].update(attempt_changes)
    data["stands"][0]["processes"][0].update(process_changes)

    for index in remove:
        data["attempts"].pop(index)

    stats_dump(data, second / "run.json")
    report = compare_runs([first, second], factor="revision")
    summary = report.serialize()

    assert summary["series"][1]["metrics"][1]["rps"] is None
    assert summary["series"][1]["metrics"][1]["failed_or_missing_repeats"] == failed
    assert report.create_figure().data[8].y[0] is None
    assert summary["issues"]
    assert all(row["delta_percent"] is None for row in summary["rows"] if (
        row["series"] == "R2" and row["metric"] == "rps" and row["clients"] == 1
    ))

def test_absent_quantile_is_a_gap_even_when_other_repeat_has_it(saved_comparison: Callable) -> None:
    first, _ = saved_comparison("first")
    second, data = saved_comparison("second", checksum="f")
    data["attempts"][0]["latency_ms"]["p99"] = None
    stats_dump(data, second / "run.json")

    report = compare_runs([first, second], factor="revision")

    assert report.serialize()["series"][1]["metrics"][1]["rps"] == 15
    assert report.create_figure().data[14].y[0] is None

def test_equal_requested_but_different_inherited_limits_are_not_equal_conditions(saved_comparison: Callable) -> None:
    first, _ = saved_comparison("first")
    second, data = saved_comparison("second", checksum="f")

    for attempt in data["attempts"]:
        attempt["execution"]["limits"]["applied"].update(address_space_bytes=1024**3, address_space_hard_bytes=1024**3)

    stats_dump(data, second / "run.json")

    with pytest.raises(NarrowException, match="Different applied generator limits"):
        compare_runs([first, second], factor="revision")

def test_latency_populations_cannot_silently_differ_between_runs(saved_comparison: Callable) -> None:
    first, _ = saved_comparison("first")
    second, data = saved_comparison("second", checksum="f")

    for attempt in data["attempts"]:
        attempt["latency_population"] = "a different population"

    stats_dump(data, second / "run.json")

    with pytest.raises(NarrowException, match="Different saved latency populations"):
        compare_runs([first, second], factor="revision")

def test_subjects_and_sessions_require_unambiguous_evidence(saved_comparison: Callable) -> None:
    first, _ = saved_comparison("first")
    second, data = saved_comparison("second", checksum="f")
    data["stands"].append({**data["stands"][0], "stand": "another", "session": 1})
    stats_dump(data, second / "run.json")

    with pytest.raises(NarrowException, match="Ambiguous saved sessions"):
        compare_runs([first, second], factor="revision")

    assert compare_runs([first, RunSelection(second, stand="server")], factor="revision").serialize()["baseline"] == "R1"
    data["stands"][0]["subject"] = SubjectSpec("server", "label-only").serialize()
    stats_dump(data, second / "run.json")

    with pytest.raises(NarrowException, match="artifact hashes"):
        compare_runs([first, RunSelection(second, stand="server")], factor="revision")

def test_subject_selector_disambiguates_same_alias_without_merging_sessions(saved_comparison: Callable) -> None:
    first, _ = saved_comparison("first")
    second, data = saved_comparison("second", checksum="f")
    original = data["stands"][0]["subject"]["fingerprint"]
    another = {
        **data["stands"][0], "session": 1, "subject": SubjectSpec("server", "other", {"runtime": "0" * 64}).serialize(),
    }
    data["stands"].append(another)
    stats_dump(data, second / "run.json")

    with pytest.raises(NarrowException, match="Ambiguous saved sessions"):
        compare_runs([first, second], factor="revision")

    selected = compare_runs([first, RunSelection(second, subject=original)], factor="revision").serialize()

    assert selected["series"][1]["subject"]["fingerprint"] == original
    assert selected["series"][1]["metrics"][1]["rps"] == 15

def test_zero_baseline_never_produces_infinite_percentage_change(saved_comparison: Callable) -> None:
    first, data = saved_comparison("first")
    second, _ = saved_comparison("second", checksum="f")

    for attempt in data["attempts"]:
        attempt.update(
            rps=0, status="http_error", responses=0, successful_responses=0, errors={"timeout": 2}, latency_ms={},
        )

    stats_dump(data, first / "run.json")
    report = compare_runs([first, second], factor="revision")
    rows = report.serialize()["rows"]
    selected = [row for row in rows if row["series"] == "R2" and row["metric"] == "rps"]

    assert all(row["baseline"] == 0 and row["delta_percent"] is None and row["change"] == "improved" for row in selected)
    json.dumps(report.serialize(), allow_nan=False)

def test_html_is_offline_safe_and_never_replaces_checkpoints(saved_comparison: Callable, tmp_path: Path) -> None:
    first, _ = saved_comparison("first")
    second, data = saved_comparison("second", checksum="f")
    untrusted = "</sCrIpT><img src=x onerror=alert(1)>"
    data["reason"] = data["attempts"][0]["reason"] = untrusted
    stats_dump(data, second / "run.json")
    original = second.joinpath("run.json").read_bytes()
    report = compare_runs([first, second], factor="revision")

    class Parser(HTMLParser):

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            assert tag not in ("img", "iframe")
            assert not (tag == "script" and "src" in dict(attrs))
            assert not any(name.startswith("on") for name, value in attrs)

    output = report.write_html(tmp_path / "comparison.html")
    html = output.read_text(encoding="utf-8")
    Parser().feed(html)

    assert "Δ %" in html and "lower is better" in html and "generator_process_lifetime" in html
    assert "&lt;/sCrIpT&gt;&lt;img" in html

    with pytest.raises(FileExistsError):
        report.write_html(second / "run.json")

    assert second.joinpath("run.json").read_bytes() == original

def test_cli_compares_from_foreign_directory_without_current_host_or_tools(
    saved_comparison: Callable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = saved_comparison("first")
    second, _ = saved_comparison("second", checksum="f")
    forbidden = Mock(side_effect=AssertionError("comparison attempted live measurement or observation"))
    monkeypatch.setattr(ProcessHandle, "start", forbidden)
    monkeypatch.setattr(identity, "collect_controller_identity", forbidden)
    monkeypatch.setattr(system, "collect_machine", forbidden)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    result = CliRunner().invoke(cli.entry_point, [
        "compare", f"{first}", f"{second}", "--factor", "revision", "--stand", "server", "--app", "py",
        "--output", "comparison.html",
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["baseline"] == "R1" and result.stderr == ""
    assert list(foreign.iterdir()) == [foreign / "comparison.html"]
    forbidden.assert_not_called()

def test_cli_refuses_insufficient_or_incompatible_inputs_without_output(saved_comparison: Callable, tmp_path: Path) -> None:
    first, _ = saved_comparison("first")
    output = tmp_path / "not-created.html"

    result = CliRunner().invoke(cli.entry_point, ["compare", f"{first}", "--factor", "revision", "--output", f"{output}"])

    assert result.exit_code != 0 and result.stdout == "" and "at least two" in result.stderr
    assert not output.exists()
