"""Shared baseline deltas, tables and offline presentation for saved observations."""

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from html import escape
from pathlib import Path
from string import Template

import plotly.graph_objects as go
import plotly.io as pio

from .metrics import MetricSpec
from .plotting import RunReport, render_table

def collect_rows(series: list[dict], specifications: Mapping[str, MetricSpec], *, coordinate_key: str) -> list[dict]:
    """Compare every retained point with the first run, without averaging input runs."""
    rows = []
    baseline = series[0]["metrics"]

    for selected in series:

        for coordinate, values in selected["metrics"].items():

            for name, specification in specifications.items():
                value, original = values[name], baseline[coordinate][name]
                delta = value - original if value is not None and original is not None else None
                delta = delta if delta is not None and math.isfinite(delta) else None
                percent = 100 * (delta / original) if delta is not None and original else None
                percent = percent if percent is not None and math.isfinite(percent) else None
                change = "unavailable"

                if value is not None and original is not None:
                    change = "unchanged" if value == original else "neutral"

                    if value != original and specification.higher_is_better is not None:
                        change = "improved" if (value > original) == specification.higher_is_better else "regressed"

                rows.append({
                    "series": selected["id"], coordinate_key: coordinate, "coordinate": coordinate,
                    "metric": name, "label": specification.format_label(), "value": value, "baseline": original,
                    "delta": delta, "delta_percent": percent, "change": change,
                    "higher_is_better": specification.higher_is_better,
                })

    return rows

class ComparisonReport:
    """Render validated HTTP or scalar inputs without observing the current host."""

    def __init__(self, data: dict, chart: RunReport | go.Figure) -> None:
        self._data = deepcopy(data)
        self._chart = chart

    def serialize(self) -> dict:
        """Return JSON-safe rows and the original selected observation population."""
        return deepcopy(self._data)

    def create_figure(self) -> go.Figure:
        """Keep saved runs distinct, with gaps for incomplete required populations."""
        figure = self._chart.create_figure() if isinstance(self._chart, RunReport) else go.Figure(self._chart)
        figure.update_layout(title={"text": escape(self._data["title"])})
        return figure

    def render_html(self) -> str:
        """Build the common self-contained chart, delta table and saved evidence view."""
        chart = pio.to_html(
            self.create_figure(), full_html=False, include_plotlyjs=True, include_mathjax=False,
            div_id="narrow-comparison", config={"responsive": True, "displaylogo": False},
        )
        table = render_table([
            "Run", self._data["coordinate_label"], "Metric / better direction", "Value", "Baseline", "Δ", "Δ %", "Change",
        ], [[
            row["series"], row["coordinate"], row["label"], row["value"], row["baseline"], row["delta"],
            row["delta_percent"], row["change"],
        ] for row in self._data["rows"]])
        conditions = render_table(["Run", "Source", "Measured code", "Comparability contract"], [[
            item["id"], item["source"], item["label"], item["comparability"],
        ] for item in self._data["series"]])
        issues = render_table(["Run", "Scope", "Issue / saved evidence"], [[
            issue["series"], issue["scope"], issue["detail"],
        ] for issue in self._data["issues"]])
        raw = render_table(["Run", "Saved observation (not necessarily usable)"], [[
            item["id"], json.dumps(attempt, ensure_ascii=True),
        ] for item in self._data["series"] for attempt in item["attempts"]])
        evidence = render_table(["Run", "Saved evidence; measurement scopes are not interchangeable"], [[
            item["id"], json.dumps(item["evidence"], ensure_ascii=True),
        ] for item in self._data["series"]])
        notes = [
            "The first saved run is the baseline. Independent input runs are retained, never pooled.",
            "Only saved observations are used. No current machine, tool, target or workload is probed or started.",
            "Missing/failed required samples are gaps, never subset medians. Per-attempt latency quantiles are not pooled.",
            "Percentage change is unavailable for a zero or missing baseline; signed baselines retain their arithmetic sign.",
            "Arrows describe a preferable value, not statistical confidence or an automatic performance verdict.",
            *self._data["notes"],
        ]
        template = Template((Path(__file__).parent / "files" / "report.html").read_text(encoding="utf-8"))
        measured = sum(attempt["phase"] == "measurement" for item in self._data["series"] for attempt in item["attempts"])
        warmups = sum(attempt["phase"] == "warmup" for item in self._data["series"] for attempt in item["attempts"])
        return template.substitute(
            status="saved comparison", date=f"Factor: {self._data['factor']}; baseline: {self._data['baseline']}", reason="",
            measured=measured, warmups=warmups, series=len(self._data["series"]), series_label="independent saved runs",
            issues=len(self._data["issues"]), chart=chart, chart_label="Saved observation comparisons",
            chart_help="Hover for saved values; use the camera button to export the figure. Missing observations remain gaps.",
            evidence_title="Evidence and comparability", notes="".join(f"<li>{escape(note)}</li>" for note in notes),
            conditions=conditions + table, targets=issues + evidence, attempts=raw,
            metadata=escape(json.dumps(self._data, indent=2, ensure_ascii=True)),
        )

    def write_html(self, path: Path) -> Path:
        """Write one new report, never replace an existing checkpoint or output."""
        contents = self.render_html()
        path = Path(path)

        with path.open("x", encoding="utf-8") as output:
            output.write(contents)

        return path
