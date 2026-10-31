from importlib.metadata import version

from ._base import App, register_app


@register_app
class Cherrypy(App):
    """
    https://github.com/cherrypy/cherrypy

    """
    alias = 'cherrypy'
    entrypoint = 'cherrypy'
    description = 'CherryPy framework application'

    def get_version(self) -> str:
        return f"CherryPy {version('cherrypy')}"
