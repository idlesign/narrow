from collections import OrderedDict

from .._base import Component
from ..utils import get_logger
from ..configuration import get_path_in_package

LOG = get_logger(__name__)

APPS = OrderedDict()


def register_app(cls):
    """
    :param Type[App] cls:
    """
    APPS[cls.alias] = cls
    return cls


class App(Component):

    entrypoint = None

    def get_version(self):
        return ''

    def get_entrypoint_path(self):
        return get_path_in_package('apps/entrypoints/%s.py' % self.entrypoint)


@register_app
class PurePy(App):

    alias = 'py'
    entrypoint = 'pure'

