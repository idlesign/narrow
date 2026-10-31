"""Selected built-in stand topologies through the same endpoint lifecycle."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from ..apps import APPS
from ..configuration import get_ssl_ca_path
from ..exceptions import NarrowException
from ..stands import STANDS
from .subjects import SubjectSpec

class StandTarget:
    """Use existing stand lifecycle through the same endpoint interface."""

    ownership = "owned"

    def __init__(self, alias: str, app_alias: str | None) -> None:
        self.stand = STANDS[alias]()
        self.platform = self.stand.prc_name
        self.subject = None
        self.alias = alias
        self.app_alias = app_alias if self.stand.uses_app else None
        self.app = APPS[self.app_alias]() if self.app_alias is not None else None
        self.url = f"{self.stand.protocol}://{self.stand.address}/"
        self.ca_file = Path(get_ssl_ca_path()) if self.stand.protocol == "https" else None

    def describe(self) -> dict:
        """Describe the built-in topology without inspecting installed tools."""
        return {
            "name": self.alias, "app": self.app_alias, "ownership": self.ownership, "url": self.url,
            "subject": self.subject.serialize() if self.subject is not None else None,
        }

    def prepare(self) -> dict:
        """Verify only tools and application selected for this topology."""
        self.stand.check_tools()
        servers = {}
        current = self.stand

        while current is not None:
            name = current.prc_name
            binary = Path(current.get_executable()).resolve()
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

        artifacts = {f"server/{name}": info["sha256"] for name, info in servers.items()}
        artifacts.update({f"app/{name}/entrypoint": info["entrypoint_sha256"] for name, info in apps.items()})
        self.subject = SubjectSpec(self.platform, artifact_sha256=artifacts)
        return {**self.describe(), "servers": servers, "apps": apps}

    @contextmanager
    def setup(self, *, timeout: float, ca_file: Path | None = None) -> Iterator["StandTarget"]:
        """Delegate built-in application readiness and owned cleanup to the stand."""

        with self.stand.setup(self.app, timeout=timeout, ca_file=ca_file):
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
