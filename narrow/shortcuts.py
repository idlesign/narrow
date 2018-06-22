from time import sleep

from ._base import PythonComponent, OSComponent
from .benchers import BENCHERS
from .configuration import bootstrap
from .stands import STANDS
from .utils import get_logger

LOG = get_logger(__name__)


def stand_run(*, stand_alias):

    bootstrap()

    stand = STANDS[stand_alias]

    with stand.setup():
        while True:
            sleep(1)


def gather_stats(*, bencher_alias, stand_alias=None):

    bootstrap()

    stats_items = {}

    versions = []

    stats = {
        'meta': {
            'versions': versions,
            'settings': {},  # e.g. connections_max
        },
        'items': stats_items,
    }

    versions.extend([
        OSComponent().get_version(),
        PythonComponent().get_version(),
    ])

    if not bencher_alias:
        bencher_alias = list(BENCHERS.keys())[0]

    bencher = BENCHERS[bencher_alias]()
    versions.append(bencher.get_version())

    for alias, stand in STANDS.items():

        if not stand_alias or stand_alias == alias:
            LOG.info('')

            version = stand.get_version()
            versions.append(version)

            with stand.setup():
                results = bencher.run(stand)
                stats_items[alias] = results

    return stats
