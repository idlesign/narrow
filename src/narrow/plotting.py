"""Offline reports from saved attempts, without new requests or pooled percentiles."""

import json
from collections.abc import Callable
from html import escape
from math import isfinite
from pathlib import Path
from statistics import median
from string import Template

import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots

from .configuration import Settings
from .exceptions import NarrowException

COLORS = (
    "#2563eb", "#15803d", "#d97706", "#7c3aed", "#0891b2",
    "#be123c", "#64748b", "#a16207", "#c026d3", "#0f766e",
)
DASHES = ("solid", "dash", "dot")

def read_metric(attempt: dict, metric: str, percentile: str | None = None) -> float | None:
    """Read a usable finite metric; failed or unverified attempts are not measurements."""

    if attempt["status"] not in ("ok", "http_error") or attempt.get("cleanup_error"):
        return None

    value = attempt.get(metric)

    if percentile is not None:
        value = value.get(percentile) if isinstance(value, dict) else None

    return value if type(value) in (int, float) and isfinite(value) and value >= 0 else None

def collect_points(
    attempts: list[dict], clients: list[int], get_metric: Callable[[dict], float | None],
) -> dict[str, list]:
    """Keep individual repeats alongside medians and min/max; missing points remain gaps."""
    points = {"values": [], "above": [], "below": [], "counts": [], "samples": []}

    for concurrency in clients:
        samples = [attempt for attempt in attempts if attempt["clients"] == concurrency]
        available = [(attempt, value) for attempt in samples if (value := get_metric(attempt)) is not None]
        values = [value for attempt, value in available]
        middle = median(values) if values else None
        points["values"].append(middle)
        points["above"].append(max(values) - middle if values else None)
        points["below"].append(middle - min(values) if values else None)
        points["counts"].append([len(values), len(samples)])
        points["samples"].extend(available)

    return points

def render_text(value: object) -> str:
    """Render a saved value as text, not report markup."""

    if isinstance(value, float):
        return escape(f"{value:.6g}")

    return escape("—" if value is None else f"{value}")

def render_table(headers: list[str], rows: list[list]) -> str:
    """Render evidence without treating saved values as markup or executable links."""
    heading = "".join(f"<th scope=\"col\">{escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{render_text(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<div class=\"table-scroll\"><table><thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>"

class RunReport:
    """Present one checkpoint, retaining request/load boundaries and cleanup evidence."""

    def __init__(self, stats: dict) -> None:

        if (
            not isinstance(stats, dict) or not isinstance(stats.get("attempts"), list)
            or not isinstance(stats.get("meta"), dict)
        ):
            raise NarrowException("Not a narrow run checkpoint")

        self.stats = stats
        self.metadata = stats["meta"]
        failed_sessions = {
            stand["session"] for stand in stats.get("stands", [])
            if any(process.get("cleanup_error") or process.get("output_truncated") for process in stand["processes"])
        }
        self.attempts = [
            {**attempt, "cleanup_error": attempt.get("cleanup_error") or "Target cleanup failed or diagnostics were truncated"}
            if attempt["session"] in failed_sessions else attempt
            for attempt in stats["attempts"]
        ]
        self.measurements = [attempt for attempt in self.attempts if attempt["phase"] == "measurement"]
        self.groups = {}

        for attempt in self.measurements:
            policy = {name: value for name, value in attempt["load"].items() if name != "clients"}
            key = (
                attempt["stand"], attempt["app"], json.dumps(policy, sort_keys=True),
                json.dumps(attempt.get("request", {}), sort_keys=True), attempt.get("latency_population", "unavailable"),
            )
            self.groups.setdefault(key, []).append(attempt)

        self.windows = [
            value for attempt in self.measurements
            if type(value := attempt.get("window_seconds")) in (int, float) and isfinite(value) and value > 0
        ]

    def create_figure(self) -> go.Figure:
        """Build both panels, also used by the browser's PNG export."""
        figure = make_subplots(rows=1, cols=2, horizontal_spacing=0.13, subplot_titles=("2xx throughput", "HTTP latency"))
        is_comparison = len(self.groups) > 1

        for group_index, (key, attempts) in enumerate(self.groups.items()):
            stand, app, policy, request_json, latency_population = key
            clients = sorted({attempt["clients"] for attempt in attempts})
            series = f"S{group_index + 1}"
            target = f"{stand}/{app}" if app else stand
            label = f"{series} · {target}"

            for metric_index, (metric, percentile, column) in enumerate((
                ("rps", None, 1), ("latency_ms", "p50", 2), ("latency_ms", "p95", 2), ("latency_ms", "p99", 2),
            )):
                points = collect_points(attempts, clients, lambda attempt: read_metric(attempt, metric, percentile))
                name = label if is_comparison else percentile or "2xx RPS"
                legend_group = series if is_comparison else name
                population = latency_population if percentile else "2xx responses / whole-run seconds"
                color_index = group_index if is_comparison else max(0, metric_index - 1)
                color = COLORS[color_index % len(COLORS)]
                dash = DASHES[metric_index - 1] if is_comparison and percentile else "solid"
                trace_metadata = {
                    "series": series, "target": stand, "app": app,
                    "policy": json.loads(policy), "request": json.loads(request_json),
                    "population": population, "metric": percentile or metric,
                }
                hover_title = f"{escape(label)} · {percentile or "2xx RPS"}<br>"
                hover = f"<br>{escape(population)}<extra></extra>"
                figure.add_trace(go.Scatter(
                    x=clients, y=points["values"], mode="lines+markers", name=escape(name), legendgroup=legend_group,
                    showlegend=not is_comparison or percentile is None,
                    connectgaps=False,
                    line={"shape": "linear", "color": color, "width": 2, "dash": dash},
                    marker={"size": 8},
                    error_y={
                        "type": "data", "array": points["above"], "arrayminus": points["below"], "symmetric": False,
                        "color": color, "thickness": 1.2, "width": 5,
                    },
                    customdata=points["counts"], meta={**trace_metadata, "summary": "median of per-run values"},
                    hovertemplate=(
                        hover_title + "clients=%{x}<br>median=%{y:.4g}"
                        "<br>available repeats=%{customdata[0]}/%{customdata[1]}" + hover
                    ),
                ), row=1, col=column)
                figure.add_trace(go.Scatter(
                    x=[attempt["clients"] for attempt, value in points["samples"]],
                    y=[value for attempt, value in points["samples"]], mode="markers", name=f"{escape(name)} attempts",
                    legendgroup=legend_group, showlegend=False, marker={"color": color, "size": 6, "opacity": 0.4},
                    customdata=[
                        [attempt["repeat"], escape(attempt["status"]), escape(attempt.get("artifacts", ""))]
                        for attempt, value in points["samples"]
                    ],
                    meta={**trace_metadata, "summary": "individual attempts"},
                    hovertemplate=(
                        hover_title + "clients=%{x}<br>value=%{y:.4g}<br>repeat=%{customdata[0]}<br>status=%{customdata[1]}"
                        "<br>%{customdata[2]}" + hover
                    ),
                ), row=1, col=column)

        title = "narrow"

        if len(self.groups) == 1:
            stand, app, policy, request_json, population = next(iter(self.groups))
            request = json.loads(request_json)
            title = f"{stand}/{app}" if app else stand
            title += f" · {request.get("method", "")} {request.get("path") or request.get("url", "")}"

        generator = self.metadata.get("generator", {}).get("version", "generator unavailable")
        subtitle = f"{len(self.measurements)} measured attempts · {len(self.attempts) - len(self.measurements)} warmups excluded"
        figure.update_layout(
            template="plotly_white", height=650,
            paper_bgcolor="#ffffff", plot_bgcolor="#ffffff",
            title={"text": f"{escape(title[:120])}<br><sup>{escape(subtitle)} · {escape(generator)}</sup>", "x": 0.04},
            font={"family": "system-ui, sans-serif", "color": "#1e293b", "size": 13},
            hoverlabel={"namelength": -1}, legend={"orientation": "h", "y": -0.19, "x": 0.5, "xanchor": "center"},
            margin={"t": 115, "b": 200, "l": 75, "r": 35},
        )
        all_clients = sorted({attempt["clients"] for attempt in self.measurements})
        figure.update_xaxes(
            title_text="Concurrent HTTP/1.1 clients (log scale)", type="log", tickmode="array", tickvals=all_clients,
            ticktext=[f"{clients}" for clients in all_clients], showgrid=False, zeroline=False,
        )
        figure.update_yaxes(rangemode="tozero", gridcolor="#e2e8f0", zeroline=False)
        figure.update_yaxes(title_text="Successful responses/s (2xx)", row=1, col=1)
        figure.update_yaxes(title_text="Latency (ms)", row=1, col=2)
        caption = (
            "Lines: medians · bars: min–max, not confidence intervals · faint dots: individual attempts."
            "<br>Latency: medians of per-attempt percentiles, not pooled percentiles. Missing/invalid values remain gaps."
        )

        if is_comparison:
            caption += "<br>Comparisons: color = series; latency solid = p50, dashed = p95, dotted = p99."

        if self.windows:
            caption += f"<br>Recorded generation windows: {min(self.windows):.3f}–{max(self.windows):.3f} s per attempt."

        figure.add_annotation(
            text=caption,
            xref="paper", yref="paper", x=0, y=-0.36, xanchor="left", yanchor="top",
            showarrow=False, align="left", font={"size": 12, "color": "#475569"},
        )

        for column, axis in ((1, "x"), (2, "x2")):

            if not any(value is not None for trace in figure.data if trace.xaxis == axis for value in trace.y):
                figure.add_annotation(text="No usable measurements", showarrow=False, row=1, col=column, x=0, y=0)

        return figure

    def render_conditions(self) -> str:
        """Describe recorded workloads without guessing the external server configuration."""
        sections = []

        for group_index, (key, attempts) in enumerate(self.groups.items(), 1):
            stand, app, policy, request_json, population = key
            request = json.loads(request_json)
            load = json.loads(policy)
            title = f"{stand}/{app}" if app else stand
            rows = [[
                "Request", f"{request.get("method", "unavailable")} {request.get("url", request.get("path", "unavailable"))}",
            ]]
            rows.append(["Clients", ", ".join(f"{clients}" for clients in sorted({attempt["clients"] for attempt in attempts}))])

            for name, label in (
                ("expected_status", "Expected HTTP status"),
                ("expected_response_bytes", "Expected response body bytes (aggregate check)"),
            ):

                if (value := request.get(name)) is not None:
                    rows.append([label, value])

            for name, label in (
                ("requests", "Requests per attempt"), ("duration", "Generation seconds per attempt"),
                ("qps", "Requested QPS (not proof of offered load)"), ("worker_threads", "Generator threads"),
                ("keepalive", "Keepalive"), ("drain", "Drain"), ("timeout", "Request timeout, seconds"),
                ("connect_timeout", "Connect timeout, seconds"),
            ):

                if (value := load.get(name)) is not None:
                    rows.append([label, value])

            rows.append(["Latency population", population])
            sections.append(f"<h3>S{group_index} · {escape(title)}</h3>" + render_table(["Condition", "Recorded value"], rows))

        options = self.metadata.get("options", {})
        settings = {
            "Repeats requested": options.get("repeats"), "Warmup requests per point": options.get("warmup_requests"),
            "Generator": self.metadata.get("generator", {}).get("version"), "Python": self.metadata.get("python"),
            "Platform": self.metadata.get("platform"), "Built-in server settings": self.metadata.get("server_settings"),
        }
        rows = [[name, json.dumps(value) if isinstance(value, dict) else value] for name, value in settings.items()]
        return render_table(["Run condition", "Recorded value"], rows) + "".join(sections)

    def render_attempts(self) -> str:
        """Keep warmups, invalid samples and outcome counts visible outside the chart."""
        rows = []

        for attempt in self.attempts:
            rows.append([
                attempt["stand"], attempt.get("app"), attempt["phase"], attempt["repeat"], attempt["clients"], attempt["status"],
                attempt.get("window_seconds"), attempt.get("rps"), json.dumps(attempt.get("statuses", {})),
                json.dumps(attempt.get("errors", {})), attempt.get("deadline_outcomes"),
                "; ".join(value for value in (attempt.get("reason"), attempt.get("cleanup_error")) if value),
                attempt.get("artifacts"),
            ])

        return render_table([
            "Target", "App", "Phase", "Repeat", "Clients", "Status", "Generation seconds", "2xx RPS (raw)",
            "HTTP statuses", "Other outcomes", "Deadline outcomes", "Reason / exclusion", "Artifacts",
        ], rows)

    def render_targets(self) -> str:
        """Show process evidence and distinguish it from unobserved external lifecycles."""
        rows = []

        for stand in self.stats.get("stands", []):
            issues = []

            for process in stand["processes"]:

                if process.get("cleanup_error"):
                    issues.append(process["cleanup_error"])

                if process.get("output_truncated"):
                    issues.append("Process diagnostics truncated")

            evidence = "No process evidence recorded"

            if stand.get("ownership") == "external":
                evidence = "External target: server configuration and lifecycle not observed by narrow"

            elif stand["processes"]:
                evidence = "Recorded process cleanup has no reported errors"

            rows.append([
                stand["stand"], stand["session"], stand.get("ownership"), "; ".join(issues) or evidence, stand.get("artifacts"),
            ])

        return render_table(["Target", "Session", "Ownership", "Evidence / issues", "Artifacts"], rows)

    def render_html(self) -> str:
        """Build a self-contained report; the camera button exports this same figure."""
        figure = self.create_figure()
        chart = pio.to_html(
            figure, full_html=False, include_plotlyjs=True, include_mathjax=False, div_id="narrow-chart",
            config={
                "responsive": True, "displaylogo": False,
                "modeBarButtonsToRemove": ["sendChartToCloud"],
                "toImageButtonOptions": {
                    "format": "png", "filename": "narrow-chart", "width": 1400, "height": figure.layout.height, "scale": 2,
                },
            },
        )
        notes = [
            "Warmups are excluded from the charts. Only saved attempts are used; no server or generator is started.",
            "HTTP errors remain in the outcome table. Valid HTTP-error attempts may contribute metrics; failed response checks "
            "and cleanup failures do not. Missing values are never replaced with zero.",
        ]

        if self.windows:
            notes.append(f"Recorded generation windows: {min(self.windows):.3f}–{max(self.windows):.3f} seconds per measured attempt.")

            if min(self.windows) < 1:
                notes.append("Some generation windows are shorter than one second; interpret small differences cautiously.")

        template = Template((Path(__file__).parent / "files" / "report.html").read_text(encoding="utf-8"))
        return template.substitute(
            status=render_text(self.stats.get("status", "unknown")), date=render_text(self.metadata.get("date", "unavailable")),
            reason=render_text(self.stats.get("reason") or ""), measured=len(self.measurements),
            warmups=len(self.attempts) - len(self.measurements), series=len(self.groups), chart=chart,
            issues=sum(attempt["status"] != "ok" or bool(attempt.get("cleanup_error")) for attempt in self.attempts),
            notes="".join(f"<li>{escape(note)}</li>" for note in notes), conditions=self.render_conditions(),
            attempts=self.render_attempts(), targets=self.render_targets(),
            metadata=escape(json.dumps(self.metadata, indent=2, ensure_ascii=False)),
        )

def plot(stats: dict, *, filename: Path | None = None) -> str:
    """Write an offline HTML report with a browser-side PNG export, without new workloads."""
    report = RunReport(stats)
    target = Path(filename or Settings.FILENAME_STATS_PLOT)
    target.write_text(report.render_html(), encoding="utf-8")
    return f"{target}"
