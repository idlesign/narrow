from .utils import run_command


class Component:
    """A named benchmark component with version information."""

    alias = None
    prc_name = None
    description = '<none>'

    def get_version(self) -> str:
        return f"{self.alias} x.x"

    def get_alias_full(self):
        return self.alias


class PythonComponent(Component):

    alias = 'python'
    prc_name = 'python'

    def get_version(self) -> str:
        return run_command(f"{self.prc_name} --version")


class OSComponent(Component):

    def get_version(self):
        return run_command('uname -srp')
