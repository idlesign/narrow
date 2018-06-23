
def application(env, start_response):
    response = 'Hello there!'
    start_response('200 OK', [
        ('Content-Type', 'text/plain'),
        ('Content-Length', '%s' % len(response)),
    ])
    return [response.encode('utf-8')]
