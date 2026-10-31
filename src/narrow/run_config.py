"""Optional TOML input for the existing runner, without another execution model."""

import os
import stat
import tomllib
from pathlib import Path

from .configuration import LoadSpec
from .requests import RequestSpec
from .runner import Runner, RunOptions
from .targets import CommandTarget, UrlTarget

MAX_CONFIG_BYTES = 1024 * 1024
LOAD_FIELDS = {
    "requests": "requests", "duration": "duration", "qps": "qps", "worker_threads": "worker_threads",
    "request_timeout": "timeout", "connect_timeout": "connect_timeout", "keepalive": "keepalive", "drain": "drain",
}
RUN_FIELDS = {"stands", "apps", "bencher", "repeats", "warmup_requests", "attempt_timeout", "ready_timeout", "budget"}
REQUEST_FIELDS = {"method", "path", "headers", "body", "ca_file", "expected_status", "expected_response_bytes", "max_body_bytes"}
COMMAND_FIELDS = {"argv", "cwd", "env", "ready_path", "ready_status"}

def read_table(value: object, *, name: str, fields: set[str]) -> dict:
    """Reject malformed tables and misspelled settings instead of ignoring them."""

    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a table")

    if unknown := value.keys() - fields:
        raise ValueError(f"Unknown fields in {name}: {sorted(unknown)!r}")

    return dict(value)

def read_path(value: object, *, base: Path, name: str) -> Path:
    """Anchor file settings without opening inputs, expanding variables or resolving symlinks."""

    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{name} must be a nonempty path string")

    return base / value

def read_target(value: object, *, base: Path, index: int) -> UrlTarget:
    """Construct a URL or argv target without probing it or inspecting the executable."""
    name = f"targets[{index}]"
    target = read_table(value, name=name, fields={"name", "url"} | COMMAND_FIELDS)

    if "name" not in target or "url" not in target:
        raise ValueError(f"{name} requires name and url")

    if "argv" not in target:

        if target.keys() & COMMAND_FIELDS:
            raise ValueError(f"{name}: command settings require argv")

        return UrlTarget(target["url"], name=target["name"])

    argv = target["argv"]

    if not isinstance(argv, list) or not all(isinstance(argument, str) for argument in argv):
        raise ValueError(f"{name}.argv must be an array of strings")

    environment = target.get("env", {})

    if not isinstance(environment, dict):
        raise ValueError(f"{name}.env must be a table")

    return CommandTarget(
        target["url"], argv, name=target["name"],
        cwd=read_path(target.get("cwd", "."), base=base, name=f"{name}.cwd"), env=environment,
        ready_path=target.get("ready_path", "/"), ready_status=target.get("ready_status", 200),
    )

def read_run_config(path: Path) -> RunOptions:
    """Read a regular UTF-8 TOML file into validated run options, without starting tools.

    Tables are run, request and targets. Body/CA/cwd paths are relative to the file;
    argv paths follow the command cwd. No shell, variable substitution or raw config
    persistence is added. Only the configuration is read, not its referenced inputs.
    """
    source = Path(path).resolve(strict=True)
    descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)

    with os.fdopen(descriptor, "rb") as stream:

        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Run config must be a regular TOML file")

        contents = stream.read(MAX_CONFIG_BYTES + 1)

    if len(contents) > MAX_CONFIG_BYTES:
        raise ValueError("Run config TOML exceeds the 1 MiB size limit")

    try:
        data = tomllib.loads(contents.decode("utf-8"))

    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
        raise ValueError("Invalid run config TOML") from error

    data = read_table(data, name="config", fields={"run", "request", "targets"})
    run = read_table(data.get("run", {}), name="run", fields=RUN_FIELDS | LOAD_FIELDS.keys() | {"clients"})
    request = read_table(data.get("request", {}), name="request", fields=REQUEST_FIELDS)

    for name in ("body", "ca_file"):

        if name in request:
            request[name] = read_path(request[name], base=source.parent, name=f"request.{name}")

    headers = request.pop("headers", [])

    if not isinstance(headers, list) or not all(isinstance(header, list) for header in headers):
        raise ValueError("request.headers must be an array of name/value arrays")

    request_spec = RequestSpec(**request, headers=tuple(tuple(header) for header in headers))
    clients = run.pop("clients", [1])

    if not isinstance(clients, list) or not clients or any(type(count) is not int or count < 1 for count in clients):
        raise ValueError("run.clients must be a nonempty array of positive integers")

    load = {field: run.pop(name) for name, field in LOAD_FIELDS.items() if name in run}
    loads = tuple(LoadSpec(clients=count, **load) for count in clients)

    for name in ("stands", "apps"):

        if name in run:
            values = run[name]

            if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
                raise ValueError(f"run.{name} must be a nonempty array of strings")

            run[name] = tuple(values)

    if "apps" in run and not run.get("stands"):
        raise ValueError("run.apps requires run.stands")

    if "bencher" in run and not isinstance(run["bencher"], str):
        raise ValueError("run.bencher must be a string")

    targets = data.get("targets", [])

    if not isinstance(targets, list):
        raise ValueError("targets must be an array of tables")

    options = RunOptions(
        **run, loads=loads, request=request_spec,
        targets=tuple(read_target(target, base=source.parent, index=index) for index, target in enumerate(targets, 1)),
    )
    Runner(options).plan()
    return options
