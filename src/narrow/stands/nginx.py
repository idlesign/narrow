from ..configuration import Settings, get_ssl_tuple
from ..processes import run_command
from ._base import Stand, register_stand
from .uwsgi import UwsgiUwsgiSocket


def quote_path(path: str) -> str:
    """Quote an nginx argument, including paths containing spaces or quotes."""
    return "\"" + path.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n") + "\""


class NginxBase(Stand):
    """A foreground nginx instance with private PID/config paths and a loopback listener."""

    address = "127.0.0.1:8001"
    prc_name = "nginx"

    def get_version(self) -> str:
        """Read nginx's version, which is printed to stderr."""
        return run_command([self.prc_name, "-v"]).replace("nginx version: ", "")

    @property
    def location_commands(self) -> str:
        return "return 200 'Hello there!';"

    def get_config(self) -> str:
        """Build a configuration with no shared PID, log or request-body paths."""
        secure = self.protocol == "https"
        tls = ""

        if secure:
            cert, key = get_ssl_tuple()
            tls = f"ssl_certificate {quote_path(cert)};\nssl_certificate_key {quote_path(key)};"

        pid_path = quote_path(f"{self.resources.path}/nginx.pid")
        body_path = quote_path(f"{self.resources.path}/body")
        uwsgi_path = quote_path(f"{self.resources.path}/uwsgi-temp")
        access_log = "/dev/stderr" if Settings.LOG_WRITE else "off"
        tls_option = " ssl" if secure else ""

        return f"""
        pid {pid_path};
        daemon off;
        worker_processes {Settings.WORKERS};
        error_log stderr warn;
        events {{ worker_connections {Settings.MAX_CONNECTIONS}; }}
        http {{
            access_log {access_log};
            client_body_temp_path {body_path};
            uwsgi_temp_path {uwsgi_path};
            proxy_temp_path {quote_path(f"{self.resources.path}/proxy-temp")};
            fastcgi_temp_path {quote_path(f"{self.resources.path}/fastcgi-temp")};
            scgi_temp_path {quote_path(f"{self.resources.path}/scgi-temp")};
            server {{
                listen {self.address}{tls_option} backlog={Settings.LISTEN_BACKLOG};
                server_name localhost;
                {tls}
                location / {{ {self.location_commands} }}
            }}
        }}
        """

    def bootstrap(self) -> None:
        """Start foreground nginx with only this invocation's private prefix."""
        filepath = self.write_config("nginx.conf", self.get_config())
        self.start_process([self.prc_name, "-p", f"{self.resources.path}/", "-c", f"{filepath}"])


class NginxSslBase(NginxBase):
    address = "127.0.0.1:4443"
    protocol = "https"


@register_stand
class NginxStatic(NginxBase):
    uses_app = False
    alias = "nginx_static"
    description = "Nginx static response"


@register_stand
class NginxSslStatic(NginxSslBase):
    uses_app = False
    alias = "nginx_ssl_static"
    description = "Nginx static response using TLS"


class NginxWithUwsgiBackend:
    unix_socket = False

    def __init__(self) -> None:
        super().__init__()
        self.child = UwsgiUwsgiSocket(unix=self.unix_socket)

    @property
    def location_commands(self) -> str:
        address = f"unix:{self.child.address}" if self.unix_socket else self.child.address
        return f"uwsgi_pass {quote_path(address)};\n{self.child.params_stub}"


@register_stand
class NginxSslUnixUwsgi(NginxWithUwsgiBackend, NginxSslBase):
    unix_socket = True
    alias = "nginx_ssl_unix_uwsgi"
    description = "Nginx HTTPS -> uWSGI over Unix socket -> app"


@register_stand
class NginxSslTcpUwsgi(NginxWithUwsgiBackend, NginxSslBase):
    alias = "nginx_ssl_tcp_uwsgi"
    description = "Nginx HTTPS -> uWSGI over TCP -> app"


@register_stand
class NginxUnixUwsgi(NginxWithUwsgiBackend, NginxBase):
    unix_socket = True
    alias = "nginx_unix_uwsgi"
    description = "Nginx HTTP -> uWSGI over Unix socket -> app"


@register_stand
class NginxTcpUwsgi(NginxWithUwsgiBackend, NginxBase):
    alias = "nginx_tcp_uwsgi"
    description = "Nginx HTTP -> uWSGI over TCP -> app"
