import logging
import shutil
import sys
from importlib.util import find_spec
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .._base import Component
from ..apps._base import App
from ..exceptions import NarrowException
from ..processes import ProcessHandle
from ..readiness import check_available, wait_ready
from ..resources import RunResources

LOG = logging.getLogger(__name__)
STANDS = {}


def register_stand(cls: type) -> type:
    """Register the class, keeping imports and construction free of external effects."""
    STANDS[cls.alias] = cls
    return cls


class Stand(Component):
    """A foreground local topology, owning only its own processes and temporary files."""

    address = "127.0.0.1:8000"
    protocol = "http"
    uses_app = True
    unix = False

    def __init__(self) -> None:
        self.prc: ProcessHandle | None = None
        self.child: Stand | None = None
        self.app: App | None = None
        self.resources = RunResources()
        self.configurations: dict[str, str] = {}
        self.output: tuple[bytes, bytes] = (b"", b"")
        self.output_truncated = False
        self.cleanup_error: str | None = None
        self.argv: tuple[str, ...] = ()

    def get_alias_full(self) -> str:
        """Name static stands without an irrelevant application suffix."""
        return f"{self.alias}_{self.app.alias}" if self.uses_app and self.app else self.alias

    def check_tools(self) -> None:
        """Check the selected topology only; unrelated servers remain optional."""

        if not shutil.which(self.prc_name):
            raise NarrowException(f"Server {self.prc_name} is not installed")

        if self.protocol == "uwsgi" or self.alias in ("uwsgi", "uwsgi_ssl"):

            if find_spec("uwsgiconf") is None:
                raise NarrowException("uWSGI stands require the narrow[uwsgi] extra")

        if self.child:
            self.child.check_tools()

    def check_running(self) -> None:
        """Require every server in this topology to remain alive."""

        if self.prc is None:
            raise NarrowException("Stand has not started")

        self.prc.check_running()

        if self.child:
            self.child.check_running()

    def write_config(self, filename: str, contents: str) -> Path:
        """Retain the generated configuration for the run's evidence."""

        if self.resources.path is None:
            raise NarrowException("Stand resources have not been prepared")

        self.configurations[filename] = contents
        path = self.resources.path / filename
        path.write_text(contents, encoding="utf-8")
        return path

    def start_process(self, argv: list[str]) -> None:
        """Retain ownership before starting, including a partially failed launch."""
        self.prc = ProcessHandle(argv)
        self.argv = self.prc.argv
        self.prc.start()

    @contextmanager
    def setup(self, app: App | None = None, *, timeout: float = 5.0) -> Iterator["Stand"]:
        """Prepare, start and verify the stand; clean up even when bootstrap fails."""

        if self.resources.path is not None:
            raise NarrowException("Stand is already running")

        if self.uses_app and app is None:
            raise NarrowException("This stand requires an application")

        self.check_tools()
        self.app = app if self.uses_app else None
        self.cleanup_error = None
        self.configurations = {}
        self.output = (b"", b"")
        self.output_truncated = False

        try:
            self.resources.prepare()

            if not self.unix:
                check_available(self.address)

            with self.child.setup(self.app, timeout=timeout) if self.child else nullcontext():

                try:
                    self.bootstrap()
                    wait_ready(self, timeout=timeout)
                    yield self

                finally:
                    self.finalize()

        finally:
            self.app = None
            self.resources.close()

    def bootstrap(self) -> None:
        raise NotImplementedError

    def finalize(self) -> None:
        """Stop only owned processes; preserve a primary exception on cleanup failure."""
        primary = sys.exception()

        try:

            if self.prc is not None:
                self.prc.stop()

        except (OSError, NarrowException) as error:
            self.cleanup_error = f"{error}"

            if primary is None:
                raise

            primary.add_note(f"Cleanup failed: {error}")
            LOG.error("Stand cleanup failed: %s", error)

        finally:

            if self.prc is not None:
                self.output = self.prc.collect_output()
                self.output_truncated = self.prc.output_truncated
                self.prc = None
