from ._base import App, register_app


@register_app
class PurePy(App):

    alias = 'py'
    description = 'Pure wsgi application'
