

class Component:

    alias = None
    prc_name = None

    def get_version(self):
        return '%s x.x' % self.alias
