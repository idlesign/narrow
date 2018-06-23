#!/usr/bin/env python
import logging
import sys

import click

from narrow import *
from narrow.utils import get_logger, configure_logging

LOG = get_logger(__name__)


option_log = click.option('--log', help='Dump logs', is_flag=True)
option_app = click.option('--app', help='Application (framework) to use')


def plot_stats():
    stats = stats_load()
    return plot(stats)


@click.group()
@click.version_option(version=VERSION_STR)
@click.option('--verbose', help='Output additional information', is_flag=True)
def entry_point(verbose):
    """narrow command line utilities."""

    configure_logging(logging.DEBUG if verbose else logging.INFO)


@entry_point.command()
@option_log
@click.option('--stand', help='Stand alias')
@click.option('--bencher', help='Benchmark tool alias')
@click.option('--plot', help='Plot collected stats', is_flag=True)
@option_app
def runlocal(log, stand, bencher, plot, app):
    """Runs stands and benchmarks locally.
    """
    Settings.LOG_WRITE = log

    stats = gather_stats(bencher_alias=bencher, stand_alias=stand, app_alias=app)

    stats_dump(stats)

    if plot:
        plot_stats()

    click.secho('Done', fg='green')


@entry_point.command()
def stats_plot():
    """Generates plot using stats dump."""
    click.secho(plot_stats(), fg='blue')


@entry_point.command()
@click.argument('stand')
@option_log
@option_app
def stand_up(stand, log, app):
    """Runs stand.

    Ctrl+C will brings the stand down.

    """
    Settings.LOG_WRITE = log
    stand_run(stand_alias=stand, app_alias=app)


def main():

    try:
        entry_point(obj={})

    except NarrowException as e:
        click.secho(u'%s' % e, err=True, fg='red')
        sys.exit(1)


if __name__ == '__main__':
    main()
