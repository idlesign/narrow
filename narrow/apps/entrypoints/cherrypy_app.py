import cherrypy


class MyApp(object):

    @cherrypy.expose
    def index(self):
        return "Hello world!"


cherrypy.config.update({'engine.autoreload.on': False})
cherrypy.server.unsubscribe()
cherrypy.engine.start()

application = cherrypy.tree.mount(MyApp())
