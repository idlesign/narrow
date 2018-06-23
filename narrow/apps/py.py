from ._base import register_app, App


@register_app
class PurePy(App):

    alias = 'py'
    description = 'Pure wsgi application'
