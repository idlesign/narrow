from ._base import register_app, App


@register_app
class Cherrypy(App):
    """
    https://github.com/cherrypy/cherrypy

    """
    alias = 'cherrypy'
    entrypoint = 'cherrypy'
    description = 'CherryPy framework application'

    def get_version(self):
        from cherrypy import __version__
        return 'CherryPy %s' % __version__
