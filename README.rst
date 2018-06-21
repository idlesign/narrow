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

    $ narrow runlocal --plot
