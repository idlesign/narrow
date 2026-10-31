from ._base import register_app, App


@register_app
class Bottle(App):
    """
    https://github.com/bottlepy/bottle

    """
    alias = 'bottle'
    entrypoint = 'bottle'
    description = 'Bottle framework application'

    def get_version(self):
        from bottle import __version__
        return 'Bottle %s' % __version__
