import json
from collections import namedtuple, OrderedDict
from time import sleep

from .._base import Component
from ..configuration import Settings
from ..exceptions import NarrowException
from ..utils import get_logger

LOG = get_logger(__name__)

BENCHERS = OrderedDict()


def register_bencher(cls):
    """
    :param Type[Bencher] cls:
    """
    BENCHERS[cls.alias] = cls
    return cls


StatsItem = namedtuple('ResultItem', ['clients', 'requests', 'rps'])


def stats_dump(stats, update=True):
    versions = []

    for version in stats['meta']['versions']:
        if version not in versions:
            versions.append(version)

    stats['meta']['versions'] = versions

    base_stats = {}

    if update:
        try:
            base_stats = stats_load()
        except:
            pass

    base_stats.update(stats)

    with open(Settings.FILENAME_STATS_DUMP, 'w') as f:
        f.write(json.dumps(base_stats))


def stats_load():
    with open(Settings.FILENAME_STATS_DUMP) as f:
        stats = f.read()

    loaded = json.loads(stats)

    items = loaded['items']

    for alias, stats_list in items.items():
        items[alias] = [StatsItem(*stats) for stats in stats_list]

    return loaded


class Bencher(Component):

    max_clients = 64

    def run(self, stand):
        url = stand.protocol + '://' + stand.address + '/'
        return self.benchmark(url)

    def benchmark(self, url):
        LOG.info('Running `%s` against %s ...', self.alias, url)

        fire = self.fire
        max_clients = self.max_clients

        results = [
            StatsItem(0, 0, 0),
        ]

        num_clients = 1

        nap = Settings.BENCHER_NAP

        while True:
            num_requests = round((num_clients / 2) * 1000)

            try:
                sleep(nap)

                rps = fire(url, num_clients=num_clients, num_requests=num_requests)

            except NarrowException as e:

                if Settings.STOP_ON_BENCHER_ERROR:
                    raise

                else:
                    rps = None
                    LOG.error('Firing failed for r%s c%s: %s', num_requests, num_clients, e)

            results.append(StatsItem(num_clients, num_requests, rps))

            if num_clients >= max_clients:
                break

            num_clients *= 2

        LOG.debug('Firing finished')

        return results

    @classmethod
    def fire(cls, url, *, num_clients, num_requests):
        raise NotImplementedError
