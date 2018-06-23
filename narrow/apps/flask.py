from ._base import register_app, App


@register_app
class Flask(App):

    alias = 'flask'
    entrypoint = 'flask'
    description = 'Flask framework application'

    def get_version(self):
        from flask import __version__
        return 'flask %s' % __version__
