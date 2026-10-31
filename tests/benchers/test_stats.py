import json
from pathlib import Path

import pytest

from narrow import NarrowException, stats_dump, stats_load
from narrow.reports.artifacts import RunStore
from narrow.reports.results import SampleResult


def test_stats_round_trip(tmp_path: Path) -> None:
    stats = {"meta": {"date": "2026-10-31"}, "attempts": [{"clients": 1, "rps": 123.45}]}
    path = tmp_path / "run.json"

    stats_dump(stats, path)

    assert stats_load(path) == stats
    assert stats_load(tmp_path) == stats


def test_load_reports_corrupt_input(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text("not JSON")

    with pytest.raises(json.JSONDecodeError, match="Expecting value"):
        stats_load(path)

    path.write_text('{"meta":{},"items":{}}')

    with pytest.raises(NarrowException, match="Not a narrow run"):
        stats_load(path)


def test_save_attempt_before_next_failure(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "new-run", {"date": "2026-10-31"})
    store.prepare()
    sample = SampleResult(1, status="invalid", raw_stdout=b"partial\x00json", raw_stderr=b"diagnostic")

    store.save_attempt(sample, {"phase": "measurement"})

    loaded = stats_load(store.path)
    assert loaded["attempts"][0]["status"] == "invalid"
    assert (store.path / "attempt-0001/stdout.bin").read_bytes() == sample.raw_stdout
