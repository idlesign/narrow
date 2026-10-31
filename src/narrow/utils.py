import logging
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from .exceptions import NarrowException


def get_logger(name):
    return logging.getLogger(name)


LOG = get_logger(__name__)


def configure_logging(level=logging.INFO):
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')


def run_command(cmd: str, check: bool = True) -> str:  # noqa: FBT001, FBT002 - Public positional argument.
    LOG.debug('Run command: %s', cmd)

    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    out = result.stdout.decode('utf-8').strip()

    if check and result.returncode:
        LOG.error(out)
        raise NarrowException(f"Command `{cmd}` failed: {out}")

    return out


def run_command_detached(cmd: str, check: bool = True) -> subprocess.Popen:  # noqa: FBT001, FBT002
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


def check_process(prc: subprocess.Popen) -> None:
    """
    :param Popen prc:
    """
    try:
        prc.wait(1)

    except subprocess.TimeoutExpired:
        pass

    if prc.returncode:
        raise NarrowException(prc.stdout.read().decode("utf-8"))


def get_named_tmp(*, prefix: str, suffix: str) -> str:

    with NamedTemporaryFile(prefix="narrow_" + prefix + "_", suffix=suffix, delete=False) as temporary_file:
        filepath = temporary_file.name
    return filepath


def create_socket_file(*, prefix: str) -> str:
    filepath = get_named_tmp(prefix=prefix, suffix='.sock')
    Path(filepath).chmod(0x666)
    return filepath


def save_to_tmp(*, prefix: str, contents: str) -> str:
    filepath = get_named_tmp(prefix=prefix, suffix='.ini')

    with Path(filepath).open("w") as target_file:
        target_file.write(contents)
        target_file.flush()

    return filepath
