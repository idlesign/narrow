from ._base import register_app, App


@register_app
class Django(App):
    """
    https://github.com/django/django

    """
    alias = 'django'
    description = 'Django framework application'

    def get_version(self):
        from django import __version__
        return 'Django %s' % __version__
