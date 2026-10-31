from pathlib import Path
from unittest.mock import Mock

import pytest

from narrow import stats_dump, stats_load
from narrow.reports import artifacts
from narrow.reports.artifacts import RunStore


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "run"
    store = RunStore(path, {"original": True})
    store.prepare()

    with pytest.raises(FileExistsError):
        RunStore(path, {"replacement": True}).prepare()

    assert stats_load(path)["meta"] == {"original": True}


def test_failed_atomic_write_keeps_previous_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "run.json"
    original = {"meta": {}, "attempts": []}
    stats_dump(original, path)
    monkeypatch.setattr(artifacts.os, "replace", Mock(side_effect=OSError("disk error")))

    with pytest.raises(OSError, match="disk error"):
        stats_dump({"meta": {"new": True}, "attempts": []}, path)

    assert stats_load(path) == original
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["run.json"]
