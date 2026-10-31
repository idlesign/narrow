import platform
import sys

from .processes import run_command


class Component:
    """A named benchmark component with version information."""

    alias = None
    prc_name = None
    description = "<none>"

    def get_version(self) -> str:
        return f"{self.alias} x.x"

    def get_alias_full(self) -> str:
        return self.alias


class PythonComponent(Component):

    alias = "python"
    prc_name = "python"

    def get_version(self) -> str:
        return run_command([sys.executable, "--version"])


class OSComponent(Component):

    def get_version(self) -> str:
        return platform.platform()
