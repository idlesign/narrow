import math
import resource
from dataclasses import dataclass
from pathlib import Path

from envbox import SettingsBase


class SettingsDefinition(SettingsBase):
    WORKERS = 1
    THREADS = 1
    LOG_WRITE = False
    MAX_CONNECTIONS = 8191
    LISTEN_BACKLOG = 128
    FILENAME_STATS_PLOT = "narrow-stats.html"
    FILENAME_STATS_DUMP = "narrow-stats.json"


Settings = SettingsDefinition()
DIR_PACKAGE = Path(__file__).resolve().parent


def get_ssl_tuple() -> tuple[str, str]:
    """Return the deliberately public test certificate/key, never production secrets."""
    return get_path_in_package("files/test.cer"), get_path_in_package("files/testnopass.pem")


def get_ssl_ca_path() -> str:
    """Return the public test CA, separate from the server certificate."""
    return get_path_in_package("files/test-ca.cer")


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


@dataclass(frozen=True)
class LoadSpec:
    """One HTTP/1.1 workload: exactly one request count or duration, never both."""

    clients: int = 1
    requests: int | None = None
    duration: float | None = None
    qps: float | None = None
    worker_threads: int = 1
    timeout: float = 5.0
    connect_timeout: float = 2.0
    keepalive: bool = True
    drain: bool = True
    latency_correction: bool | None = None

    def __post_init__(self) -> None:

        for name in ("clients", "worker_threads"):
            value = getattr(self, name)

            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        if (self.requests is None) == (self.duration is None):
            raise ValueError("Specify exactly one of requests or duration")

        if self.requests is not None and (type(self.requests) is not int or self.requests <= 0):
            raise ValueError("requests must be a positive integer")

        for name in ("duration", "qps", "timeout", "connect_timeout"):
            value = getattr(self, name)

            if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive")

        if type(self.keepalive) is not bool or type(self.drain) is not bool:
            raise ValueError("keepalive and drain must be boolean")

        if self.latency_correction is None:
            object.__setattr__(self, "latency_correction", self.qps is not None)

        if type(self.latency_correction) is not bool or self.latency_correction and self.qps is None:
            raise ValueError("latency_correction must be boolean and requires QPS when enabled")
