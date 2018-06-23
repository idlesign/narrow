from .apps import APPS
from .benchers import BENCHERS, stats_dump, stats_load
from .configuration import bootstrap, Settings
from .exceptions import NarrowException
from .plotting import plot
from .shortcuts import gather_stats, stand_run
from .stands import STANDS


VERSION = (0, 1, 0)
"""Application version number tuple."""

VERSION_STR = '.'.join(map(str, VERSION))
"""Application version number string."""

