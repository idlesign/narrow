from pathlib import Path
from unittest.mock import Mock

import pytest

from narrow import plotting


@pytest.fixture
def saved_run() -> dict:
    attempts = [{
        "stand": "nginx_static", "app": None, "session": 0, "phase": "measurement", "repeat": 1,
        "clients": clients, "load": {"clients": clients, "requests": 10}, "rps": rps,
        "status": "ok" if rps is not None else "error", "latency_ms": {"p95": 10.0 if rps else None},
        "latency_population": "all HTTP responses", "statuses": {}, "errors": {},
    } for clients, rps in ((1, 100.0), (2, None), (4, 200.0))]
    return {"meta": {"date": "2026-10-31", "generator": {"version": "oha 1.16.0"}}, "attempts": attempts, "status": "failed"}


def test_plot_measured_points_and_gaps(saved_run: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    plot_method = Mock(return_value="narrow-stats.html")
    monkeypatch.setattr(plotting, "plot_method", plot_method)

    result = plotting.plot(saved_run)

    assert result == "narrow-stats.html"
    figure = plot_method.call_args.args[0]
    throughput = figure.data[0]
    assert throughput.x == (1, 2, 4)
    assert throughput.y == (100, None, 200)
    assert throughput.connectgaps is False
    assert throughput.line.shape == "linear"
    assert throughput.mode == "lines+markers"
    assert figure.layout.yaxis.title.text == "Successful responses/s (2xx)"
    assert plot_method.call_args.kwargs["auto_open"] is False


def test_repeats_are_not_pooled_percentiles(saved_run: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    plot_method = Mock(return_value="report.html")
    monkeypatch.setattr(plotting, "plot_method", plot_method)
    saved_run["attempts"].append({**saved_run["attempts"][0], "repeat": 2, "rps": 200.0, "latency_ms": {"p95": 30.0}})

    plotting.plot(saved_run)

    figure = plot_method.call_args.args[0]
    assert figure.data[0].y[0] == 150.0
    assert figure.data[0].error_y.array[0] == 50.0
    latency = figure.data[2]
    assert "median of per-run p95" in latency.name
    assert latency.y[0] == 20.0
    assert "all HTTP responses" in latency.hovertemplate


def test_failed_cleanup_has_evidence_but_no_plotted_summary(saved_run: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    plot_method = Mock(return_value="report.html")
    monkeypatch.setattr(plotting, "plot_method", plot_method)
    saved_run["stands"] = [{
        "session": 0, "stand": "nginx_static", "app": None, "artifacts": "stand-0001",
        "processes": [{"cleanup_error": "cannot confirm termination"}],
    }]

    plotting.plot(saved_run)

    figure = plot_method.call_args.args[0]
    assert figure.data[0].y == (None, None, None)
    assert "cannot confirm termination" in figure.data[-1].cells.values[-2]


def test_build_html_from_saved_data_only(saved_run: dict, tmp_path: Path) -> None:
    path = tmp_path / "report.html"

    result = plotting.plot(saved_run, filename=path)

    assert result == f"{path}"
    assert path.is_file()
