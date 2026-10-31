import pytest

from narrow import MetricSpec


@pytest.mark.parametrize(("metric", "expected"), [
    (
        MetricSpec("Throughput (responses/s)", higher_is_better=True),
        "Throughput (responses/s) — ↑ higher is better",
    ),
    (MetricSpec("CPU time (s)", higher_is_better=False), "CPU time (s) — ↓ lower is better"),
    (MetricSpec("Concurrent clients"), "Concurrent clients"),
])
def test_metric_direction_is_explicit_and_not_http_specific(metric: MetricSpec, expected: str) -> None:
    assert metric.format_label() == expected


@pytest.mark.parametrize(("arguments", "message"), [
    ({"label": "  "}, "label must be a nonempty string"),
    ({"label": None}, "label must be a nonempty string"),
    ({"label": "Latency", "higher_is_better": "lower"}, "higher_is_better must be bool or None"),
    ({"label": "Latency", "higher_is_better": 1}, "higher_is_better must be bool or None"),
])
def test_reject_invalid_metric_description(arguments: dict, message: str) -> None:

    with pytest.raises(ValueError, match=message):
        MetricSpec(**arguments)
