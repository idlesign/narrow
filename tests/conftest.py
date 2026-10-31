from collections.abc import Iterator
from pathlib import Path

import pytest

from narrow import STANDS


@pytest.fixture(scope="session", autouse=True)
def stand_socket_cleanup() -> Iterator[None]:
    # Legacy registry construction creates placeholder Unix socket files on import.
    socket_paths = [
        Path(stand.child.address)
        for stand in STANDS.values()
        if stand.child is not None and stand.child.unix
    ]

    yield

    for socket_path in socket_paths:
        socket_path.unlink(missing_ok=True)
