"""HTTP endpoints: external, foreground commands and existing built-in stands."""

import hashlib
import json
import logging
import os
import re
import shutil
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import ContextManager, Protocol
from urllib.parse import urlsplit

from .apps import APPS
from .configuration import get_ssl_ca_path
from .exceptions import NarrowException
from .processes import ProcessHandle
from .readiness import check_available, split_address
from .requests import RequestSpec, redact_url, validate_url
from .stands import STANDS

LOG = logging.getLogger(__name__)
PUBLIC_ENV_SWITCHES = frozenset((
    "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "PYTHONUNBUFFERED", "PYTHONSAFEPATH", "PYTHONUTF8",
))

class UrlTarget:
    """An explicit external endpoint; narrow neither starts nor stops its server."""

    ownership = "external"
    app_alias = None
    ca_file = None

    def __init__(self, url: str, *, name: str = "custom") -> None:
        validate_url(url)

        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", name):
            raise ValueError("Target name must contain 1..64 letters, digits, dots, underscores or hyphens")

        self.url = url
        self.alias = name

    def describe(self) -> dict:
        """Describe selection without performing I/O or claiming server provenance."""
        return {
            "name": self.alias, "app": self.app_alias, "ownership": self.ownership,
            "url": redact_url(self.url), "url_sha256": hashlib.sha256(self.url.encode()).hexdigest(),
        }

    def prepare(self) -> dict:
        """External server identity remains unverified; do not issue a hidden probe."""
        return self.describe()

    @contextmanager
    def setup(self, *, timeout: float, ca_file: Path | None = None) -> Iterator["UrlTarget"]:
        """Borrow an endpoint without allocating server resources."""
        yield self

    def check_running(self) -> None:
        """The bounded HTTP generator observes this endpoint, not a foreign PID."""

    def get_evidence(self) -> list[dict]:
        """There are no owned server processes to inspect or clean up."""
        return []

class CommandTarget(UrlTarget):
    """Own a foreground argv and verify its loopback HTTP readiness with a hard deadline."""

    ownership = "owned"

    def __init__(
        self, url: str, argv: Sequence[str], *, name: str = "custom", cwd: Path | None = None,
        env: Mapping[str, str] | None = None, ready_path: str = "/", ready_status: int = 200,
    ) -> None:
        super().__init__(url, name=name)
        split_address(urlsplit(url).netloc)
        RequestSpec(path=ready_path, expected_status=ready_status)

        if not argv or isinstance(argv, str) or any(not isinstance(value, str) or "\0" in value for value in argv):
            raise ValueError("Command argv must be a nonempty sequence of strings")

        if not argv[0]:
            raise ValueError("Command executable must not be empty")

        if cwd is not None and not isinstance(cwd, Path):
            raise ValueError("cwd must be a pathlib.Path")

        self.argv = tuple(argv)
        self.cwd = cwd
        self.env = dict(env or {})

        if any(
            not isinstance(key, str) or not key or "=" in key or "\0" in key
            or not isinstance(value, str) or "\0" in value for key, value in self.env.items()
        ):
            raise ValueError("Invalid command environment")

        self.ready_url = RequestSpec(path=ready_path).build_url(url)
        self.ready_status = ready_status
        self.process: ProcessHandle | None = None
        self.output = (b"", b"")
        self.output_truncated = False
        self.cleanup_error = None
        self.probe = {}
        self.probe_output = (b"", b"")
        self._prepared = False
        self._launch_env = None
        self._binary = {}

    def _redact(self, data: bytes) -> bytes:
        # Fixed interpreter switches are public; unknown overrides remain private.
        secrets = {
            value for name, value in self.env.items()
            if value and not (name in PUBLIC_ENV_SWITCHES and value in ("0", "1"))
        }

        for value in sorted(secrets, key=len, reverse=True):
            data = data.replace(value.encode(), b"[redacted]")

        return RequestSpec().redact(RequestSpec().redact(data, self.url), self.ready_url)

    def describe(self) -> dict:
        """Record override names, preserving public switches and redacting other values."""
        return {
            **super().describe(), "argv": [self._redact(value.encode()).decode() for value in self.argv],
            "cwd": f"{self.cwd}" if self.cwd is not None else None, "env_keys": sorted(self.env),
            "ready_url": redact_url(self.ready_url), "ready_status": self.ready_status,
            "readiness_maximum_attempts": 8,
        }

    def prepare(self) -> dict:
        """Resolve and fingerprint the executable without invoking arbitrary --version commands."""
        self.cwd = (self.cwd or Path.cwd()).resolve(strict=True)

        if not self.cwd.is_dir():
            raise ValueError("Command cwd must be a directory")

        self._launch_env = os.environ | self.env
        search = os.pathsep.join(
            f"{self.cwd / item}" if not Path(item).is_absolute() else item
            for item in self._launch_env.get("PATH", os.defpath).split(os.pathsep)
        )
        command = f"{self.cwd / self.argv[0]}" if "/" in self.argv[0] else shutil.which(self.argv[0], path=search)

        if command is None or not Path(command).is_file() or not os.access(command, os.X_OK):
            raise NarrowException("Command executable is missing or not executable")

        binary = Path(command).resolve()

        with binary.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()

        # A venv interpreter must be invoked through its symlink, not the resolved binary.
        self.argv = (command, *self.argv[1:])
        self._binary = {"path": f"{binary}", "sha256": checksum}
        self._prepared = True
        return {**self.describe(), "binary": self._binary}

    def check_running(self) -> None:
        """Observe only the server process group created by this target."""

        if self.process is None:
            raise NarrowException("Command target is not running")

        self.process.check_running()

    @contextmanager
    def setup(self, *, timeout: float, ca_file: Path | None = None) -> Iterator["CommandTarget"]:
        """Start, probe and always stop owned resources, preserving a primary failure."""

        if self.process is not None:
            raise NarrowException("Command target is already running")

        if not self._prepared:
            self.prepare()

        self.output = (b"", b"")
        self.cleanup_error = None
        self.output_truncated = False
        self.probe = {"status": "pending", "attempts": None, "maximum_attempts": 8}
        self.probe_output = (b"", b"")
        check_available(urlsplit(self.url).netloc)
        self.process = ProcessHandle(self.argv, cwd=self.cwd, env=self._launch_env)

        try:
            self.process.start()
            self.check_running()
            options = {
                "url": self.ready_url, "timeout": timeout, "expected_status": self.ready_status,
                "ca_file": f"{ca_file}" if ca_file is not None else None,
            }
            probe = ProcessHandle([
                sys.executable, "-I", "-c", "from narrow.readiness import run_probe; run_probe()", json.dumps(options),
            ], output_limit=16384)

            try:
                probe.start()
                result = probe.wait(timeout=timeout)

                if result.error or result.returncode:
                    raise NarrowException("Command target did not become ready; see saved readiness diagnostics")

                try:
                    attempts = json.loads(result.stdout)["attempts"]

                    if type(attempts) is not int or not 1 <= attempts <= 8:
                        raise ValueError("Invalid readiness probe count")

                except (ValueError, KeyError, TypeError) as error:
                    raise NarrowException("Malformed readiness probe result") from error

                self.probe.update(status="ok", attempts=attempts)

            finally:
                self.probe_output = probe.collect_output()
                self.output_truncated = probe.output_truncated

            self.check_running()
            yield self

        finally:
            primary = sys.exception()

            if primary is not None and self.probe["status"] == "pending":
                self.probe["status"] = "failed"

            try:
                self.process.stop()

            except (OSError, NarrowException) as error:
                self.cleanup_error = f"{error}"

                if primary is None:
                    raise

                primary.add_note(f"Cleanup failed: {error}")
                LOG.error("Command target cleanup failed: %s", error)

            finally:
                self.output = self.process.collect_output()
                self.output_truncated = self.output_truncated or self.process.output_truncated
                self.process = None

    def get_evidence(self) -> list[dict]:
        """Retain server/probe diagnostics with private environment values removed."""
        files = {
            "stdout.bin": self.output[0], "stderr.bin": self.output[1],
            "ready-stdout.bin": self.probe_output[0], "ready-stderr.bin": self.probe_output[1],
        }
        redacted = {name: self._redact(contents) for name, contents in files.items()}
        return [{
            "alias": self.alias, "argv": self.describe()["argv"],
            "cleanup_error": self._redact(self.cleanup_error.encode()).decode() if self.cleanup_error is not None else None,
            "output_truncated": self.output_truncated, "readiness": self.probe,
            "artifacts_redacted": files != redacted, "files": redacted,
        }]

class StandTarget:
    """Use existing stand lifecycle through the same endpoint interface."""

    ownership = "owned"

    def __init__(self, alias: str, app_alias: str | None) -> None:
        self.stand = STANDS[alias]()
        self.alias = alias
        self.app_alias = app_alias if self.stand.uses_app else None
        self.app = APPS[self.app_alias]() if self.app_alias is not None else None
        self.url = f"{self.stand.protocol}://{self.stand.address}/"
        self.ca_file = Path(get_ssl_ca_path()) if self.stand.protocol == "https" else None

    def describe(self) -> dict:
        """Describe the built-in topology without inspecting installed tools."""
        return {"name": self.alias, "app": self.app_alias, "ownership": self.ownership, "url": self.url}

    def prepare(self) -> dict:
        """Verify only tools and application selected for this topology."""
        self.stand.check_tools()
        servers = {}
        current = self.stand

        while current is not None:
            name = current.prc_name
            binary = Path(shutil.which(name)).resolve()
            servers[name] = {
                "path": f"{binary}", "version": current.get_version(), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
            current.prc_name = f"{binary}"
            current = current.child

        apps = {}

        if self.app is not None:

            try:
                version = self.app.get_version()

            except PackageNotFoundError as error:
                raise NarrowException(f"Application {self.app_alias} is not installed") from error

            entrypoint = Path(self.app.get_entrypoint_path())
            apps[self.app_alias] = {
                "version": version, "entrypoint_sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
            }

        return {**self.describe(), "servers": servers, "apps": apps}

    @contextmanager
    def setup(self, *, timeout: float, ca_file: Path | None = None) -> Iterator["StandTarget"]:
        """Delegate built-in application readiness and owned cleanup to the stand."""

        with self.stand.setup(self.app, timeout=timeout):
            yield self

    def check_running(self) -> None:
        """Require the complete built-in process topology to remain alive."""
        self.stand.check_running()

    def get_evidence(self) -> list[dict]:
        """Flatten server diagnostics without introducing another process manager."""
        result = []
        current = self.stand

        while current is not None:
            result.append({
                "alias": current.alias, "argv": current.argv, "cleanup_error": current.cleanup_error,
                "output_truncated": current.output_truncated,
                "files": {**{name: contents.encode() for name, contents in current.configurations.items()},
                          "stdout.bin": current.output[0], "stderr.bin": current.output[1]},
            })
            current = current.child

        return result

class Target(Protocol):
    alias: str
    app_alias: str | None
    url: str
    ownership: str
    ca_file: Path | None

    def describe(self) -> dict: ...
    def prepare(self) -> dict: ...
    def setup(self, *, timeout: float, ca_file: Path | None = None) -> ContextManager: ...
    def check_running(self) -> None: ...
    def get_evidence(self) -> list[dict]: ...
