from django.conf import settings
from django.conf.urls import url
from django.core.wsgi import get_wsgi_application
from django.http import HttpResponse

settings.configure(
    DEBUG=False,
    SECRET_KEY='A-random-secret-key!',
    ROOT_URLCONF=__name__,
    ALLOWED_HOSTS=['localhost', '127.0.0.1'],
    MIDDLEWARE=[
        # Sane minimum for http 1.1 (Content-Length)
        'django.middleware.common.CommonMiddleware',
    ]
)


def index(request):
    return HttpResponse('Hello there!')


urlpatterns = [
    url(r'^$', index),
]


application = get_wsgi_application()
