import resource
from pathlib import Path

from envbox import SettingsBase


class SettingsDefinition(SettingsBase):
    WORKERS = 1
    THREADS = 1
    LOG_WRITE = False
    MAX_CONNECTIONS = 8191
    STOP_ON_BENCHER_ERROR = False
    BENCHER_NAP = 0.25
    FILENAME_STATS_PLOT = "narrow-stats.html"
    FILENAME_STATS_DUMP = "narrow-stats.json"


Settings = SettingsDefinition()
DIR_PACKAGE = Path(__file__).resolve().parent


def get_ssl_tuple() -> tuple[str, str]:
    """Return the deliberately public test certificate/key, never production secrets."""
    return get_path_in_package("files/test.cer"), get_path_in_package("files/testnopass.pem")


def get_path_in_package(filename: str) -> str:
    """Resolve an installed package resource, independent of the working directory."""
    return f"{DIR_PACKAGE / filename}"


def get_path_in_current(filename: str) -> str:
    """Resolve a path using the current directory at the time of the call."""
    return f"{Path.cwd() / filename}"


def get_environment_info() -> dict:
    """Observe Linux limits without changing application settings or host configuration."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    observations = {"ulimit_soft": soft, "ulimit_hard": hard}

    for name, filename in (
        ("max_conn", "/proc/sys/net/core/somaxconn"),
        ("tcp_backlog", "/proc/sys/net/ipv4/tcp_max_syn_backlog"),
    ):

        try:
            observations[name] = int(Path(filename).read_text().strip())

        except FileNotFoundError:
            observations[name] = None

    return observations


def bootstrap() -> dict:
    """Return environment observations; never overwrite requested limits."""
    return get_environment_info()
