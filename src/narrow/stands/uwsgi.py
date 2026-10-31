import sys
from typing import TYPE_CHECKING

from ..configuration import Settings, get_ssl_tuple
from ..processes import run_command
from ._base import Stand, register_stand

if TYPE_CHECKING:
    from uwsgiconf.options.networking_sockets import Socket
    from uwsgiconf.presets.nice import PythonSection


class Uwsgi(Stand):
    """Foreground uWSGI master with direct sockets, not an implicit HTTP router."""

    prc_name = "uwsgi"
    params_stub = "\n".join(
        f"uwsgi_param {name} {value};" for name, value in (
            ("QUERY_STRING", "$query_string"), ("REQUEST_METHOD", "$request_method"),
            ("CONTENT_TYPE", "$content_type"), ("CONTENT_LENGTH", "$content_length"),
            ("REQUEST_URI", "$request_uri"), ("PATH_INFO", "$document_uri"),
            ("DOCUMENT_ROOT", "$document_root"), ("SERVER_PROTOCOL", "$server_protocol"),
            ("REMOTE_ADDR", "$remote_addr"), ("REMOTE_PORT", "$remote_port"),
            ("SERVER_ADDR", "$server_addr"), ("SERVER_PORT", "$server_port"), ("SERVER_NAME", "$server_name"),
        )
    )

    def get_version(self) -> str:
        """Query only the selected server executable."""
        version = run_command([self.prc_name, "--version"])
        return f"uwsgi {version}"

    def generate_section(self, listener: "Socket") -> "PythonSection":
        """Build the foreground master's configuration for the selected application."""
        from uwsgiconf.presets.nice import PythonSection

        return PythonSection(
            wsgi_module=self.app.get_entrypoint_path(), process_prefix=self.alias,
            params_python={"python_home": sys.prefix},
            workers=Settings.WORKERS, threads=Settings.THREADS, log_into="/dev/stderr",
        ).networking.register_socket(listener).networking.set_basic_params(
            queue_size=Settings.MAX_CONNECTIONS,
        ).logging.set_basic_params(no_requests=not Settings.LOG_WRITE)

    @property
    def socket(self) -> "Socket":
        raise NotImplementedError

    def bootstrap(self) -> None:
        """Create configuration/socket paths only inside an explicit stand run."""

        if self.unix:
            self.address = f"{self.resources.path / "backend.sock"}"

        config = self.generate_section(self.socket).as_configuration().format()
        filepath = self.write_config("uwsgi.ini", config)
        self.start_process([self.prc_name, "--ini", f"{filepath}"])


@register_stand
class UwsgiPy(Uwsgi):
    alias = "uwsgi"
    address = "127.0.0.1:8002"
    description = "uWSGI direct HTTP socket -> app response"

    @property
    def socket(self) -> "Socket":
        from uwsgiconf.presets.nice import PythonSection

        return PythonSection.networking.sockets.http(self.address, http11=True)


@register_stand
class UwsgiSslPy(Uwsgi):
    alias = "uwsgi_ssl"
    address = "127.0.0.1:4443"
    protocol = "https"
    description = "uWSGI direct HTTPS socket -> app response"

    @property
    def socket(self) -> "Socket":
        from uwsgiconf.presets.nice import PythonSection

        cert, key = get_ssl_tuple()
        return PythonSection.networking.sockets.https(self.address, cert=cert, key=key)


class UwsgiUwsgiSocket(Uwsgi):
    protocol = "uwsgi"
    address = "127.0.0.1:8003"

    def __init__(self, *, unix: bool = False) -> None:
        super().__init__()
        self.unix = unix
        self.alias = "uwsgi_uwsgi_unix" if unix else "uwsgi_uwsgi_tcp"

    @property
    def socket(self) -> "Socket":
        from uwsgiconf.presets.nice import PythonSection

        return PythonSection.networking.sockets.uwsgi(self.address)
