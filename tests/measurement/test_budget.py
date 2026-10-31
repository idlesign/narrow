import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow.measurement import budget
from narrow.measurement.budget import Budget

@pytest.fixture
def budget_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    current = [100.0]
    monkeypatch.setattr(budget, "time", SimpleNamespace(monotonic=lambda: current[0]))
    monkeypatch.setattr(budget, "read_boot_id", lambda: "same-boot")
    return current

def test_saved_budget_is_not_reset_between_invocations(
    tmp_path: Path, budget_clock: list[float],
) -> None:
    path = tmp_path / "budget.json"
    first = Budget(path, 30)
    assert first.data is first.deadline is None and not path.exists()

    with pytest.raises(RuntimeError, match="opened explicitly"):
        first.remaining()

    first.open()
    initial = path.read_bytes()
    budget_clock[0] = 112
    second = Budget(path, 30).open()
    assert first.deadline == second.deadline == 130 and second.remaining() == 18
    budget_clock[0] = 135
    assert Budget(path, 30).open().remaining() == -5
    assert path.read_bytes() == initial

    with pytest.raises(ValueError, match="cannot replace"):
        Budget(path, 40).open()

    with pytest.raises(RuntimeError, match="already open"):
        first.open()

@pytest.mark.parametrize("seconds", [0.25, 1, 29, 901, 3600])
def test_common_budget_is_not_limited_to_one_benchmark_policy(
    tmp_path: Path, budget_clock: list[float], seconds: float,
) -> None:
    opened = Budget(tmp_path / "budget.json", seconds).open()

    assert opened.remaining() == seconds and opened.deadline == 100 + seconds

@pytest.mark.parametrize("seconds", [True, "30", 0, -1, float("nan"), float("inf"), -(10**1000), 10**1000])
def test_invalid_limits_do_not_create_a_record(tmp_path: Path, seconds: object) -> None:
    path = tmp_path / "budget.json"

    with pytest.raises(ValueError, match="finite and positive"):
        Budget(path, seconds)

    assert not path.exists()

@pytest.mark.parametrize("field,value,message", [
    ("boot_id", "different-boot", "cannot replace"),
    ("budget_seconds", True, "saved budget seconds"),
    ("budget_seconds", float("nan"), "saved budget seconds"),
    ("monotonic_start", 101, "future"),
    ("monotonic_start", float("nan"), "saved monotonic"),
    ("monotonic_start", 10**1000, "saved monotonic"),
    ("utc_start", "not a timestamp", "valid UTC"),
    ("utc_start", "2026-10-31T12:00:00", "must use UTC"),
])
def test_saved_budget_rejects_inconsistent_fields(
    tmp_path: Path, budget_clock: list[float], field: str, value: object, message: str,
) -> None:
    path = tmp_path / "budget.json"
    opened = Budget(path, 30).open()
    record = {**opened.data, field: value}
    path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match=message):
        Budget(path, 30).open()

@pytest.mark.parametrize("payload,message", [
    ("", "bounded regular"), ("{}", "fields"), ("not JSON", "invalid saved budget JSON"),
    ('{"boot_id":"first","boot_id":"duplicate"}', "duplicate"),
    ("x" * 4097, "bounded regular"),
])
def test_saved_budget_refuses_unusable_records(
    tmp_path: Path, budget_clock: list[float], payload: str, message: str,
) -> None:
    path = tmp_path / "budget.json"
    path.write_text(payload)

    with pytest.raises(ValueError, match=message):
        Budget(path, 30).open()

    assert path.read_text() == payload

def test_saved_budget_never_follows_a_symlink(tmp_path: Path, budget_clock: list[float]) -> None:
    real = tmp_path / "real.json"
    Budget(real, 30).open()
    link = tmp_path / "budget.json"
    link.symlink_to(real)

    with pytest.raises(OSError, match="symbolic|Too many levels"):
        Budget(link, 30).open()

def test_cleanup_reserve_never_renews_or_spends_equal_remaining_time(
    tmp_path: Path, budget_clock: list[float],
) -> None:
    opened = Budget(tmp_path / "budget.json", 30).open()
    budget_clock[0] = 112

    assert opened.require(reserve=5, minimum=10) == 125

    with pytest.raises(TimeoutError, match="cleanup reserve"):
        opened.require(reserve=6, minimum=12)

    assert opened.deadline == 130

@pytest.mark.parametrize("reserve", [True, float("nan"), float("inf"), 10**1000, -1])
def test_cleanup_reserve_must_be_nonnegative_and_finite(
    tmp_path: Path, budget_clock: list[float], reserve: object,
) -> None:
    opened = Budget(tmp_path / "budget.json", 30).open()

    with pytest.raises(ValueError, match="finite and nonnegative"):
        opened.require(reserve=reserve)

@pytest.mark.parametrize("now", [99, -1, float("inf"), float("nan")])
def test_invalid_or_backwards_clock_cannot_grant_time(
    tmp_path: Path, budget_clock: list[float], now: float,
) -> None:
    opened = Budget(tmp_path / "budget.json", 30).open()
    budget_clock[0] = now

    with pytest.raises(ValueError, match="clock"):
        opened.remaining()

    assert opened.deadline == 130

def test_cancelled_create_keeps_the_original_record_and_deadline(
    tmp_path: Path, budget_clock: list[float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "budget.json"
    fsync = Mock(side_effect=KeyboardInterrupt("cancel budget publication"))
    monkeypatch.setattr(budget, "os", SimpleNamespace(
        open=os.open, close=os.close, fstat=os.fstat, read=os.read, fsync=fsync,
        O_RDONLY=os.O_RDONLY, O_CLOEXEC=os.O_CLOEXEC, O_NOFOLLOW=os.O_NOFOLLOW, O_NONBLOCK=os.O_NONBLOCK,
    ))

    with pytest.raises(KeyboardInterrupt, match="cancel budget publication"):
        Budget(path, 30).open()

    assert path.exists()
    budget_clock[0] = 112
    reopened = Budget(path, 30).open()
    assert reopened.deadline == 130 and reopened.remaining() == 18

def test_cancelled_read_closes_the_saved_record_without_modification(
    tmp_path: Path, budget_clock: list[float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "budget.json"
    Budget(path, 30).open()
    original = path.read_bytes()
    close = Mock(wraps=os.close)
    monkeypatch.setattr(budget, "os", SimpleNamespace(
        open=os.open, close=close, fstat=os.fstat, read=Mock(side_effect=KeyboardInterrupt("cancel budget read")),
        O_RDONLY=os.O_RDONLY, O_CLOEXEC=os.O_CLOEXEC, O_NOFOLLOW=os.O_NOFOLLOW, O_NONBLOCK=os.O_NONBLOCK,
    ))

    with pytest.raises(KeyboardInterrupt, match="cancel budget read"):
        Budget(path, 30).open()

    close.assert_called_once()

    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(close.call_args.args[0])

    assert path.read_bytes() == original

def test_cancelled_partial_write_cannot_start_a_fresh_budget(
    tmp_path: Path, budget_clock: list[float], monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "budget.json"
    original_open = Path.open

    @contextmanager
    def create_partial() -> Iterator[SimpleNamespace]:

        with original_open(path, "x", encoding="utf-8") as output:

            def write(payload: str) -> int:
                output.write(payload[:8])
                output.flush()
                raise KeyboardInterrupt("cancel partial budget write")

            yield SimpleNamespace(write=write)

    def open_record(source: Path, *arguments: object, **options: object) -> object:

        if source == path and arguments == ("x",):
            return create_partial()

        return original_open(source, *arguments, **options)

    monkeypatch.setattr(Path, "open", open_record)
    initial = Budget(path, 30)

    with pytest.raises(KeyboardInterrupt, match="cancel partial budget write"):
        initial.open()

    assert initial.data is initial.deadline is None and path.stat().st_size == 8
    budget_clock[0] = 112

    with pytest.raises(ValueError, match="invalid saved budget JSON"):
        Budget(path, 30).open()

    assert path.stat().st_size == 8
