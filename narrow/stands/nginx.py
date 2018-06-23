from ._base import register_stand, Stand
from .uwsgi import UwsgiUwsgiSocket
from ..configuration import get_path_in_current, get_ssl_tuple, Settings
from ..utils import run_command_detached, save_to_tmp, get_logger, run_command, check_process

LOG = get_logger(__name__)


class NginxBase(Stand):

    address = '127.0.0.1:8001'
    prc_name = 'nginx'

    def get_version(self):
        return run_command('%s -v' % self.prc_name).replace('nginx version: ', '')

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

    def get_config(self):

        if Settings.LOG_WRITE:
            log_path_access = get_path_in_current('log_%s_access.log' % self.alias)
            log_path_error = get_path_in_current('log_%s_error.log' % self.alias)

        else:
            log_path_access = log_path_error = '/dev/null'

        config = '''
        pid /tmp/narrow_nginx.pid;
        daemon off;
        worker_processes %(workers)s;
        
        events {
            worker_connections %(max_connections)s;
        }
        
        error_log %(log_error)s;
        
        http {
            server {
            
                access_log %(log_access)s;
                
                listen %(port)s backlog=%(max_connections)s;
                server_name _;
                
                location %(location_path)s {
                     %(location_commands)s
                }
            }
        }
        ''' % dict(
            port=self.port,
            max_connections=Settings.MAX_CONNECTIONS,
            location_path=self.location,
            location_commands=self.location_commands,
            log_access=log_path_access,
            log_error=log_path_error,
            workers=Settings.WORKERS,
        )

        return config

    def bootstrap(self):
        super().bootstrap()

        config = self.get_config()
        filepath = save_to_tmp(prefix=self.alias, contents=config)

        LOG.debug('Nginx config: %s', filepath)

        return run_command_detached('%s -c %s' % (self.prc_name, filepath))


class NginxSslBase(NginxBase):

    alias = 'nginx_ssl'
    address = '127.0.0.1:4443'
    protocol = 'https'

    def get_config(self):
        config = super().get_config()

        cert, key = get_ssl_tuple()

        config = config.replace('backlog=', 'ssl backlog=')
        config = config.replace(
            'server_name _;', 'ssl_certificate %s;\n'
                              'ssl_certificate_key %s;\n'
                              'server_name _;' % (cert, key))

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
            address = 'unix://%s' % address

        commands = 'uwsgi_pass %s;\n' % address
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
