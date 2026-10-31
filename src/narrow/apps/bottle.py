from importlib.metadata import version

from ._base import App, register_app


@register_app
class Bottle(App):
    """
    https://github.com/bottlepy/bottle

    """
    alias = 'bottle'
    entrypoint = 'bottle'
    description = 'Bottle framework application'

    def get_version(self) -> str:
        return f"Bottle {version('bottle')}"
