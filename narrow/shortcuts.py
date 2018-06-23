from time import sleep

from ._base import PythonComponent, OSComponent
from .benchers import BENCHERS
from .configuration import bootstrap
from .stands import STANDS
from .apps import APPS
from .utils import get_logger

LOG = get_logger(__name__)


def get_component(registry, alias=None):

    if not alias:
        alias = list(registry.keys())[0]

    return registry[alias]


def stand_run(*, stand_alias, app_alias=None):

    bootstrap()

    stand = STANDS[stand_alias]
    app = get_component(APPS, app_alias)

    with stand.setup(app):
        while True:
            sleep(1)


def gather_stats(*, bencher_alias, stand_alias=None, app_alias=None):

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

    app = get_component(APPS, app_alias)()
    bencher = get_component(BENCHERS, bencher_alias)()
    versions.append(bencher.get_version())

    for alias, stand in STANDS.items():

        if not stand_alias or stand_alias == alias:
            LOG.info('')

            version = stand.get_version()
            versions.append(version)

            with stand.setup(app):
                results = bencher.run(stand)
                stats_items[stand.get_alias_full()] = results

    return stats
