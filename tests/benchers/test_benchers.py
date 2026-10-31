from types import ModuleType
from unittest.mock import Mock

import pytest

from narrow.benchers import Bencher, H2loadBencher, StatsItem, WeighttpBencher, h2load, weighttp
from narrow.benchers import _base as benchers_base
from narrow.exceptions import BencherException


@pytest.mark.parametrize(
    ("module", "bencher_class", "summary", "command", "expected"),
    [
        pytest.param(
            h2load, H2loadBencher,
            "finished in 1s, 123.45 req/s,\nstatus codes: 10 2xx, 0 3xx, 0 4xx, 0 5xx",
            "h2load --h1 -c 2 -n 10 http://127.0.0.1/", "123.45", id="h2load",
        ),
        pytest.param(
            weighttp, WeighttpBencher,
            "finished in 1s, 10 requests, 123 req/s,\nstatus codes: 10 2xx, 0 3xx, 0 4xx, 0 5xx",
            "weighttp -c 2 -n 10 http://127.0.0.1/", "123", id="weighttp",
        ),
    ],
)
def test_parse_generator_output(
    module: ModuleType, bencher_class: type[Bencher], summary: str, command: str,
    expected: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_command = Mock(return_value=summary)
    monkeypatch.setattr(module, "run_command", run_command)

    result = bencher_class.fire("http://127.0.0.1/", num_clients=2, num_requests=10)

    assert result == expected
    run_command.assert_called_once_with(command)


@pytest.mark.parametrize(("module", "bencher_class"), [(h2load, H2loadBencher), (weighttp, WeighttpBencher)])
def test_reject_unparseable_output(
    module: ModuleType, bencher_class: type[Bencher], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "run_command", Mock(return_value="unrecognized output"))

    with pytest.raises(BencherException, match="Failed to parse results data"):
        bencher_class.fire("http://127.0.0.1/", num_clients=2, num_requests=10)


def test_concurrency_grid_without_running_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    bencher = H2loadBencher()
    fire = Mock(return_value="100.0")
    monkeypatch.setattr(bencher, "fire", fire)
    monkeypatch.setattr(benchers_base, "sleep", Mock())

    results = bencher.benchmark("http://127.0.0.1/")

    assert results == [StatsItem(0, 0, 0)] + [
        StatsItem(clients, clients * 500, "100.0") for clients in (1, 2, 4, 8, 16, 32, 64)
    ]
    assert fire.call_count == 7
