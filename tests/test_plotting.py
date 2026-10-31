from unittest.mock import Mock

import pytest

from narrow import plotting
from narrow.benchers import StatsItem


def test_plot_saved_stats_without_opening_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    plot_method = Mock(return_value="narrow-stats.html")
    monkeypatch.setattr(plotting, "plot_method", plot_method)
    stats = {
        "meta": {"date": "2026-10-31 00:00:00", "versions": ["Python 3.12"]},
        "items": {"uwsgi_py": [StatsItem(0, 0, 0), StatsItem(1, 500, 100), StatsItem(2, 1000, None)]},
    }

    result = plotting.plot(stats)

    assert result == "narrow-stats.html"
    figure = plot_method.call_args.args[0]
    assert len(figure["data"]) == 1
    assert figure["data"][0].x == (0, 1)
    assert figure["data"][0].y == (0, 100)
    assert figure["layout"].yaxis.title.text == "RPS"
