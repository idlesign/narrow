"""Explicit HTTP requests and bounded snapshots of their input files."""

import hashlib
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SENSITIVE_HEADERS = frozenset(("authorization", "proxy-authorization", "cookie", "x-api-key", "x-auth-token"))
HTTP_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+\Z")

def validate_url(url: str) -> None:
    """Accept explicit HTTP endpoints, without credentials, fragments or control bytes."""

    if not isinstance(url, str) or "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("URL must not contain whitespace or control characters")

    parsed = urlsplit(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.fragment:
        raise ValueError("An HTTP/HTTPS URL without a fragment is required")

    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not supported")

    if parsed.port is not None and parsed.port <= 0:
        raise ValueError("URL port must be positive")

def redact_url(url: str) -> str:
    """Keep endpoint structure but not query values in persisted diagnostics."""
    parsed = urlsplit(url)
    query = urlencode([(name, "[redacted]") for name, _value in parse_qsl(parsed.query, keep_blank_values=True)])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))

@dataclass(frozen=True)
class RequestSpec:
    """One request; construction validates values without opening files or sockets."""

    method: str = "GET"
    path: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    body: Path | None = None
    ca_file: Path | None = None
    expected_status: int | None = None
    expected_response_bytes: int | None = None
    max_body_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:

        if not isinstance(self.method, str) or not HTTP_TOKEN.fullmatch(self.method):
            raise ValueError("method must be an HTTP token")

        if self.path is not None:

            if not isinstance(self.path, str) or not self.path.startswith("/") or self.path.startswith("//"):
                raise ValueError("path must start with one slash")

            validate_url(f"http://127.0.0.1{self.path}")

        if not isinstance(self.headers, tuple):
            raise ValueError("headers must be a tuple of name/value pairs")

        for header in self.headers:

            if not isinstance(header, tuple) or len(header) != 2:
                raise ValueError("headers must contain name/value pairs")

            name, value = header

            if not isinstance(name, str) or not HTTP_TOKEN.fullmatch(name) or not isinstance(value, str):
                raise ValueError("Invalid HTTP header")

            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("HTTP headers must not contain control characters")

        for name in ("body", "ca_file"):

            if (value := getattr(self, name)) is not None and not isinstance(value, Path):
                raise ValueError(f"{name} must be a pathlib.Path")

        if self.expected_status is not None and (
            type(self.expected_status) is not int or not 100 <= self.expected_status <= 599
        ):
            raise ValueError("expected_status must be an HTTP status code")

        if self.expected_response_bytes is not None and (
            type(self.expected_response_bytes) is not int or self.expected_response_bytes < 0
        ):
            raise ValueError("expected_response_bytes must be a nonnegative integer")

        if self.method == "HEAD" and self.expected_response_bytes not in (None, 0):
            raise ValueError("HEAD responses do not have a body")

        if type(self.max_body_bytes) is not int or self.max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive integer")

    def build_url(self, url: str) -> str:
        """Replace path/query only when explicitly requested; never replace the origin."""
        validate_url(url)
        parsed = urlsplit(url)
        path = urlsplit(self.path) if self.path is not None else parsed
        return urlunsplit((parsed.scheme, parsed.netloc, path.path or "/", path.query, ""))

    def serialize(self) -> dict:
        """Describe the request without body contents or credential header values."""
        return {
            "method": self.method, "path": redact_url(self.path) if self.path is not None else None,
            "headers": [
                [name, "[redacted]" if name.lower() in SENSITIVE_HEADERS else value] for name, value in self.headers
            ],
            "headers_sha256": hashlib.sha256(repr(self.headers).encode()).hexdigest(),
            "body": {"source": self.body.name} if self.body is not None else None,
            "ca_file": {"source": self.ca_file.name} if self.ca_file is not None else None,
            "expected_status": self.expected_status,
            "expected_response_bytes": self.expected_response_bytes, "max_body_bytes": self.max_body_bytes,
        }

    def redact(self, data: bytes, url: str) -> bytes:
        """Remove known credential/query values; redacted raw evidence is marked by its caller."""
        secrets = [value for name, value in self.headers if name.lower() in SENSITIVE_HEADERS]
        query = urlsplit(url).query
        secrets.extend(value for _name, value in parse_qsl(query) if value)
        secrets.extend(part.partition("=")[2] for part in query.split("&") if "=" in part)
        result = data

        for value in sorted(set(secrets), key=len, reverse=True):

            if value:
                result = result.replace(value.encode(), b"[redacted]")

        return result

def snapshot_request(request: RequestSpec, directory: Path) -> tuple[RequestSpec, dict]:
    """Snapshot regular files before measurement, never blocking on a FIFO open."""
    metadata = request.serialize()
    paths = {}

    for name, limit in (("body", request.max_body_bytes), ("ca_file", 8 * 1024 * 1024)):
        source = getattr(request, name)

        if source is None:
            continue

        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)

        with os.fdopen(descriptor, "rb") as stream:

            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"{name} must be a regular file")

            data = stream.read(limit + 1)

        if len(data) > limit:
            raise ValueError(f"{name} exceeds its input size limit")

        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / ("body.bin" if name == "body" else "ca.pem")

        with path.open("xb") as output:
            output.write(data)

        paths[name] = path.resolve()
        metadata[name] = {"name": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    return replace(request, **paths), metadata
