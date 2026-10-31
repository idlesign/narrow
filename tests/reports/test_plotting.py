from html.parser import HTMLParser
from pathlib import Path

import pytest

from narrow import NarrowException, SubjectSpec
from narrow.reports import plotting

class ReportParser(HTMLParser):

    def __init__(self) -> None:
        super().__init__()
        self.tags = []
        self.text = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)

@pytest.fixture
def saved_run() -> dict:
    attempts = [{
        "stand": "nginx_static", "app": None, "session": 0, "phase": "measurement", "repeat": 1,
        "clients": clients, "load": {"clients": clients, "requests": 10}, "rps": rps,
        "status": "ok" if rps is not None else "error", "latency_ms": {"p95": 10.0 if rps else None},
        "latency_population": "all HTTP responses", "statuses": {}, "errors": {},
    } for clients, rps in ((1, 100.0), (2, None), (4, 200.0))]
    return {"meta": {"date": "2026-10-31", "generator": {"version": "oha 1.16.0"}}, "attempts": attempts, "status": "failed"}

def test_plot_measured_points_and_gaps(saved_run: dict) -> None:
    figure = plotting.RunReport(saved_run).create_figure()

    throughput, samples = figure.data[:2]
    assert throughput.x == (1, 2, 4)
    assert throughput.y == (100, None, 200)
    assert throughput.connectgaps is False
    assert throughput.line.shape == "linear"
    assert throughput.mode == "lines+markers"
    assert samples.x == (1, 4) and samples.y == (100, 200)
    assert samples.mode == "markers"
    assert samples.legendgroup == throughput.legendgroup
    assert figure.layout.yaxis.title.text == "Successful responses/s (2xx) — ↑ higher is better"
    assert figure.layout.yaxis2.title.text == "Latency (ms) — ↓ lower is better"
    assert figure.layout.xaxis.title.text == "Concurrent HTTP/1.1 clients (log scale)"
    assert throughput.meta["higher_is_better"] is samples.meta["higher_is_better"] is True
    assert all(trace.meta["higher_is_better"] is False for trace in figure.data[2:])

def test_repeats_are_not_pooled_percentiles(saved_run: dict) -> None:
    saved_run["attempts"].extend([
        {**saved_run["attempts"][0], "repeat": 2, "rps": 200.0, "latency_ms": {"p95": 30.0}},
        {**saved_run["attempts"][0], "phase": "warmup", "rps": 9000.0, "latency_ms": {"p95": 100.0}},
    ])

    figure = plotting.RunReport(saved_run).create_figure()

    assert figure.data[0].y[0] == 150.0
    assert figure.data[0].error_y.array[0] == 50.0
    assert figure.data[0].error_y.arrayminus[0] == 50.0
    assert figure.data[0].customdata[0] == [2, 2]
    latency, samples = figure.data[4:6]
    assert latency.meta["summary"] == "median of per-run values"
    assert latency.name == "p95"
    assert latency.y == (20, None, 10)
    assert latency.error_y.array[0] == 10
    assert "all HTTP responses" in latency.hovertemplate
    assert samples.y == (10, 30, 10)

@pytest.mark.parametrize("process", [
    pytest.param({"cleanup_error": "cannot confirm termination"}, id="cleanup-error"),
    pytest.param({"output_truncated": True}, id="truncated-evidence"),
])
def test_failed_target_evidence_excludes_metrics(saved_run: dict, process: dict) -> None:
    saved_run["stands"] = [{
        "session": 0, "stand": "nginx_static", "app": None, "ownership": "owned", "artifacts": "stand-0001",
        "processes": [process],
    }]
    report = plotting.RunReport(saved_run)

    figure = report.create_figure()
    html = report.render_html()

    assert figure.data[0].y == (None, None, None)
    assert figure.data[1].y == ()
    assert (process.get("cleanup_error") or "Process diagnostics truncated") in html
    assert "Target cleanup failed or diagnostics were truncated" in html
    assert "No usable measurements" in html

@pytest.mark.parametrize(("status", "cleanup_error", "expected"), [
    pytest.param("ok", None, 100, id="valid"),
    pytest.param("http_error", None, 100, id="known-http-errors"),
    pytest.param("response_error", None, None, id="wrong-response"),
    pytest.param("error", None, None, id="failed-generator"),
    pytest.param("ok", "cannot confirm generator cleanup", None, id="generator-cleanup"),
])
def test_invalid_attempts_are_gaps(saved_run: dict, status: str, cleanup_error: str | None, expected: float | None) -> None:
    saved_run["attempts"][0].update(status=status, cleanup_error=cleanup_error)

    figure = plotting.RunReport(saved_run).create_figure()

    assert figure.data[0].y[0] == expected

@pytest.mark.parametrize(("value", "expected"), [
    (float("nan"), None), (float("inf"), None), (-1, None), (None, None), (0, 0),
])
def test_missing_or_invalid_metrics_are_not_zeroes(saved_run: dict, value: float | None, expected: float | None) -> None:
    saved_run["attempts"][0]["rps"] = value

    figure = plotting.RunReport(saved_run).create_figure()

    assert figure.data[0].y[0] == expected

def test_build_offline_report_safely_from_saved_data(saved_run: dict, tmp_path: Path) -> None:
    path = tmp_path / "report.html"
    untrusted = "</sCrIpT><img src=x onerror=alert(1)>"
    saved_run["reason"] = untrusted
    saved_run["meta"]["date"] = untrusted
    saved_run["attempts"][0].update(
        stand=untrusted, artifacts=untrusted, errors={untrusted: 1}, request={"url": untrusted, "method": "GET"},
    )

    result = plotting.plot(saved_run, filename=path)
    parser = ReportParser()
    parser.feed(path.read_text())

    assert result == f"{path}"
    assert untrusted in "".join(parser.text)
    assert not any(tag in ("img", "iframe") or tag == "script" and "src" in attrs for tag, attrs in parser.tags)
    assert not any(name.startswith("on") for tag, attrs in parser.tags for name in attrs)
    assert not any(tag == "a" and "href" in attrs for tag, attrs in parser.tags)

@pytest.mark.parametrize("difference", [
    {"request": {"method": "POST", "url": "http://example.test/upload"}},
    {"load": {"clients": 1, "requests": 20}},
    {"latency_population": "all HTTP responses; latency-corrected"},
])
def test_different_requests_loads_and_populations_are_separate_series(saved_run: dict, difference: dict) -> None:
    saved_run["attempts"].append({**saved_run["attempts"][0], **difference})

    figure = plotting.RunReport(saved_run).create_figure()

    assert len(figure.data) == 16
    assert figure.data[0].y[0] == 100
    assert figure.data[0].meta["series"] == "S1"
    assert figure.data[8].meta["series"] == "S2"
    assert figure.data[8].name == "S2 · nginx_static"
    assert figure.data[8].y == (100,)

def test_servers_share_panels_but_keep_distinct_colors_and_legend_groups(saved_run: dict) -> None:
    saved_run["attempts"].append({**saved_run["attempts"][0], "stand": "uwsgi", "latency_ms": {"p95": 20.0}})

    figure = plotting.RunReport(saved_run).create_figure()

    assert [trace.name for trace in figure.data if trace.showlegend] == ["S1 · nginx_static", "S2 · uwsgi"]
    assert figure.data[0].line.color != figure.data[8].line.color
    assert figure.data[0].xaxis == figure.data[8].xaxis == "x"
    assert figure.data[4].xaxis == figure.data[12].xaxis == "x2"
    assert figure.data[4].line.color == figure.data[0].line.color
    assert figure.data[12].line.color == figure.data[8].line.color
    assert figure.data[12].line.dash == "dash" and figure.data[14].line.dash == "dot"
    assert {trace.legendgroup for trace in figure.data[:8]} == {"S1"}
    assert {trace.legendgroup for trace in figure.data[8:]} == {"S2"}
    assert figure.data[8].meta["higher_is_better"] is True
    assert all(trace.meta["higher_is_better"] is False for trace in figure.data[10:])

def test_same_named_builds_are_separate_series_even_with_unchanged_revision(saved_run: dict) -> None:
    identities = [SubjectSpec("server", "same-label", {"runtime": checksum * 64}).serialize() for checksum in ("a", "b")]
    saved_run["attempts"] = [
        {**saved_run["attempts"][0], "subject": subject, "rps": rate}
        for subject, rate in zip(identities, (100, 300), strict=True)
    ]

    figure = plotting.RunReport(saved_run).create_figure()

    assert len(figure.data) == 16
    assert figure.data[0].y == (100,) and figure.data[8].y == (300,)
    assert figure.data[0].meta["subject"]["fingerprint"] == identities[0]["fingerprint"]
    assert figure.data[8].meta["subject"]["fingerprint"] == identities[1]["fingerprint"]
    assert identities[0]["fingerprint"][:12] in figure.data[0].name
    assert identities[1]["fingerprint"][:12] in figure.data[8].name
    assert "server@same-label" in figure.data[0].name

def test_external_target_cleanup_is_not_claimed(saved_run: dict) -> None:
    saved_run["stands"] = [{"stand": "nginx_static", "session": 0, "ownership": "external", "processes": []}]

    html = plotting.RunReport(saved_run).render_html()

    assert "External target: server configuration and lifecycle not observed by narrow" in html
    assert "Recorded process cleanup has no reported errors" not in html

def test_warmup_only_run_has_no_synthetic_measurements(saved_run: dict) -> None:
    saved_run["attempts"] = [{**saved_run["attempts"][0], "phase": "warmup", "status": "error"}]

    figure = plotting.RunReport(saved_run).create_figure()

    assert not figure.data
    assert sum(annotation.text == "No usable measurements" for annotation in figure.layout.annotations) == 2

@pytest.mark.parametrize("data", [None, {}, {"meta": [], "attempts": []}])
def test_reject_malformed_checkpoint_before_writing(data: object, tmp_path: Path) -> None:
    path = tmp_path / "report.html"

    with pytest.raises(NarrowException, match="Not a narrow run checkpoint"):
        plotting.plot(data, filename=path)

    assert not path.exists()
