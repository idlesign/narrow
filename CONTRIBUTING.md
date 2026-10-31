# Contributing to narrow

Read and follow [IDLE.md](IDLE.md) before working on this project.
See [Development](README.md#development) for setup and verification commands.
Use ordinary CPython 3.12 with the GIL locally and in CI.
Framework test dependencies live in the `frameworks` group, explicitly included by `tests`.
The uWSGI server and its configuration builder belong to the optional `uwsgi` extra.

## Submit issues

Report unexpected behavior and propose features in the
[issue tracker](https://github.com/idlesign/narrow/issues).

## Write code

Fork the repository, make a focused change, and open a pull request.
Keep public behavior intact during tooling migrations. Tests must not start
benchmark stands, load generators, or cleanup commands against the host system.

## Spread the word

Share useful tips and experience wherever you find convenient.
