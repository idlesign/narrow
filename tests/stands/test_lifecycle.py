from unittest.mock import Mock

import pytest

from narrow import APPS, STANDS, NarrowException
from narrow.stands import _base
from narrow.stands._base import Stand


class ExampleStand(Stand):
    alias = "example"
    prc_name = "example-server"

    def bootstrap(self) -> None:
        self.start_process([self.prc_name, "arg with spaces"])


@pytest.fixture
def stand_processes(monkeypatch: pytest.MonkeyPatch) -> list:
    handles = []

    def create(*args: object) -> Mock:
        handle = Mock(argv=("example-server",), collect_output=Mock(return_value=(b"out", b"err")), output_truncated=False)
        handles.append(handle)
        return handle

    monkeypatch.setattr(_base, "ProcessHandle", create)
    monkeypatch.setattr(_base.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(_base, "check_available", Mock())
    monkeypatch.setattr(_base, "wait_ready", Mock())
    return handles


@pytest.mark.parametrize("failure", [None, NarrowException("measurement failed"), KeyboardInterrupt()])
def test_cleanup_and_reuse_after_measurement(failure: BaseException | None, stand_processes: list[Mock]) -> None:
    stand = ExampleStand()

    try:

        with stand.setup(APPS["py"]()):
            assert stand.app.alias == "py"

            if failure:
                raise failure

    except (NarrowException, KeyboardInterrupt) as error:
        assert error is failure

    assert stand.prc is stand.app is stand.resources.path is None
    stand_processes[0].stop.assert_called_once()
    assert stand.output == (b"out", b"err")

    with stand.setup(APPS["py"]()):
        pass

    assert len(stand_processes) == 2


def test_parent_failure_stops_started_backend(stand_processes: list, monkeypatch: pytest.MonkeyPatch) -> None:
    stand = ExampleStand()
    stand.child = ExampleStand()
    monkeypatch.setattr(stand, "bootstrap", Mock(side_effect=NarrowException("parent launch failed")))

    with pytest.raises(NarrowException, match="parent launch failed"):

        with stand.setup(APPS["py"]()):
            pytest.fail("unreachable")

    stand_processes[0].stop.assert_called_once()
    assert stand.resources.path is stand.child.resources.path is None


def test_cleanup_error_does_not_mask_primary(stand_processes: list) -> None:
    stand = ExampleStand()

    with pytest.raises(ValueError, match="primary") as raised:

        with stand.setup(APPS["py"]()):
            stand_processes[0].stop.side_effect = NarrowException("cannot stop")
            raise ValueError("primary")

    assert raised.value.__notes__ == ["Cleanup failed: cannot stop"]
    assert stand.cleanup_error == "cannot stop"


def test_readiness_failure_cleans_up(stand_processes: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_base, "wait_ready", Mock(side_effect=NarrowException("not ready")))
    stand = ExampleStand()

    with pytest.raises(NarrowException, match="not ready"):

        with stand.setup(APPS["py"]()):
            pytest.fail("unreachable")

    stand_processes[0].stop.assert_called_once()
    assert stand.resources.path is None


def test_registry_and_constructors_do_not_allocate(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare = Mock(side_effect=AssertionError("unexpected I/O"))
    monkeypatch.setattr(_base.RunResources, "prepare", prepare)

    stands = [stand_class() for stand_class in STANDS.values()]

    assert len(stands) == 8
    prepare.assert_not_called()
    assert all(stand.resources.path is None for stand in stands)
