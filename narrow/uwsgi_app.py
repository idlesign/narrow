
def application(env, start_response):
    start_response('200 OK', [
        ('Content-Type', 'text/plain'),
        ('Content-Length', '12'),
    ])
    return [b'Hello there!']
