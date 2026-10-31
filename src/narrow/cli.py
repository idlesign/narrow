import logging
import sys
from pathlib import Path

import click

from . import APPS, BENCHERS, STANDS, VERSION
from .artifacts import stats_load
from .configuration import LoadSpec, Settings
from .exceptions import NarrowException
from .plotting import plot
from .runner import RunOptions
from .shortcuts import gather_stats, stand_run


@click.group()
@click.version_option(version=VERSION)
@click.option("--verbose", is_flag=True, help="Include diagnostic information.")
def entry_point(verbose: bool) -> None:
    """Local Python web-server measurements. Runtime tools must be prepared separately."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s: %(message)s")


@entry_point.command()
@click.option("--stand", multiple=True, required=True, help="Explicit stand aliases; repeat to select several.")
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
@click.option("--output", type=click.Path(path_type=Path), required=True, help="New private result directory; never overwritten.")
@click.option("--log", is_flag=True, help="Include server access logs in captured diagnostics.")
@click.option("--plot", "create_plot", is_flag=True, help="Build HTML after the run without further requests.")
def runlocal(
    stand: tuple[str, ...], app: tuple[str, ...], bencher: str, clients: tuple[int, ...],
    requests: int | None, duration: float | None, qps: float | None, repeats: int,
    warmup_requests: int, worker_threads: int, request_timeout: float, connect_timeout: float,
    attempt_timeout: float, ready_timeout: float, budget: float, keepalive: bool, drain: bool,
    output: Path, log: bool, create_plot: bool,
) -> None:
    """Run only explicitly requested workloads; no benchmark starts with defaults alone."""

    try:
        loads = tuple(LoadSpec(
            clients=count, requests=requests, duration=duration, qps=qps, worker_threads=worker_threads,
            timeout=request_timeout, connect_timeout=connect_timeout, keepalive=keepalive, drain=drain,
        ) for count in clients)
        options = RunOptions(
            stands=stand, apps=app, bencher=bencher, loads=loads, repeats=repeats,
            warmup_requests=warmup_requests, attempt_timeout=attempt_timeout, ready_timeout=ready_timeout, budget=budget,
        )
        Settings.LOG_WRITE = log
        stats = gather_stats(options, output)

        if stats["status"] != "complete":
            raise NarrowException(f"Run contains failed attempts; see {output / "run.json"}")

        if create_plot:
            plot(stats, filename=output / "report.html")

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
