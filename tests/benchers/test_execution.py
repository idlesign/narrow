import json
from collections.abc import Callable
from unittest.mock import Mock

import pytest

from narrow import NarrowException, ProcessLimits
from narrow.benchers import Bencher, H2loadBencher, OhaBencher, WeighttpBencher
from narrow.benchers import _base as base
from narrow.configuration import LoadSpec
from narrow.processes import CommandResult


@pytest.mark.parametrize(("bencher_class", "filename"), [
    (OhaBencher, "oha/success.json"), (H2loadBencher, "legacy-success.txt"), (WeighttpBencher, "legacy-success.txt"),
])
@pytest.mark.parametrize(("wait_error", "status"), [
    (None, "ok"), (KeyboardInterrupt, "cancelled"), (NarrowException("cleanup failed"), "error"),
])
def test_all_generators_share_limits_and_execution_evidence(
    bencher_class: type[Bencher], filename: str, wait_error: BaseException | type[BaseException] | None,
    status: str, datafix_readbin: Callable[[str], bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = ProcessLimits(cpus=(2,), address_space_bytes=1024**3)
    bencher = bencher_class()
    bencher.provenance = {"version": "unit-test prepared generator"}
    url = "http://127.0.0.1/"
    load = LoadSpec(requests=10)
    argv = tuple(bencher.build_argv(url, load))
    stdout = datafix_readbin(filename)
    execution = {
        "limits": {"requested": limits.serialize(), "applied": {"cpus": [2]}, "error": None},
        "resources": {"user_seconds": 0.25, "system_seconds": 0.125, "max_rss_bytes": 2097152},
    }
    command = CommandResult(argv, 0, stdout, b"diagnostic", 1, execution=execution)
    process = Mock(
        argv=argv, collect_output=Mock(return_value=(stdout, b"diagnostic")),
        get_execution=Mock(return_value=execution), get_returncode=Mock(return_value=0),
        wait=Mock(return_value=command, side_effect=wait_error),
    )
    spawn = Mock(return_value=process)
    monkeypatch.setattr(base, "ProcessHandle", spawn)
    monkeypatch.setattr(base, "monotonic", lambda: 0.0)

    sample = bencher.run_once(url, load, timeout=2, limits=limits)

    assert sample.status == status
    assert sample.execution == execution
    assert json.loads(json.dumps(sample.serialize()))["execution"] == execution
    assert sample.raw_stdout == stdout
    assert spawn.call_args.kwargs["limits"] is limits
    process.start.assert_called_once()
    process.wait.assert_called_once_with(timeout=2)
