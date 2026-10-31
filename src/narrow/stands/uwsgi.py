from uwsgiconf.presets.nice import PythonSection

from ._base import register_stand, Stand
from ..configuration import get_path_in_current, get_path_in_package, Settings, get_ssl_tuple
from ..utils import create_socket_file, get_logger, run_command_detached, run_command, check_process

sockets = PythonSection.networking.sockets

LOG = get_logger(__name__)


class Uwsgi(Stand):
    """

    For SSL support compile uwsgi on system having:
        sudo apt install libssl-dev

    """

    prc_name = 'uwsgi'

    @property
    def params_stub(self):
        params = (
            'uwsgi_param QUERY_STRING $query_string;\n'
            'uwsgi_param REQUEST_METHOD $request_method;\n'
            'uwsgi_param CONTENT_TYPE $content_type;\n'
            'uwsgi_param CONTENT_LENGTH $content_length;\n'
            'uwsgi_param REQUEST_URI $request_uri;\n'
            'uwsgi_param PATH_INFO $document_uri;\n'
            'uwsgi_param DOCUMENT_ROOT $document_root;\n'
            'uwsgi_param SERVER_PROTOCOL $server_protocol;\n'
            'uwsgi_param REMOTE_ADDR $remote_addr;\n'
            'uwsgi_param REMOTE_PORT $remote_port;\n'
            'uwsgi_param SERVER_ADDR $server_addr;\n'
            'uwsgi_param SERVER_PORT $server_port;\n'
            'uwsgi_param SERVER_NAME $server_name;\n'
        )
        return params

    def get_version(self):
        return 'uwsgi %s' % run_command('%s --version' % self.prc_name)

    def generate_section(self, socket):

        alias = self.alias

        if Settings.LOG_WRITE:
            log_path = get_path_in_current('log_%s.log' % alias)

        else:
            log_path = '/dev/null'

        section = PythonSection(
            wsgi_module=self.app.get_entrypoint_path(),
            process_prefix=alias,
            workers=Settings.WORKERS,
            threads=Settings.THREADS,
            log_into=log_path,

        ).networking.register_socket(
            socket,

        ).networking.set_basic_params(
            queue_size=Settings.MAX_CONNECTIONS,
        )

        return section

    @property
    def socket(self):
        raise NotImplementedError

    def bootstrap(self):
        super().bootstrap()

        section = self.generate_section(self.socket)
        config = section.as_configuration()
        filepath = config.tofile()

        LOG.debug('Uwsgi config: %s', filepath)

        return run_command_detached('%s --ini %s' % (self.prc_name, filepath))


@register_stand
class UwsgiPy(Uwsgi):

    alias = 'uwsgi'
    address = '127.0.0.1:8002'
    description = 'uwsgi HTTP router -> app response'

    @property
    def socket(self):
        return sockets.http(self.address, http11=True)


@register_stand
class UwsgiSslPy(Uwsgi):

    alias = 'uwsgi_ssl'
    address = '127.0.0.1:4443'
    protocol = 'https'
    description = 'uwsgi HTTP router -> app response. Using SSL'

    @property
    def socket(self):
        cert, key = get_ssl_tuple()
        return sockets.https(self.address, cert=cert, key=key)


class UwsgiUwsgiSocket(Uwsgi):

    address = '127.0.0.1:8003'

    def __init__(self, unix=False):
        super().__init__()

        self.unix = unix

        if unix:
            alias = 'uwsgi_uwsgi_unix'
            address = create_socket_file(prefix=alias)

            LOG.debug('Uwsgi socket file: %s' % address)

        else:
            alias = 'uwsgi_uwsgi_tcp'
            address = '127.0.0.1:8003'

        self.alias = alias
        self.address = address

    @property
    def socket(self):
        return sockets.uwsgi(self.address)
