from dataclasses import replace

import pytest

from narrow.benchers import Bencher, H2loadBencher, WeighttpBencher
from narrow.configuration import LoadSpec
from narrow.exceptions import BencherException


@pytest.mark.parametrize(("bencher_class", "summary", "expected"), [
    (H2loadBencher, "finished in 1s, 123.45 req/s,\nstatus codes: 10 2xx, 0 3xx, 0 4xx, 0 5xx", 123.45),
    (WeighttpBencher, "finished in 1s, 10 requests, 123 req/s,\nstatus codes: 10 2xx, 0 3xx, 0 4xx, 0 5xx", 123.0),
])
def test_parse_numeric_generator_rate(bencher_class: type[Bencher], summary: str, expected: float) -> None:
    result = bencher_class().parse_report(summary.encode(), LoadSpec(requests=10))

    assert result.rps == expected
    assert isinstance(result.rps, float)
    assert result.status == "ok"


@pytest.mark.parametrize("bencher_class", [H2loadBencher, WeighttpBencher])
def test_reject_unparseable_output(bencher_class: type[Bencher]) -> None:

    with pytest.raises(BencherException, match="Cannot parse"):
        bencher_class().parse_report(b"unrecognized output", LoadSpec(requests=10))


def test_partial_success_is_not_reported_as_no_success() -> None:
    result = H2loadBencher().parse_report(
        b"finished in 1s, 10.0 req/s,\nstatus codes: 4 2xx, 0 3xx, 0 4xx, 6 5xx", LoadSpec(requests=10),
    )

    assert result.status == "http_error"
    assert result.successful_responses == 4
    assert result.rps == 4.0
    assert "4/10" in result.reason


def test_weighttp_keepalive_is_explicit() -> None:
    load = LoadSpec(requests=10)
    bencher = WeighttpBencher()

    assert "-k" in bencher.build_argv("http://127.0.0.1/", load)
    assert "-k" not in bencher.build_argv("http://127.0.0.1/", replace(load, keepalive=False))


@pytest.mark.parametrize(("url", "load"), [
    ("https://127.0.0.1/", LoadSpec(requests=10)), ("http://127.0.0.1/", LoadSpec(duration=1)),
    ("http://127.0.0.1/", LoadSpec(requests=10, qps=2)),
])
def test_legacy_unsupported_policy_is_rejected(url: str, load: LoadSpec) -> None:

    with pytest.raises(BencherException, match="unsupported"):
        WeighttpBencher().validate_load(url, load)
