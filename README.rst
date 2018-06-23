narrow
======
https://github.com/idlesign/narrow

|release| |lic|

.. |release| image:: https://img.shields.io/pypi/v/narrow.svg
    :target: https://pypi.python.org/pypi/narrow

.. |lic| image:: https://img.shields.io/pypi/l/narrow.svg
    :target: https://pypi.python.org/pypi/narrow


**Work in progress. Stay tuned.**


Description
-----------

*Naive throughput measurements for Python web apps and servers*

This tries to measure a throughput for various Python web apps and servers

Benchmark report sample: https://idlesign.github.io/narrow/

Stands:

* nginx_ssl_static: Nginx static response using SSL
* nginx_ssl_tcp_uwsgi: Nginx -> UWSGI -> TCP socket -> uwsgi -> app response. Using SSL
* nginx_ssl_unix_uwsgi: Nginx -> UWSGI -> Unix socket -> uwsgi -> app response. Using SSL
* nginx_static: Nginx static response
* nginx_tcp_uwsgi: Nginx -> UWSGI -> TCP socket -> uwsgi -> app response
* nginx_unix_uwsgi: Nginx -> UWSGI -> Unix socket -> uwsgi -> app response
* uwsgi: uwsgi HTTP router -> app response
* uwsgi_ssl: uwsgi HTTP router -> app response. Using SSL

Apps/frameworks:

* bottle: Bottle framework application
* cherrypy: CherryPy framework application
* django: Django framework application
* flask: Flask framework application
* py: Pure wsgi application -- default

Benchers:

* h2load: h2load from nghttp2
* weighttp: weighttp (no SSL support)



Requirements
------------

Basics:

* Python 3.4+
* `nginx <https://github.com/nginx/nginx>`_
* `uwsgi <https://github.com/unbit/uwsgi>`_

Benchmarking tools (any):

* `h2load <https://github.com/nghttp2/nghttp2/>`_
* `weighttp <https://github.com/lighttpd/weighttp>`_


Running
-------

Run benchmarks, dump and plot the results:

.. code-block:: bash

    $ narrow --help

    $ narrow list_stands
    $ narrow list_apps
    $ narrow list_benchers

    $ narrow runlocal --plot
    $ narrow --verbose runlocal --log --stand uwsgi --app flask --bencher weighttp
    $ narrow stats_plot
