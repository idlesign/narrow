# narrow installation

Requires CPython 3.12+ and [uv](https://docs.astral.sh/uv/); benchmark runs require Linux.

```shell
uv tool install narrow
```

This installs the published PyPI release, not unreleased local changes.
For uWSGI stands, install the optional server extra with `uv tool install "narrow[uwsgi]"`;
nginx and oha 1.16.0 must be prepared separately. No servers are required just for
help, component lists or saved reports. See
[the documentation](https://github.com/idlesign/narrow/blob/master/docs/index.md).
