from ..configuration import Settings, get_path_in_current, get_ssl_tuple
from ..utils import get_logger, run_command, run_command_detached, save_to_tmp
from ._base import Stand, register_stand
from .uwsgi import UwsgiUwsgiSocket

LOG = get_logger(__name__)


class NginxBase(Stand):
    """Build and launch the legacy nginx configuration."""

    address = '127.0.0.1:8001'
    prc_name = 'nginx'

    def get_version(self) -> str:
        return run_command(f"{self.prc_name} -v").replace("nginx version: ", "")

    @property
    def location(self):
        return '/'

    @property
    def location_commands(self):
        return "return 200 'Hello there!';"

    @property
    def port(self):
        _, _, port_ = self.address.partition(':')
        return port_

    def get_config(self) -> str:

        if Settings.LOG_WRITE:
            log_path_access = get_path_in_current(f"log_{self.alias}_access.log")
            log_path_error = get_path_in_current(f"log_{self.alias}_error.log")

        else:
            log_path_access = log_path_error = '/dev/null'

        config = f"""
        pid /tmp/narrow_nginx.pid;
        daemon off;
        worker_processes {Settings.WORKERS};

        events {{
            worker_connections {Settings.MAX_CONNECTIONS};
        }}

        error_log {log_path_error};

        http {{
            server {{

                access_log {log_path_access};

                listen {self.port} backlog={Settings.MAX_CONNECTIONS};
                server_name _;

                location {self.location} {{
                     {self.location_commands}
                }}
            }}
        }}
        """

        return config

    def bootstrap(self):
        super().bootstrap()

        config = self.get_config()
        filepath = save_to_tmp(prefix=self.alias, contents=config)

        LOG.debug('Nginx config: %s', filepath)

        return run_command_detached(f"{self.prc_name} -c {filepath}")


class NginxSslBase(NginxBase):

    alias = 'nginx_ssl'
    address = '127.0.0.1:4443'
    protocol = 'https'

    def get_config(self):
        config = super().get_config()

        cert, key = get_ssl_tuple()

        config = config.replace('backlog=', 'ssl backlog=')
        config = config.replace(
            "server_name _;", f"ssl_certificate {cert};\n"
                              f"ssl_certificate_key {key};\n"
                              "server_name _;")

        return config


@register_stand
class NginxStatic(NginxBase):

    uses_app = False
    alias = 'nginx_static'
    description = 'Nginx static response'


@register_stand
class NginxSslStatic(NginxBase):

    uses_app = False
    alias = 'nginx_ssl_static'
    description = 'Nginx static response using SSL'


class NginxWithUwsgiBackend:

    unix_socket = False

    def __init__(self):
        super().__init__()
        self.child = UwsgiUwsgiSocket(unix=self.unix_socket)

    @property
    def location_commands(self):
        uwsgi = self.child

        address = uwsgi.address

        if self.unix_socket:
            address = f"unix://{address}"

        commands = f"uwsgi_pass {address};\n"
        commands += uwsgi.params_stub

        return commands


@register_stand
class NginxSslUnixUwsgi(NginxWithUwsgiBackend, NginxSslBase):

    unix_socket = True
    alias = 'nginx_ssl_unix_uwsgi'
    description = 'Nginx -> UWSGI -> Unix socket -> uwsgi -> app response. Using SSL'


@register_stand
class NginxSslTcpUwsgi(NginxWithUwsgiBackend, NginxSslBase):

    alias = 'nginx_ssl_tcp_uwsgi'
    description = 'Nginx -> UWSGI -> TCP socket -> uwsgi -> app response. Using SSL'


@register_stand
class NginxUnixUwsgi(NginxWithUwsgiBackend, NginxBase):

    unix_socket = True
    alias = 'nginx_unix_uwsgi'
    description = 'Nginx -> UWSGI -> Unix socket -> uwsgi -> app response'


@register_stand
class NginxTcpUwsgi(NginxWithUwsgiBackend, NginxBase):

    alias = 'nginx_tcp_uwsgi'
    description = 'Nginx -> UWSGI -> TCP socket -> uwsgi -> app response'
