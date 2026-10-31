import logging
import sys
import json
from pathlib import Path

import click
from click.core import ParameterSource

from . import APPS, BENCHERS, STANDS, VERSION
from .artifacts import stats_load
from .configuration import LoadSpec, Settings
from .exceptions import NarrowException
from .plotting import plot
from .runner import Runner, RunOptions
from .requests import RequestSpec
from .run_config import read_run_config
from .targets import CommandTarget, UrlTarget
from .shortcuts import gather_stats, stand_run


@click.group()
@click.version_option(version=VERSION)
@click.option("--verbose", is_flag=True, help="Include diagnostic information.")
def entry_point(verbose: bool) -> None:
    """Explicit HTTP/HTTPS measurements. Prepare server tools separately."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s: %(message)s")


@entry_point.command()
@click.option("--cfg", "config", type=click.Path(path_type=Path, dir_okay=False), help="Read run settings from a TOML file.")
@click.option("--stand", multiple=True, help="Built-in stand aliases; mutually exclusive with --url.")
@click.option("--url", help="Explicit external endpoint or URL of the foreground command after --.")
@click.option("--name", default="custom", help="Name of the user-supplied URL or command target.")
@click.option("--cwd", type=click.Path(path_type=Path, file_okay=False), help="Working directory of the command after --.")
@click.option("--env", multiple=True, help="Command environment override NAME=VALUE; values are not recorded.")
@click.option("--ready-path", default="/", help="Safe GET readiness path, independent of the measured request.")
@click.option("--ready-status", type=int, default=200, show_default=True)
@click.option("--method", default="GET", show_default=True)
@click.option("--path", help="Replace the measured path/query, keeping the target origin.")
@click.option(
    "--header", multiple=True,
    help="NAME: VALUE. Known credentials are redacted in artifacts; headers remain in OS argv.",
)
@click.option("--body", type=click.Path(path_type=Path, dir_okay=False), help="Body file, snapshotted before measurement.")
@click.option("--ca-file", type=click.Path(path_type=Path, dir_okay=False), help="Explicit HTTPS CA; otherwise use system trust.")
@click.option("--expected-status", type=int, help="Require this exact status; by default require 2xx.")
@click.option(
    "--expected-bytes", type=click.IntRange(min=0),
    help="Require total body bytes to equal this size times completed responses; individual bodies are not checked.",
)
@click.option("--dry-run", is_flag=True, help="Print the plan without reading body/CA contents, launching tools or making requests.")
@click.option("--execute", is_flag=True, help="Explicitly opt in to the bounded workload.")
@click.option("--app", multiple=True, default=("py",), show_default=True)
@click.option("--bencher", default="oha", show_default=True)
@click.option("--clients", type=click.IntRange(min=1), multiple=True, default=(1,), show_default=True)
@click.option("--requests", type=click.IntRange(min=1), help="Count per concurrency point and repeat.")
@click.option("--duration", type=click.FloatRange(min=0, min_open=True), help="Generation window in seconds, instead of count.")
@click.option("--qps", type=click.FloatRange(min=0, min_open=True), help="Total requested QPS, not proof of offered load.")
@click.option("--repeats", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--warmup-requests", type=click.IntRange(min=0), default=0, help="Separate unmeasured count at each concurrency point.")
@click.option("--worker-threads", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--request-timeout", type=float, default=5.0, show_default=True)
@click.option("--connect-timeout", type=float, default=2.0, show_default=True)
@click.option("--attempt-timeout", type=float, default=30.0, show_default=True)
@click.option("--ready-timeout", type=float, default=5.0, show_default=True)
@click.option("--budget", type=float, default=120.0, help="Execution deadline in seconds, including reserved cleanup.")
@click.option("--keepalive/--no-keepalive", default=True, show_default=True)
@click.option("--drain/--no-drain", default=True, show_default=True)
@click.option("--output", type=click.Path(path_type=Path), help="Required with --execute; a new private result directory.")
@click.option("--log", is_flag=True, help="Include server access logs in captured diagnostics.")
@click.option("--plot", "create_plot", is_flag=True, help="Build HTML after the run without further requests.")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run(
    context: click.Context, config: Path | None, stand: tuple[str, ...], app: tuple[str, ...], bencher: str, clients: tuple[int, ...],
    requests: int | None, duration: float | None, qps: float | None, repeats: int,
    warmup_requests: int, worker_threads: int, request_timeout: float, connect_timeout: float,
    attempt_timeout: float, ready_timeout: float, budget: float, keepalive: bool, drain: bool,
    output: Path | None, log: bool, create_plot: bool, url: str | None, name: str, cwd: Path | None,
    env: tuple[str, ...], ready_path: str, ready_status: int, method: str, path: str | None,
    header: tuple[str, ...], body: Path | None, ca_file: Path | None, expected_status: int | None,
    expected_bytes: int | None, dry_run: bool, execute: bool, command: tuple[str, ...],
) -> None:
    """Run only explicitly requested workloads; no benchmark starts with defaults alone."""

    try:

        if dry_run == execute:
            raise ValueError("Select exactly one of --dry-run or --execute")

        if execute and output is None:
            raise ValueError("--execute requires --output")

        if config is not None:
            controls = {"config", "dry_run", "execute", "output", "create_plot", "log", "command"}

            if command or any(
                parameter not in controls and context.get_parameter_source(parameter) not in (None, ParameterSource.DEFAULT)
                for parameter in context.params
            ):
                raise ValueError("--cfg cannot be combined with CLI run settings or a command")

            options = read_run_config(config)

        else:

            if bool(stand) == bool(url):
                raise ValueError("Select --stand or --url, not both")

            if command and not url:
                raise ValueError("A foreground command requires --url")

            if not command and (cwd is not None or env or ready_path != "/" or ready_status != 200):
                raise ValueError("Command cwd/environment/readiness options require an argv after --")

            if url and app != ("py",):
                raise ValueError("--app applies only to built-in stands")

            if not url and name != "custom":
                raise ValueError("--name applies only to --url")

            headers = []

            for value in header:
                key, separator, content = value.partition(":")

                if not separator:
                    raise ValueError("Headers must use NAME: VALUE")

                headers.append((key.strip(), content.lstrip()))

            environment = {}

            for value in env:
                key, separator, content = value.partition("=")

                if not separator or key in environment:
                    raise ValueError("Environment overrides must use distinct NAME=VALUE entries")

                environment[key] = content

            targets = ()

            if command:
                targets = (CommandTarget(
                    url, command, name=name, cwd=cwd, env=environment, ready_path=ready_path, ready_status=ready_status,
                ),)

            elif url:
                targets = (UrlTarget(url, name=name),)

            request = RequestSpec(
                method=method, path=path, headers=tuple(headers), body=body, ca_file=ca_file,
                expected_status=expected_status, expected_response_bytes=expected_bytes,
            )
            loads = tuple(LoadSpec(
                clients=count, requests=requests, duration=duration, qps=qps, worker_threads=worker_threads,
                timeout=request_timeout, connect_timeout=connect_timeout, keepalive=keepalive, drain=drain,
            ) for count in clients)
            options = RunOptions(
                stands=stand, apps=app if stand else (), targets=targets, request=request,
                bencher=bencher, loads=loads, repeats=repeats,
                warmup_requests=warmup_requests, attempt_timeout=attempt_timeout, ready_timeout=ready_timeout, budget=budget,
            )

        if log and not options.stands:
            raise ValueError("--log applies only to built-in stands")

        Settings.LOG_WRITE = log

        if dry_run:
            click.echo(json.dumps(Runner(options).plan(), allow_nan=False))
            return

        stats = gather_stats(options, output)

        if create_plot:
            plot(stats, filename=output / "report.html")

        if stats["status"] != "complete":
            raise NarrowException(f"Run contains failed attempts; see {output / "run.json"}")

    except (ValueError, OSError, NarrowException) as error:
        raise click.ClickException(f"{error}") from error

    click.echo(f"{output.resolve() / "run.json"}")


@entry_point.command()
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option("--output", type=click.Path(path_type=Path), default=Path("narrow-stats.html"))
def stats_plot(source: Path, output: Path) -> None:
    """Build an HTML report from a saved run, without starting servers or generators."""

    try:
        click.echo(plot(stats_load(source), filename=output))

    except (ValueError, OSError, NarrowException) as error:
        raise click.ClickException(f"{error}") from error


@entry_point.command()
@click.argument("stand")
@click.option("--app", default="py", show_default=True)
@click.option("--log", is_flag=True)
def stand_up(stand: str, app: str, log: bool) -> None:
    """Start one stand; Ctrl+C stops only its owned processes."""
    Settings.LOG_WRITE = log

    try:
        stand_run(stand_alias=stand, app_alias=app)

    except (OSError, NarrowException) as error:
        raise click.ClickException(f"{error}") from error


def print_registry_items(registry: dict) -> None:
    """Print available components without constructing or preparing them."""

    for alias, item in sorted(registry.items()):
        click.echo(f"{alias}: {item.description}")


@entry_point.command()
def list_stands() -> None:
    """List local server topologies without starting them."""
    print_registry_items(STANDS)


@entry_point.command()
def list_benchers() -> None:
    """List generators without executing them."""
    print_registry_items(BENCHERS)


@entry_point.command()
def list_apps() -> None:
    """List application adapters without importing their optional frameworks."""
    print_registry_items(APPS)


def main() -> None:

    try:
        entry_point()

    except NarrowException as error:
        click.echo(f"{error}", err=True)
        sys.exit(1)
