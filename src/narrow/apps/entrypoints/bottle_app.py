import bottle


@bottle.route('/')
def index():
    return 'Hello there!'


application = bottle.default_app()
