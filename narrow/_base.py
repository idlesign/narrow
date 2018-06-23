from .utils import run_command


class Component:

    alias = None
    prc_name = None
    description = '<none>'

    def get_version(self):
        return '%s x.x' % self.alias

    def get_alias_full(self):
        return self.alias


class PythonComponent(Component):

    alias = 'python'
    prc_name = 'python'

    def get_version(self):
        return run_command('%s --version' % self.prc_name)


class OSComponent(Component):

    def get_version(self):
        return run_command('uname -srp')
