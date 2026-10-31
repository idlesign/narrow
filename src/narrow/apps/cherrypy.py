from importlib.metadata import version

from ._base import App, register_app


@register_app
class Cherrypy(App):
    """
    https://github.com/cherrypy/cherrypy

    """
    response_body = b"Hello world!"
    alias = "cherrypy"
    entrypoint = "cherrypy"
    description = "CherryPy framework application"

    def get_version(self) -> str:
        package_version = version("cherrypy")
        return f"CherryPy {package_version}"
