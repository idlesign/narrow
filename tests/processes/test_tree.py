import io
import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from narrow.processes import tree

@pytest.fixture
def owned_lineage(tmp_path, monkeypatch):
    state = SimpleNamespace(
        rows={100: {"parent": os.getpid(), "ticks": 1}}, children={}, opened=[], closed=[], signals=[],
        stat_reads=[], on_open=lambda _pid: None, read_error=None,
    )
    original_read_text = Path.read_text
    original_open = Path.open
    original_iterdir = Path.iterdir

    def read_text(path, *arguments, **options):

        if path.is_relative_to("/proc"):
            assert path.name == "stat", "only confirmed identities may be read"
            pid = int(path.parent.name)
            state.stat_reads.append(pid)

            if pid == 200 and state.read_error is not None:
                error, state.read_error = state.read_error, None
                raise error

            if pid not in state.rows:
                raise FileNotFoundError("synthetic process exited")

            row = state.rows[pid]
            fields = ["S", f"{row['parent']}", *["0"] * 17, f"{row['ticks']}"]
            return f"{pid} (synthetic actor) {' '.join(fields)}"

        return original_read_text(path, *arguments, **options)

    def open_file(path, *arguments, **options):

        if path.is_relative_to("/proc"):
            assert path.name == "children", "process environments must never be opened"
            pid = int(path.parts[2])
            assert pid in state.rows
            return io.StringIO(" ".join(f"{child}" for child in state.children.get(pid, [])))

        return original_open(path, *arguments, **options)

    def iterdir(path):

        if path.is_relative_to("/proc"):
            assert path.name == "task", "global process enumeration is forbidden"
            pid = int(path.parent.name)
            return iter([path / f"{pid}"])

        return original_iterdir(path)

    class Anchor:
        def __init__(self, pid, *, start_ticks, allowed_signals):
            self.pid = pid
            self.ticks = start_ticks
            self.allowed = allowed_signals
            self.closed = False

        def open(self):
            state.opened.append((self.pid, self.ticks))

            try:
                state.on_open(self.pid)

                if self.has_exited():
                    raise ProcessLookupError("synthetic PID reuse during open")

            except BaseException:
                self.close()
                raise

            return self

        def has_exited(self):
            assert not self.closed
            return self.pid not in state.rows or state.rows[self.pid]["ticks"] != self.ticks

        def send_signal(self, signum):
            assert signum in self.allowed and not self.has_exited()
            state.signals.append((self.pid, self.ticks, signum))

            if signum == signal.SIGKILL:
                state.rows.pop(self.pid)

        def close(self):

            if not self.closed:
                state.closed.append((self.pid, self.ticks))

            self.closed = True

    clock = [0.0]
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(tree, "PidfdProcess", Anchor)
    monkeypatch.setattr(tree, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    ))
    state.owned = tree.ProcessTree()
    state.owned.open_root(100)

    try:
        yield state

    finally:
        state.owned.close()


def test_owned_discovery_retains_descendants_after_reparenting_and_forces_leak_cleanup(owned_lineage):
    state = owned_lineage
    state.rows.update({200: {"parent": 100, "ticks": 2}, 300: {"parent": 200, "ticks": 3}})
    state.children.update({100: [200], 200: [300]})
    state.owned.discover()
    state.rows.pop(100)
    state.rows[200]["parent"] = 999
    cleanup = state.owned.stop(grace=0.1)

    assert state.opened == [(100, 1), (200, 2), (300, 3)]
    assert not cleanup["remaining_identities_including_zombies"]
    assert [(pid, ticks) for pid, ticks, signum in state.signals if signum == signal.SIGKILL] == [(200, 2), (300, 3)]


@pytest.mark.parametrize("changed", ["identity", "parent", "parent_exit", "cancel"])
def test_owned_discovery_discards_unconfirmed_pidfds_and_preserves_cancellation(owned_lineage, changed):
    state = owned_lineage
    state.rows[200] = {"parent": 100, "ticks": 2}
    state.children[100] = [200]

    def change_after_open(pid):

        if pid != 200:
            return

        if changed == "identity":
            state.rows[200] = {"parent": 999, "ticks": 20}

        elif changed == "parent":
            state.rows[200]["parent"] = 999

        elif changed == "parent_exit":
            state.rows.pop(100)

        else:
            state.read_error = KeyboardInterrupt("cancel descendant anchor")

    state.on_open = change_after_open

    if changed == "cancel":

        with pytest.raises(KeyboardInterrupt, match="cancel descendant anchor"):
            state.owned.discover()

    else:
        state.owned.discover()

    assert list(state.owned.identities.values()) == [{"pid": 100, "start_ticks": 1}]
    assert (200, 2) in state.closed
    assert not state.signals


def test_owned_signals_never_follow_a_reused_retained_pid(owned_lineage):
    state = owned_lineage
    state.rows[200] = {"parent": 100, "ticks": 2}
    state.children[100] = [200]
    state.owned.discover()
    state.rows[200] = {"parent": 999, "ticks": 20}
    state.owned.send_signal(signal.SIGTERM)

    assert state.opened == [(100, 1), (200, 2)]
    assert state.signals == [(100, 1, signal.SIGTERM)]


def test_owned_discovery_fails_closed_when_identity_budget_is_exhausted(owned_lineage, monkeypatch):
    state = owned_lineage
    state.rows[200] = {"parent": 100, "ticks": 2}
    state.children[100] = [200]
    monkeypatch.setattr(tree, "MAX_IDENTITIES", 1)

    with pytest.raises(ValueError, match="identity discovery bound"):
        state.owned.discover()

    assert state.opened == [(100, 1)] and not state.signals
    with pytest.raises(ValueError, match="identity discovery bound"):
        state.owned.stop(grace=0.1)

    assert not state.owned.get_remaining()
    assert all(pid == 100 for pid, _ticks, _signum in state.signals)

def test_cleanup_cancellation_does_not_skip_kill_or_hide_the_first_discovery_error(owned_lineage, monkeypatch):
    state = owned_lineage
    anchor = state.owned.anchors[(100, 1)]
    send_signal = anchor.send_signal

    def send(signum):

        if signum == signal.SIGCONT:
            raise KeyboardInterrupt("synthetic continue cancellation")

        send_signal(signum)

    def discover():
        raise ValueError("synthetic discovery failure")

    monkeypatch.setattr(anchor, "send_signal", send)
    monkeypatch.setattr(state.owned, "discover", discover)

    with pytest.raises(KeyboardInterrupt, match="continue cancellation") as caught:
        state.owned.stop(grace=0.1)

    assert state.signals == [(100, 1, signal.SIGKILL)] and not state.owned.get_remaining()
    assert "synthetic discovery failure" in caught.value.__notes__[0]
