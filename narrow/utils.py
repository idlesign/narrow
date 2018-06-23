import logging
import os
import subprocess
from tempfile import NamedTemporaryFile

from .exceptions import NarrowException


if False:
    from subprocess import Popen


def get_logger(name):
    return logging.getLogger(name)


LOG = get_logger(__name__)


def configure_logging(level=logging.INFO):
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')


def run_command(cmd, check=True):
    LOG.debug('Run command: %s', cmd)

    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    out = result.stdout.decode('utf-8').strip()

    if check and result.returncode:
        LOG.error(out)
        raise NarrowException('Command `%s` failed: %s' % (cmd, out))

    return out


def run_command_detached(cmd, check=True):
    """
    :param cmd:
    :param args:
    :rtype: Popen
    """
    LOG.debug('Run command detached: %s', cmd)

    prc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    # Check for early exit in case of a misconfiguration.
    if check:
        check_process(prc)

    return prc


def check_process(prc):
    """
    :param Popen prc:
    """
    try:
        prc.wait(1)

    except subprocess.TimeoutExpired:
        pass

    if prc.returncode:
        raise NarrowException('%s' % prc.stdout.read().decode('utf-8'))


def get_named_tmp(*, prefix, suffix):
    with NamedTemporaryFile(prefix='narrow_' + prefix + '_', suffix=suffix, delete=False) as f:
        filepath = f.name
    return filepath


def create_socket_file(*, prefix):
    filepath = get_named_tmp(prefix=prefix, suffix='.sock')
    os.chmod(filepath, 0x666)
    return filepath


def save_to_tmp(*, prefix, contents):
    filepath = get_named_tmp(prefix=prefix, suffix='.ini')

    with open(filepath, 'w') as target_file:
        target_file.write(contents)
        target_file.flush()

    return filepath
