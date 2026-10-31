from importlib.metadata import version

from ._base import App, register_app


@register_app
class Flask(App):
    """
    https://github.com/pallets/flask

    """
    alias = 'flask'
    entrypoint = 'flask'
    description = 'Flask framework application'

    def get_version(self) -> str:
        return f"Flask {version('flask')}"
