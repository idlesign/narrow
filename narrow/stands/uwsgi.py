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

    def get_version(self):
        return 'uwsgi %s' % run_command('%s --version' % self.prc_name)

    def generate_section(self, socket):

        alias = self.alias

        if Settings.LOG_WRITE:
            log_path = get_path_in_current('log_%s.log' % alias)

        else:
            log_path = '/dev/null'

        section = PythonSection(
            wsgi_module=get_path_in_package('uwsgi_app.py'),
            process_prefix=alias,
            workers=1,
            threads=1,
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

        prc = run_command_detached('%s --ini %s' % (self.prc_name, filepath))

        # Check for early exit in case of a misconfiguration.
        check_process(prc)

        return prc


@register_stand
class UwsgiPy(Uwsgi):

    alias = 'uwsgi_py'
    address = '127.0.0.1:8002'

    @property
    def socket(self):
        return sockets.http(self.address)


@register_stand
class UwsgiSslPy(Uwsgi):

    alias = 'uwsgi_ssl_py'
    address = '127.0.0.1:4443'
    protocol = 'https'

    @property
    def socket(self):
        cert, key = get_ssl_tuple()
        return sockets.https(self.address, cert=cert, key=key)


class UwsgiUwsgiPy(Uwsgi):

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
