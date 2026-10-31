from collections import OrderedDict

from .._base import Component
from ..configuration import get_path_in_package
from ..utils import get_logger

LOG = get_logger(__name__)

APPS = OrderedDict()


def register_app(cls):
    """
    :param Type[App] cls:
    """
    APPS[cls.alias] = cls
    return cls


class App(Component):
    """A WSGI application available to server stands."""

    def get_version(self):
        return ''

    def get_entrypoint_path(self) -> str:
        """Return the installed application's WSGI entry point file."""
        return get_path_in_package(f"apps/entrypoints/{self.alias}_app.py")
