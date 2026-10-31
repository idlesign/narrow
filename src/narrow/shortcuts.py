from datetime import datetime
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
    app = get_component(APPS, app_alias)()

    with stand.setup(app):
        while True:
            sleep(1)


def gather_stats(*, bencher_alias, stand_alias=None, app_aliases=None):

    bootstrap()
    app_aliases = app_aliases or list(APPS.keys())

    stats_items = {}

    versions = []

    stats = {
        'meta': {
            'date': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'versions': versions,
            'settings': {},  # e.g. connections_max
        },
        'items': stats_items,
    }

    bencher = get_component(BENCHERS, bencher_alias)()

    versions.extend([
        OSComponent().get_version(),
        PythonComponent().get_version(),
        bencher.get_version(),
    ])

    for alias, stand in STANDS.items():

        if not stand_alias or stand_alias == alias:
            LOG.info('')

            version = stand.get_version()
            versions.append(version)

            for app_alias in app_aliases:
                LOG.info('')
                app = get_component(APPS, app_alias)()
                app_version = app.get_version()

                with stand.setup(app):
                    results = bencher.run(stand)
                    stats_items[stand.get_alias_full()] = results

                sleep(1)  # need to wait for process instead, but for now - cooldown

                versions.append(app_version)

    return stats
