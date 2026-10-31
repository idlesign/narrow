from collections import OrderedDict

from .._base import Component
from ..configuration import get_path_in_package

APPS = OrderedDict()


def register_app(cls: type) -> type:
    """Register an adapter without importing its optional framework."""
    APPS[cls.alias] = cls
    return cls


class App(Component):
    """A WSGI application available to server stands."""

    response_body = b"Hello there!"

    def get_version(self) -> str:
        return ""

    def get_entrypoint_path(self) -> str:
        """Return the installed application's WSGI entry point file."""
        return get_path_in_package(f"apps/entrypoints/{self.alias}_app.py")
