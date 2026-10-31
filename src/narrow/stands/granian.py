import shutil
import sys
from pathlib import Path

from ..configuration import Settings, get_ssl_tuple
from ..exceptions import NarrowException
from ..processes import run_command
from ..readiness import split_address
from ._base import Stand, register_stand


@register_stand
class Granian(Stand):
    """Foreground Granian serving the selected WSGI application over HTTP/1.1."""

    alias = "granian"
    prc_name = "granian"
    address = "127.0.0.1:8004"
    description = "Granian HTTP/1.1 -> WSGI app response"

    def get_executable(self) -> str:
        """Find an optional tool dependency in the current Python environment first."""
        return shutil.which(self.prc_name, path=f"{Path(sys.executable).parent}") or super().get_executable()

    def get_version(self) -> str:
        """Query the selected executable without importing the optional server."""
        return run_command([self.get_executable(), "--version"])

    def check_tools(self) -> None:
        """Reject an unsupported backlog instead of silently increasing it."""
        super().check_tools()

        if Settings.LISTEN_BACKLOG < 128:
            raise NarrowException("Granian requires LISTEN_BACKLOG >= 128")

    def bootstrap(self) -> None:
        """Use the shared process owner with explicit WSGI and connection limits."""
        host, port = split_address(self.address)
        argv = [
            self.get_executable(), "--interface", "wsgi", "--http", "1",
            "--host", host, "--port", f"{port}",
            "--workers", f"{Settings.WORKERS}", "--blocking-threads", f"{Settings.THREADS}",
            "--runtime-threads", "1", "--backlog", f"{Settings.LISTEN_BACKLOG}",
            "--backpressure", f"{Settings.MAX_CONNECTIONS}",
            "--access-log" if Settings.LOG_WRITE else "--no-access-log",
            "--working-dir", f"{self.resources.path}",
        ]

        if self.protocol == "https":
            certificate, key = get_ssl_tuple()
            argv.extend(("--ssl-certificate", certificate, "--ssl-keyfile", key))

        argv.append(f"{self.app.get_entrypoint_path()}:application")
        self.start_process(argv)


@register_stand
class GranianSsl(Granian):
    """Granian WSGI with the packaged test certificate and verified HTTPS probes."""

    alias = "granian_ssl"
    address = "127.0.0.1:4444"
    protocol = "https"
    description = "Granian HTTPS -> WSGI app response"
