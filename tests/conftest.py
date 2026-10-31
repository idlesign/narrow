from collections.abc import Callable, Iterator

import pytest

from narrow import STANDS
from narrow.stands._base import Stand


@pytest.fixture
def prepared_stand() -> Iterator[Callable[[str], Stand]]:
    stands = []

    def prepare(alias: str) -> Stand:
        stand = STANDS[alias]()
        stand.resources.prepare()
        stands.append(stand)
        return stand

    yield prepare

    for stand in stands:
        stand.resources.close()
