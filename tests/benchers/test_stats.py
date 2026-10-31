import json
from pathlib import Path

import pytest

from narrow import Settings, stats_dump, stats_load
from narrow.benchers import StatsItem


def test_stats_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    stats = {
        "meta": {"date": "2026-10-31 00:00:00", "versions": ["Python 3.12", "Python 3.12"]},
        "items": {"uwsgi_py": [StatsItem(0, 0, 0), StatsItem(1, 500, "123.45"), StatsItem(2, 1000, None)]},
    }

    stats_dump(stats, update=False)
    loaded = stats_load()

    assert loaded == stats
    assert loaded["meta"]["versions"] == ["Python 3.12"]
    assert loaded["items"]["uwsgi_py"][1].rps == "123.45"
    assert type(loaded["items"]["uwsgi_py"][1]).__name__ == "ResultItem"


@pytest.mark.parametrize("previous_contents", ["", "not JSON"])
def test_stats_update_replaces_invalid_dump(
    previous_contents: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    Path(Settings.FILENAME_STATS_DUMP).write_text(previous_contents)
    stats = {"meta": {"versions": []}, "items": {}}

    stats_dump(stats)

    assert stats_load() == stats


def test_stats_load_reports_invalid_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path(Settings.FILENAME_STATS_DUMP).write_text("not JSON")

    with pytest.raises(json.JSONDecodeError, match="Expecting value"):
        stats_load()
