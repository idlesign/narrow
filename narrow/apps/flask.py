from ._base import register_app, App


@register_app
class Flask(App):
    """
    https://github.com/pallets/flask

    """
    alias = 'flask'
    entrypoint = 'flask'
    description = 'Flask framework application'

    def get_version(self):
        from flask import __version__
        return 'Flask %s' % __version__
