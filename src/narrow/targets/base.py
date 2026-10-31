"""Public endpoint lifecycle implemented by platform adapters."""

from pathlib import Path
from typing import ContextManager, Protocol

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
