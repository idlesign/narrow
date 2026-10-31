from pathlib import Path
from time import sleep

from .apps import APPS
from .exceptions import NarrowException
from .runner import Runner, RunOptions
from .stands import STANDS


def stand_run(*, stand_alias: str, app_alias: str = "py") -> None:
    """Run one selected local stand until interrupted, with owned cleanup."""

    if stand_alias not in STANDS or app_alias not in APPS:
        raise NarrowException("Unknown stand or application")

    stand = STANDS[stand_alias]()
    app = APPS[app_alias]() if stand.uses_app else None

    with stand.setup(app):

        while True:
            stand.check_running()
            sleep(0.1)


def gather_stats(options: RunOptions, output: Path) -> dict:
    """Execute an explicit run matrix and save evidence in a new directory."""
    return Runner(options).run(output)
