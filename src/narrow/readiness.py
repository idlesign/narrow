"""Bounded local socket and HTTP readiness, separate from measured traffic."""

import http.client
import ipaddress
import socket
import ssl
import json
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .configuration import get_ssl_ca_path
from .exceptions import NarrowException

if TYPE_CHECKING:
    from .stands._base import Stand


def split_address(address: str) -> tuple[str, int]:
    """Read an explicitly local listener address."""
    parsed = urlsplit(f"//{address}")
    host, port = parsed.hostname, parsed.port

    if host is None or port is None or not ipaddress.ip_address(host).is_loopback:
        raise NarrowException("Stand listeners must use an explicit loopback IP and port")

    return host, port


def check_available(address: str) -> None:
    """Reject an occupied TCP port before launching a stand."""
    host, port = split_address(address)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    try:

        with socket.socket(family, socket.SOCK_STREAM) as probe:
            # TIME_WAIT from a stopped stand is not an active listener.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))

    except OSError as error:
        raise NarrowException(f"Stand address {address} is unavailable: {error}") from error


def _wait_for_probe(
    probe: Callable[[float], None], *, timeout: float, attempts: int,
    check_running: Callable[[], None] | None, title: str,
) -> int:
    deadline = monotonic() + timeout
    last_error = "No readiness response"

    for attempt in range(1, attempts + 1):

        if check_running is not None:
            check_running()

        remaining = deadline - monotonic()

        if remaining <= 0:
            break

        try:
            probe(min(remaining, 0.25))

            if check_running is not None:
                check_running()

            return attempt

        except (OSError, http.client.HTTPException, NarrowException) as error:
            last_error = f"{error}"

        sleep(min(0.1, max(0, deadline - monotonic())))

    raise NarrowException(f"{title} did not become ready: {last_error}")

def wait_http_ready(
    url: str, *, timeout: float, ca_file: Path | None = None, expected_status: int = 200,
    expected_body: bytes | None = None, attempts: int = 8, check_running: Callable[[], None] | None = None,
) -> int:
    """Probe a numeric loopback endpoint with GET, independently of the measured request."""
    parsed = urlsplit(url)
    host, port = split_address(parsed.netloc)

    def probe(connection_timeout: float) -> None:
        options = {"timeout": connection_timeout}

        if parsed.scheme == "https":
            options["context"] = ssl.create_default_context(cafile=ca_file)
            connection = http.client.HTTPSConnection(host, port, **options)

        else:
            connection = http.client.HTTPConnection(host, port, **options)

        try:
            path = f"{parsed.path or '/'}{f'?{parsed.query}' if parsed.query else ''}"
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read(len(expected_body) + 1) if expected_body is not None else None

            if response.status != expected_status or (expected_body is not None and body != expected_body):
                raise NarrowException("Unexpected readiness status or response body")

        finally:
            connection.close()

    return _wait_for_probe(probe, timeout=timeout, attempts=attempts, check_running=check_running, title="HTTP endpoint")

def wait_ready(stand: "Stand", *, timeout: float, attempts: int = 8) -> None:
    """Check built-in HTTP response identity or the internal uWSGI socket."""

    if stand.protocol != "uwsgi":
        wait_http_ready(
            f"{stand.protocol}://{stand.address}/", timeout=timeout, attempts=attempts,
            ca_file=get_ssl_ca_path() if stand.protocol == "https" else None,
            expected_body=stand.app.response_body if stand.uses_app else b"Hello there!", check_running=stand.check_running,
        )
        return

    def probe(connection_timeout: float) -> None:
        family = socket.AF_UNIX if stand.unix else socket.AF_INET
        address = stand.address if stand.unix else split_address(stand.address)

        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(connection_timeout)
            connection.connect(address)

    _wait_for_probe(probe, timeout=timeout, attempts=attempts, check_running=stand.check_running, title=f"Stand {stand.alias}")

def run_probe() -> None:
    """Private subprocess entry: a parent ProcessHandle enforces the hard HTTP deadline."""
    options = json.loads(sys.argv[1])
    attempts = wait_http_ready(**options)
    print(json.dumps({"attempts": attempts}))
