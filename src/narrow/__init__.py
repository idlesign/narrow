from .apps import APPS
from .benchers import BENCHERS, stats_dump, stats_load
from .configuration import Settings, bootstrap
from .exceptions import NarrowException
from .plotting import plot
from .shortcuts import gather_stats, stand_run
from .stands import STANDS

__all__ = [
    "APPS",
    "BENCHERS",
    "STANDS",
    "VERSION",
    "NarrowException",
    "Settings",
    "bootstrap",
    "gather_stats",
    "plot",
    "stand_run",
    "stats_dump",
    "stats_load",
]

VERSION = "0.1.0"
"""Application version number."""
