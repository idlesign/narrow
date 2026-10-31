"""Reports from saved attempts only: no synthetic points, smoothing or new requests."""

import json
from collections.abc import Callable
from html import escape
from pathlib import Path
from statistics import median

import plotly.graph_objects as go
import plotly.offline
from colorhash import ColorHash
from plotly.subplots import make_subplots

from .configuration import Settings
from .exceptions import NarrowException

plot_method = plotly.offline.plot


def collect_points(
    attempts: list[dict], clients: list[int], get_metric: Callable[[dict], float | None],
) -> dict[str, list]:
    """Summarize available repeat metrics; missing points remain gaps."""
    points = {"values": [], "above": [], "below": [], "counts": []}

    for concurrency in clients:
        samples = [attempt for attempt in attempts if attempt["clients"] == concurrency]
        values = [
            value for attempt in samples
            if attempt["status"] in ("ok", "http_error") and not attempt.get("cleanup_error")
            and (value := get_metric(attempt)) is not None
        ]
        middle = median(values) if values else None
        points["values"].append(middle)
        points["above"].append(max(values) - middle if values else None)
        points["below"].append(middle - min(values) if values else None)
        points["counts"].append([len(values), len(samples)])

    return points


def plot(stats: dict, *, filename: Path | None = None) -> str:
    """Plot medians and min/max of run metrics, not a pooled latency percentile."""

    if not isinstance(stats.get("attempts"), list) or not isinstance(stats.get("meta"), dict):
        raise NarrowException("Not a narrow run checkpoint")

    figure = make_subplots(
        rows=3, cols=1, specs=[[{}], [{}], [{"type": "table"}]],
        row_heights=[0.32, 0.32, 0.36], vertical_spacing=0.12,
        subplot_titles=(
            "2xx throughput: median and min/max", "Per-run latency percentiles: median and min/max", "Attempts and failures",
        ),
    )
    measurements = [attempt for attempt in stats["attempts"] if attempt["phase"] == "measurement"]
    groups = {}
    failed_sessions = {
        stand["session"] for stand in stats.get("stands", [])
        if any(process["cleanup_error"] or process.get("output_truncated") for process in stand["processes"])
    }

    for attempt in measurements:
        policy = {name: value for name, value in attempt["load"].items() if name != "clients"}
        key = (attempt["stand"], attempt["app"], json.dumps(policy, sort_keys=True))
        groups.setdefault(key, []).append(attempt)

    for (stand_alias, app_alias, policy), attempts in groups.items():
        title = f"{stand_alias}/{app_alias}" if app_alias else stand_alias
        clients = sorted({attempt["clients"] for attempt in attempts})
        usable = [
            {**attempt, "cleanup_error": "Stand cleanup failed"} if attempt["session"] in failed_sessions else attempt
            for attempt in attempts
        ]

        for metric, percentile, row in (
            ("rps", None, 1), ("latency_ms", "p50", 2), ("latency_ms", "p95", 2), ("latency_ms", "p99", 2),
        ):

            def get_metric(attempt: dict) -> float | None:
                value = attempt.get(metric)
                return value.get(percentile) if isinstance(value, dict) else value

            points = collect_points(usable, clients, get_metric)
            name = title if percentile is None else f"{title}: median of per-run {percentile}"
            population = attempts[0].get("latency_population", "unavailable") if percentile else "2xx / whole-run seconds"
            figure.add_trace(go.Scatter(
                x=clients, y=points["values"], mode="lines+markers", name=escape(name),
                connectgaps=False, line={"shape": "linear", "color": ColorHash(name).hex},
                error_y={"type": "data", "array": points["above"], "arrayminus": points["below"], "symmetric": False},
                customdata=points["counts"],
                hovertemplate=(
                    "clients=%{x}<br>value=%{y}<br>available repeats=%{customdata[0]}/%{customdata[1]}"
                    + f"<br>{escape(population)}<extra></extra>"
                ),
                meta={"policy": policy, "population": population},
            ), row=row, col=1)

    rows = []

    for attempt in stats["attempts"]:
        errors = {**attempt.get("errors", {})}
        http_errors = {
            code: count for code, count in attempt.get("statuses", {}).items()
            if not code.startswith("2") and count
        }
        rows.append([
            attempt["stand"], attempt.get("app") or "—", attempt["clients"], attempt["phase"], attempt["repeat"],
            attempt["status"], attempt.get("rps"), json.dumps(http_errors), json.dumps(errors),
            attempt.get("reason") or "", attempt.get("artifacts") or "",
        ])

    for stand in stats.get("stands", []):

        for process in stand["processes"]:

            if process["cleanup_error"]:
                rows.append([stand["stand"], stand.get("app") or "—", "—", "cleanup", "—", "failed", "—", "—", "—",
                             process["cleanup_error"], stand["artifacts"]])

    table = [[escape(f"{value}") for value in column] for column in zip(*rows, strict=True)] if rows else [[]] * 11
    figure.add_trace(go.Table(
        header={"values": [
            "Stand", "App", "Clients", "Phase", "Repeat", "Status", "2xx RPS", "HTTP errors", "Other outcomes", "Reason", "Artifacts",
        ]},
        cells={"values": table},
    ), row=3, col=1)
    metadata = stats["meta"]
    details = json.dumps({
        "options": metadata.get("options"), "generator": metadata.get("generator"),
        "servers": metadata.get("servers"), "server_settings": metadata.get("server_settings"),
    }, sort_keys=True)
    run_status = escape(stats.get("status", "unknown"))
    gathered_date = escape(metadata.get("date", ""))
    figure.update_layout(
        title={"text": f"narrow: {run_status}<br>{gathered_date}"},
        height=1200, hoverlabel={"namelength": -1},
        legend={"orientation": "h", "y": -0.05}, margin={"b": 180},
    )
    figure.add_annotation(
        text=escape(details), xref="paper", yref="paper", x=0, y=-0.15, showarrow=False, align="left", font={"size": 10},
    )
    figure.update_xaxes(title_text="Concurrent HTTP/1.1 clients", row=1, col=1)
    figure.update_xaxes(title_text="Concurrent HTTP/1.1 clients", row=2, col=1)
    figure.update_yaxes(title_text="Successful responses/s (2xx)", row=1, col=1)
    figure.update_yaxes(title_text="Latency (ms; population in trace metadata)", row=2, col=1)
    return plot_method(figure, filename=f"{filename or Settings.FILENAME_STATS_PLOT}", auto_open=False)
