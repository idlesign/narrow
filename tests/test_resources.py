from pathlib import Path

from narrow.resources import RunResources


def test_resources_are_explicit_private_and_independent() -> None:
    first = RunResources()
    second = RunResources()
    assert first.path is second.path is None

    first_path = first.prepare()
    second_path = second.prepare()

    try:
        assert first_path != second_path
        assert first_path.stat().st_mode & 0o777 == 0o700
        (first_path / "server.ini").write_text("partial config")
        assert not (first_path / "backend.sock").exists()

    finally:
        first.close()
        first.close()
        second.close()

    assert not Path(first_path).exists()
    assert not Path(second_path).exists()
    assert first.path is None
