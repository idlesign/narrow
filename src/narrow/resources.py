"""Private, explicit temporary storage for one stand."""

from pathlib import Path
from tempfile import TemporaryDirectory


class RunResources:
    """Create nothing until prepare(), remove only this stand's own directory."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._directory: TemporaryDirectory | None = None

    def prepare(self) -> Path:
        """Allocate a private directory for sockets, configs and PID files."""

        if self._directory is not None:
            raise RuntimeError("Resources already prepared")

        self._directory = TemporaryDirectory(prefix="narrow-")
        self.path = Path(self._directory.name)
        return self.path

    def close(self) -> None:
        """Remove owned temporary files, including a partially configured stand."""

        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None
            self.path = None
