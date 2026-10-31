import os

from envbox import SettingsBase

from .utils import run_command, get_logger


class _S(SettingsBase):

    WORKERS = 1
    THREADS = 1
    LOG_WRITE = False
    MAX_CONNECTIONS = 8191
    STOP_ON_BENCHER_ERROR = False
    BENCHER_NAP = 0.25
    FILENAME_STATS_PLOT = 'narrow-stats'
    FILENAME_STATS_DUMP = 'narrow-stats.json'


Settings = _S()


LOG = get_logger(__name__)
DIR_PACKAGE = os.path.dirname(os.path.abspath(__file__))
DIR_CURRENT = os.getcwd()


def get_ssl_tuple():
    cert = get_path_in_package('files/test.cer')
    key = get_path_in_package('files/testnopass.pem')
    return cert, key


def get_path_in_package(filename):
    return os.path.join(DIR_PACKAGE, filename)


def get_path_in_current(filename):
    return os.path.join(DIR_CURRENT, filename)


def bootstrap():
    """
    SSL stuff (for `files` dir):

        # private key
        openssl genrsa -aes128 -passout pass:foobar -out test.pem 2048
        # public key
        openssl rsa -in test.pem -passin pass:foobar -pubout -out test.pub
        # self-signed x509 certificate
        openssl req -x509 -new -key test.pem -passin pass:foobar -days 3650 -out test.cer
        # remove password
        openssl rsa -in test.pem -out testnopass.pem
        # show info
        openssl x509 -in test.cer -text -noout

    """
    env_info = get_environment_info()

    Settings.MAX_CONNECTIONS = env_info['max_conn']

    return env_info


def get_environment_info():
    """

    From http://brianmcdonnell.github.io/pycon_ie_2013

        ulimit -Hn 131072
        ulimit -Sn 65536

        sysctl -w net.core.somaxconn=8191
        sysctl -w net.ipv4.tcp_max_syn_backlog=8191

    :rtype: dict
    """
    env_info = dict(

        max_conn = int(run_command('cat /proc/sys/net/core/somaxconn')),
        tcp_backlog = int(run_command('cat /proc/sys/net/ipv4/tcp_max_syn_backlog')),

        ulimit_soft = run_command('ulimit -S'),
        ulimit_hard = run_command('ulimit -H'),

    )

    LOG.info(
        'Current net.ipv4.tcp_max_syn_backlog = %(tcp_backlog)s\n'
        'Current net.core.somaxconn = %(max_conn)s\n'
        'Current ulimit soft = %(ulimit_soft)s\n'
        'Current ulimit hard = %(ulimit_hard)s' % env_info
    )

    return env_info
