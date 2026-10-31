# narrow

[![PyPI](https://img.shields.io/pypi/v/narrow.svg)](https://pypi.org/project/narrow/)
[![License](https://img.shields.io/pypi/l/narrow.svg)](https://pypi.org/project/narrow/)

*Naive throughput measurements for Python web apps and servers.*

narrow runs WSGI applications on uWSGI and nginx stands, invokes oha (or h2load/weighttp),
and saves raw evidence, JSON checkpoints and interactive Plotly HTML reports.
Workloads, warmup and repeats are explicit and bounded by deadlines.

Requires Python 3.12+. See the
[documentation](https://github.com/idlesign/narrow/blob/master/docs/index.md)
for installation, stands, applications, and limitations. The
[historical report](https://idlesign.github.io/narrow/) is not a new measurement.

## Development

Use [makeapp](https://pypi.org/project/makeapp/) to streamline development:

```shell
uv tool install makeapp
# Or update an existing installation:
uv tool upgrade makeapp
```

From the project directory, using CPython 3.12 with the GIL:

```shell
ma tools
ma up --tool
ma tests
ma docs --build
```
