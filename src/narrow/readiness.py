"""Bounded local socket and HTTP readiness, separate from measured traffic."""

import http.client
import ipaddress
import socket
import ssl
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


def wait_ready(stand: "Stand", *, timeout: float, attempts: int = 8) -> None:
    """Require live owned processes and the expected response, with bounded HTTP probes."""
    deadline = monotonic() + timeout
    last_error = "No readiness response"

    for _attempt in range(attempts):
        stand.check_running()
        remaining = deadline - monotonic()

        if remaining <= 0:
            break

        connection = None

        try:

            if stand.protocol == "uwsgi":
                family = socket.AF_UNIX if stand.unix else socket.AF_INET
                target = stand.address if stand.unix else split_address(stand.address)

                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    probe.settimeout(min(remaining, 0.25))
                    probe.connect(target)

            else:
                host, port = split_address(stand.address)
                options = {"timeout": min(remaining, 0.25)}

                if stand.protocol == "https":
                    options["context"] = ssl.create_default_context(cafile=get_ssl_ca_path())
                    connection = http.client.HTTPSConnection(host, port, **options)

                else:
                    connection = http.client.HTTPConnection(host, port, **options)

                connection.request("GET", "/", headers={"Connection": "close"})
                response = connection.getresponse()
                body = response.read(1024)
                expected = stand.app.response_body if stand.uses_app else b"Hello there!"

                if response.status != 200 or body != expected:
                    raise NarrowException("Unexpected readiness status or response body")

            stand.check_running()
            return

        except (OSError, http.client.HTTPException, NarrowException) as error:
            last_error = f"{error}"

        finally:

            if connection is not None:
                connection.close()

        sleep(min(0.1, max(0, deadline - monotonic())))

    raise NarrowException(f"Stand {stand.alias} did not become ready: {last_error}")
