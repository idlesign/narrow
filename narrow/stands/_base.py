from collections import OrderedDict
from contextlib import contextmanager

from .._base import Component
from ..utils import get_logger, run_command

LOG = get_logger(__name__)

STANDS = OrderedDict()


def register_stand(cls):
    """
    :param Type[Stand] cls:
    """
    STANDS[cls.alias] = cls()
    return cls


@contextmanager
def nullcontext():
    yield


class Stand(Component):

    address = '0.0.0.0:8000'
    protocol = 'http'

    def __init__(self):
        self.prc = None
        self.child = None

    def get_version(self):
        return '%s x.x' % self.alias

    @contextmanager
    def setup(self):
        contextman = nullcontext

        child = self.child

        if child:
            contextman = child.setup

        LOG.info('Working with `%s` stand ...', self.alias)

        with contextman():

            prc = self.bootstrap()

            if prc:
                LOG.debug('Stand `%s` spawned process %s' % (self.alias, prc.pid))
                self.prc = prc

            try:
                yield

            finally:
                self.finalize()

    def bootstrap(self):
        pass

    def finalize(self):
        prc = self.prc

        if not prc:
            return

        # need to wipe all processes.
        try:
            run_command('killall %s' % self.prc_name)

        except Exception as e:
            LOG.warning('%s' % e)

        LOG.debug('Stand `%s` finalized process %s' % (self.alias, prc.pid))
