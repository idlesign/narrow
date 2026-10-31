from importlib.metadata import version

from ._base import App, register_app


@register_app
class Django(App):
    """
    https://github.com/django/django

    """
    alias = 'django'
    description = 'Django framework application'

    def get_version(self) -> str:
        return f"Django {version('django')}"
