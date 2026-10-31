"""External endpoints: no ownership, process launch or hidden HTTP probe."""

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..requests import redact_url, validate_url
from .subjects import SubjectSpec

class UrlTarget:
    """An explicit external endpoint; narrow neither starts nor stops its server."""

    ownership = "external"
    app_alias = None
    ca_file = None

    def __init__(self, url: str, *, name: str = "custom", subject: SubjectSpec | None = None) -> None:
        validate_url(url)

        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", name):
            raise ValueError("Target name must contain 1..64 letters, digits, dots, underscores or hyphens")

        if subject is not None and not isinstance(subject, SubjectSpec):
            raise TypeError("subject must be a SubjectSpec")

        self.url = url
        self.alias = name
        self.subject = subject

    def describe(self) -> dict:
        """Describe selection without performing I/O or claiming server provenance."""
        return {
            "name": self.alias, "app": self.app_alias, "ownership": self.ownership,
            "url": redact_url(self.url), "url_sha256": hashlib.sha256(self.url.encode()).hexdigest(),
            "subject": self.subject.serialize() if self.subject is not None else None,
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
