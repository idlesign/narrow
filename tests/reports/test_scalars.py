import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from narrow.reports.compare import ComparisonReport, ScalarSample, ScalarSeries, compare_scalar_series
from narrow.reports.metrics import MetricSpec, calculate_median
from narrow.targets.subjects import SubjectSpec

SPECIFICATIONS = {
    "rate": MetricSpec("Calls/s", higher_is_better=True), "cost": MetricSpec("Overhead (%)", higher_is_better=False),
}

@pytest.fixture
def scalar_series() -> Callable:

    def create(revision: str, *, name: str = "campaign", samples: tuple = (10, 20), **changes: object) -> ScalarSeries:
        definitions = {
            metric: {"unit": unit, "numerator": "observed work", "denominator": "declared window",
                     "aggregation": "per-attempt observation", "scope": "declared scalar workload"}
            for metric, unit in (("rate", "calls/s"), ("cost", "%"))
        }
        original = (ScalarSample("warmup", {"rate": 9999, "cost": 9999}, warmup=True),) + tuple(
            ScalarSample(f"sample-{index}", {"rate": value, "cost": -value if value is not None else None})
            for index, value in enumerate(samples, 1)
        )
        return replace(ScalarSeries(
            name=name, source=f"public/{name}/{revision}", case="cache",
            subject=SubjectSpec("any-platform", revision, {"runtime": revision * 64}),
            methodology_fingerprint="c" * 64, machine_fingerprint="d" * 64, definitions=definitions,
            samples=original, required_samples=2, required_warmups=1,
        ), **changes)

    return create

def test_scalar_samples_share_http_comparison_presentation_without_http_facts(scalar_series: Callable, tmp_path: Path) -> None:
    report = compare_scalar_series([
        scalar_series("a"), scalar_series("b", samples=(20, 40)), scalar_series("b", name="independent", samples=(100, 200)),
    ], specifications=SPECIFICATIONS)
    data = report.serialize()
    figure = report.create_figure()
    output = report.write_html(tmp_path / "comparison.html")

    assert isinstance(report, ComparisonReport)
    assert [item["metrics"]["cache"]["rate"] for item in data["series"]] == [15, 30, 150]
    assert figure.data[0].y == (15, 30) and figure.data[1].y == (None, 150)
    assert figure.data[0].connectgaps is False
    assert figure.layout.xaxis.title.text == "Selected subject order (not Git ancestry)"
    row = next(row for row in data["rows"] if row["series"] == "R2" and row["metric"] == "cost")
    assert row["value"] == -30 and row["delta"] == -15 and row["change"] == "improved"
    assert row["higher_is_better"] is False and "lower is better" in row["label"]
    assert data["comparability"] == "fingerprint-only" and "conditions" not in data["series"][0]
    assert "Fingerprint-only comparability" in output.read_text() and "Concurrent HTTP" not in output.read_text()
    assert "Scope" in output.read_text() and "warmup" in output.read_text()
    json.dumps(data, allow_nan=False)

@pytest.mark.parametrize("changes", [
    {"status": "failed"}, {"samples": ()}, {"required_samples": 3},
])
def test_required_population_failure_is_a_gap_even_when_a_subset_is_valid(scalar_series: Callable, changes: dict) -> None:
    report = compare_scalar_series([scalar_series("a", **changes)], specifications=SPECIFICATIONS)

    assert report.serialize()["series"][0]["metrics"]["cache"] == {"rate": None, "cost": None}
    assert report.serialize()["issues"]
    assert report.create_figure().data[0].y == (None,)

def test_a_missing_scalar_is_not_replaced_by_the_other_required_sample(scalar_series: Callable) -> None:
    series = scalar_series("a")
    partial = replace(series.samples[1], values={"rate": None, "cost": -10})
    selected = replace(series, samples=(series.samples[0], partial, series.samples[2]))

    report = compare_scalar_series([selected], specifications=SPECIFICATIONS)

    assert report.serialize()["series"][0]["metrics"]["cache"] == {"rate": None, "cost": -15}

def test_scalar_html_escapes_saved_names_sources_and_units(scalar_series: Callable) -> None:
    payload = "</script><script>alert(1)</script>"
    selected = scalar_series("a", name=payload, source=payload)
    definitions = {name: {**definition, "unit": payload} for name, definition in selected.definitions.items()}

    report = compare_scalar_series([replace(selected, definitions=definitions)], specifications=SPECIFICATIONS)
    rendered = report.render_html()

    assert payload not in rendered and "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in rendered

@pytest.mark.parametrize("changes", [
    {"machine_fingerprint": "e" * 64}, {"methodology_fingerprint": "e" * 64}, {"case": "ipc"},
])
def test_different_scalar_contracts_are_not_compared(scalar_series: Callable, changes: dict) -> None:

    with pytest.raises(ValueError, match="different workload"):
        compare_scalar_series([scalar_series("a"), scalar_series("b", **changes)], specifications=SPECIFICATIONS)

def test_scalar_subjects_and_original_samples_cannot_be_duplicated(scalar_series: Callable) -> None:
    selected = scalar_series("a")

    with pytest.raises(ValueError, match="Duplicate scalar run subject"):
        compare_scalar_series([selected, selected], specifications=SPECIFICATIONS)

    with pytest.raises(ValueError, match="Duplicate original scalar sample"):
        compare_scalar_series(
            [replace(selected, samples=(selected.samples[1], selected.samples[1]))], specifications=SPECIFICATIONS,
        )

@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 10**1000])
def test_nonfinite_or_boolean_scalars_do_not_enter_a_report(value: object) -> None:

    with pytest.raises(ValueError, match="finite numeric"):
        ScalarSample("sample", {"metric": value})

def test_finite_median_does_not_overflow_and_keeps_signed_values() -> None:
    assert calculate_median([1e308, 1.6e308]) == pytest.approx(1.3e308)
    assert calculate_median([-10, -20]) == -15
