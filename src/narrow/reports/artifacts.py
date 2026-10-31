"""Atomic checkpoints and a private directory of raw evidence for each invocation."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from ..configuration import Settings
from ..exceptions import NarrowException
from ..requests import RequestSpec
from .results import SampleResult

if TYPE_CHECKING:
    from ..targets import Target

def stats_dump(stats: dict, path: Path | None = None) -> None:
    """Atomically replace one checkpoint; never merge unrelated runs or suppress errors."""
    target = Path(path or Settings.FILENAME_STATS_DUMP)
    temporary = None

    try:

        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(stats, stream, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, target)

    finally:

        if temporary is not None:
            temporary.unlink(missing_ok=True)


def stats_load(path: Path | None = None) -> dict:
    """Read the new checkpoint format, reporting malformed input rather than guessing."""
    source = Path(path or Settings.FILENAME_STATS_DUMP)

    if source.is_dir():
        source /= "run.json"

    data = json.loads(source.read_text(encoding="utf-8"))

    if not isinstance(data, dict) or not isinstance(data.get("attempts"), list) or "meta" not in data:
        raise NarrowException("Not a narrow run checkpoint")

    return data


class RunStore:
    """One new output directory; completed attempts survive interruption and later failure."""

    def __init__(self, path: Path, meta: dict) -> None:
        self.path = Path(path)
        self.data = {"meta": meta, "status": "running", "reason": None, "stands": [], "attempts": []}

    def prepare(self) -> None:
        """Create a new private result directory, refusing existing paths."""
        self.path.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.checkpoint()

    def checkpoint(self) -> None:
        """Publish the current complete index atomically."""
        stats_dump(self.data, self.path / "run.json")

    def save_attempt(self, sample: SampleResult, metadata: dict) -> dict:
        """Write raw bytes and per-attempt metadata before publishing the checkpoint."""
        relative = f"attempt-{len(self.data["attempts"]) + 1:04d}"
        directory = self.path / relative
        directory.mkdir(mode=0o700)
        (directory / "stdout.bin").write_bytes(sample.raw_stdout)
        (directory / "stderr.bin").write_bytes(sample.raw_stderr)
        attempt = {**metadata, **sample.serialize(), "artifacts": relative}
        stats_dump(attempt, directory / "result.json")
        self.data["attempts"].append(attempt)
        self.checkpoint()
        return attempt

    def save_target(self, target: "Target", metadata: dict, *, request: RequestSpec) -> None:
        """Record owned process evidence, or explicit lack of ownership of an external URL."""
        relative = f"stand-{len(self.data["stands"]) + 1:04d}"
        directory = self.path / relative
        directory.mkdir(mode=0o700)
        evidence = []
        url = request.build_url(target.url)

        for original in target.get_evidence():
            entry = dict(original)
            files = entry.pop("files")
            prefix = f"{entry['alias']}-"

            for filename, contents in files.items():

                if Path(filename).name != filename:
                    raise ValueError("Evidence filenames must not contain directories")

                redacted = request.redact(contents, url)
                (directory / f"{prefix}{filename}").write_bytes(redacted)
                entry["artifacts_redacted"] = entry.get("artifacts_redacted", False) or redacted != contents

            entry["argv"] = [request.redact(value.encode(), url).decode() for value in entry["argv"]]

            if entry["cleanup_error"] is not None:
                entry["cleanup_error"] = request.redact(entry["cleanup_error"].encode(), url).decode()

            evidence.append(entry)

        self.data["stands"].append({**metadata, "ownership": target.ownership, "artifacts": relative, "processes": evidence})
        self.checkpoint()
