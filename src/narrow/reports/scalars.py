"""Compare declared scalar populations without pretending they are HTTP attempts."""

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from html import escape

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..targets.subjects import SubjectSpec
from .comparison import ComparisonReport, collect_rows
from .metrics import MetricSpec, calculate_median
from .plotting import COLORS

STATUSES = ("complete", "failed", "invalid", "unsupported", "cancelled")

def _require_text(value: object, name: str) -> None:

    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"Scalar {name} requires bounded nonempty text")

@dataclass(frozen=True)
class ScalarSample:
    """One original scalar observation; a warmup is retained but never aggregated."""

    identity: str
    values: Mapping[str, float | None]
    status: str = "complete"
    warmup: bool = False

    def __post_init__(self) -> None:
        _require_text(self.identity, "sample identity")

        if self.status not in STATUSES or type(self.warmup) is not bool or not isinstance(self.values, Mapping):
            raise ValueError("Invalid scalar sample state")

        for name, value in self.values.items():
            _require_text(name, "metric name")

            if value is not None:
                calculate_median([value])

        object.__setattr__(self, "values", deepcopy(dict(self.values)))

    def serialize(self) -> dict:
        """Return saved scalar values, without inventing request counts or CPU scopes."""
        return {
            "identity": self.identity, "metrics": dict(self.values), "status": self.status,
            "phase": "warmup" if self.warmup else "measurement",
        }

@dataclass(frozen=True)
class ScalarSeries:
    """One saved run under an explicit fingerprint-only comparison contract.

    The producer validates its own workload, gates and source evidence before
    supplying these normalized observations. Hash equality is not proof of the
    stronger, observed HTTP MeasurementConditions contract.
    """

    name: str
    source: str
    case: str
    subject: SubjectSpec
    methodology_fingerprint: str
    machine_fingerprint: str
    definitions: Mapping[str, dict]
    samples: Sequence[ScalarSample]
    required_samples: int
    required_warmups: int = 0
    status: str = "complete"
    synthetic: bool = False

    def __post_init__(self) -> None:

        for name in ("name", "source", "case"):
            _require_text(getattr(self, name), name)

        for name in ("methodology_fingerprint", "machine_fingerprint"):

            if not isinstance(value := getattr(self, name), str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"Scalar {name} requires a complete SHA-256 digest")

        if not isinstance(self.subject, SubjectSpec) or self.status not in STATUSES or type(self.synthetic) is not bool:
            raise ValueError("Invalid scalar subject or run state")

        for name, minimum in (("required_samples", 1), ("required_warmups", 0)):

            if type(value := getattr(self, name)) is not int or not minimum <= value <= 64:
                raise ValueError(f"Scalar {name} is outside its bounded population")

        if not isinstance(self.definitions, Mapping) or not 1 <= len(self.definitions) <= 64:
            raise ValueError("Scalar comparison requires explicit metric definitions")

        for name, definition in self.definitions.items():
            _require_text(name, "metric name")

            if not isinstance(definition, dict) or set(definition) != {
                "unit", "numerator", "denominator", "aggregation", "scope",
            }:
                raise ValueError("Scalar metrics require explicit unit, population, aggregation and scope")

            for field, value in definition.items():
                _require_text(value, field)

        if not isinstance(self.samples, (list, tuple)) or len(self.samples) > 128:
            raise ValueError("Scalar observations require a bounded original sample population")

        if any(not isinstance(sample, ScalarSample) for sample in self.samples):
            raise ValueError("Scalar observations require ScalarSample instances")

        json.dumps(dict(self.definitions), allow_nan=False, sort_keys=True)
        object.__setattr__(self, "definitions", deepcopy(dict(self.definitions)))
        object.__setattr__(self, "samples", tuple(self.samples))

def _summarize_series(series: ScalarSeries, index: int, specifications: Mapping[str, MetricSpec]) -> tuple[dict, list[dict]]:
    observations = [sample for sample in series.samples if not sample.warmup]
    warmups = [sample for sample in series.samples if sample.warmup]
    identities = [sample.identity for sample in series.samples]

    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate original scalar sample")

    for sample in series.samples:

        if sample.status == "complete" and sample.values.keys() != specifications.keys():
            raise ValueError("Complete scalar sample differs from its declared metric population")

    complete = (
        series.status == "complete" and len(observations) == series.required_samples
        and len(warmups) == series.required_warmups and all(sample.status == "complete" for sample in series.samples)
    )

    if complete and not series.subject.artifact_sha256:
        raise ValueError("Complete scalar runs require saved subject artifact hashes")

    values = {}

    for name in specifications:
        samples = [sample.values.get(name) for sample in observations]
        values[name] = calculate_median(samples) if complete and all(value is not None for value in samples) else None

    record = {
        "id": f"R{index}", "name": series.name, "source": series.source, "case": series.case,
        "subject": series.subject.serialize(), "label": f"{series.subject.platform}@{series.subject.revision or 'unlabeled'}",
        "methodology_fingerprint": series.methodology_fingerprint, "machine_fingerprint": series.machine_fingerprint,
        "comparability": f"fingerprint-only: method={series.methodology_fingerprint}; machine={series.machine_fingerprint}",
        "metrics": {series.case: values}, "attempts": [sample.serialize() for sample in series.samples],
        "status": series.status, "synthetic": series.synthetic,
        "evidence": {
            "metric_definitions": dict(series.definitions),
            "aggregation": "median of all required original samples; no pooled quantiles",
            "required_samples": series.required_samples, "required_warmups": series.required_warmups,
            "observed_samples": len(observations), "observed_warmups": len(warmups),
        },
    }
    issues = [
        {"series": record["id"], "scope": sample.identity, "detail": sample.status}
        for sample in series.samples if sample.status != "complete"
    ]

    if not complete:
        issues.append({
            "series": record["id"], "scope": "run", "detail": "Run failed or required original samples are incomplete",
        })

    for name, value in values.items():

        if value is None:
            issues.append({"series": record["id"], "scope": name, "detail": "Observation unavailable; no subset median"})

    return record, issues

def _create_figure(series: list[dict], specifications: Mapping[str, MetricSpec]) -> go.Figure:
    figure = make_subplots(
        rows=len(specifications), cols=1, shared_xaxes=True, vertical_spacing=min(0.06, 1 / (len(specifications) + 1)),
        subplot_titles=tuple(escape(specification.label) for specification in specifications.values()),
    )
    subjects = list(dict.fromkeys(item["subject"]["fingerprint"] for item in series))
    labels = {
        item["subject"]["fingerprint"]: (
            f"{(item['subject']['revision'] or item['subject']['platform'])[:12]} [{item['subject']['fingerprint'][:12]}]"
        )
        for item in series
    }
    groups = list(dict.fromkeys(item["name"] for item in series))

    for row, (name, specification) in enumerate(specifications.items(), 1):

        for group_index, group in enumerate(groups):
            members = {item["subject"]["fingerprint"]: item for item in series if item["name"] == group}
            values = [
                members[subject]["metrics"][members[subject]["case"]][name] if subject in members else None
                for subject in subjects
            ]
            figure.add_trace(go.Scatter(
                x=list(range(len(subjects))), y=values, mode="lines+markers", name=escape(group),
                legendgroup=group, showlegend=row == 1, connectgaps=False,
                line={"color": COLORS[group_index % len(COLORS)]},
                customdata=[[escape(labels[subject])] for subject in subjects],
                meta={
                    "metric": name, "higher_is_better": specification.higher_is_better,
                    "summary": "median of required samples",
                },
                hovertemplate="%{customdata[0]}<br>value=%{y:.6g}<extra></extra>",
            ), row=row, col=1)

        unit = series[0]["evidence"]["metric_definitions"][name]["unit"]
        axis = MetricSpec(unit, higher_is_better=specification.higher_is_better)
        figure.update_yaxes(title_text=escape(axis.format_label()), row=row, col=1)

        if not any(value is not None for trace in figure.data if trace.meta["metric"] == name for value in trace.y):
            figure.add_annotation(text="No accepted observations", showarrow=False, row=row, col=1, x=0, y=0)

    figure.update_xaxes(
        tickmode="array", tickvals=list(range(len(subjects))), ticktext=[escape(labels[subject]) for subject in subjects],
        title_text="Selected subject order (not Git ancestry)",
    )
    figure.update_layout(template="plotly_white", height=max(500, 290 * len(specifications)), margin={"t": 90, "b": 100})
    return figure

def compare_scalar_series(series: Sequence[ScalarSeries], *, specifications: Mapping[str, MetricSpec]) -> ComparisonReport:
    """Compare explicit scalar samples by saved fingerprints, never by fake HTTP conditions."""

    if not isinstance(series, (tuple, list)) or not series or len(series) > 4096:
        raise ValueError("Scalar comparison requires a bounded nonempty saved run selection")

    if not isinstance(specifications, Mapping) or not specifications or any(
        not isinstance(specification, MetricSpec) for specification in specifications.values()
    ):
        raise ValueError("Scalar comparison requires explicit MetricSpec descriptions")

    if any(not isinstance(item, ScalarSeries) for item in series):
        raise ValueError("Scalar comparison requires ScalarSeries instances")

    first = series[0]
    selected, issues, seen = [], [], set()

    for index, item in enumerate(series, 1):
        item = replace(item, samples=tuple(replace(sample) for sample in item.samples))

        if (
            item.case != first.case or item.methodology_fingerprint != first.methodology_fingerprint
            or item.machine_fingerprint != first.machine_fingerprint or item.definitions != first.definitions
            or item.required_samples != first.required_samples or item.required_warmups != first.required_warmups
            or item.definitions.keys() != specifications.keys() or item.subject.platform != first.subject.platform
        ):
            raise ValueError(
                "Scalar runs have different workload, method, machine, metric definitions or required populations"
            )

        identity = (item.name, item.subject.serialize()["fingerprint"])

        if identity in seen:
            raise ValueError("Duplicate scalar run subject; name independent input series separately")

        seen.add(identity)
        record, problems = _summarize_series(item, index, specifications)
        selected.append(record)
        issues.extend(problems)

    synthetic = any(item.synthetic for item in series)
    data = {
        "factor": "revision", "baseline": "R1", "series": selected,
        "rows": collect_rows(selected, specifications, coordinate_key="case"), "issues": issues,
        "title": f"{'SYNTHETIC — not measured performance · ' if synthetic else ''}{first.case} · saved scalar comparison",
        "coordinate_label": "Case", "comparability": "fingerprint-only", "synthetic": synthetic,
        "notes": [
            "Fingerprint-only comparability: the producer validates its saved workload, metric definitions, "
            "gates and source evidence.",
            "This scalar view does not establish observed HTTP MeasurementConditions, server CPU scope "
            "or a common request population.",
            "Campaigns remain separate graph lines; the saved subject order does not infer Git ancestry "
            "or measurement timestamps.",
            *(["SYNTHETIC observations — not measured platform performance."] if synthetic else []),
        ],
    }
    json.dumps(data, allow_nan=False)
    return ComparisonReport(data, _create_figure(selected, specifications))
