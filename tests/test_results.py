import pytest

from narrow.configuration import LoadSpec
from narrow.results import SampleResult


@pytest.mark.parametrize("parameters", [
    {}, {"requests": 1, "duration": 1}, {"requests": 0}, {"requests": True},
    {"requests": 10, "clients": 0}, {"requests": 10, "timeout": float("nan")},
    {"duration": float("inf")}, {"requests": 1, "qps": -1},
])
def test_reject_invalid_load_before_external_actions(parameters: dict) -> None:

    with pytest.raises(ValueError, match="Specify|must be"):
        LoadSpec(**parameters)


def test_metadata_does_not_include_raw_bytes() -> None:
    sample = SampleResult(clients=1, raw_stdout=b"raw", raw_stderr=b"err")

    metadata = sample.serialize()

    assert "raw_stdout" not in metadata
    assert metadata["rps"] is None
    assert metadata["latency_ms"] == {}
