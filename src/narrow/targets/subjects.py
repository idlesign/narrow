"""Portable subject identity, independent of Git, Python packaging or a platform SDK."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

@dataclass(frozen=True)
class SubjectSpec:
    """Identify measured code using caller-supplied revision and artifact hashes.

    A revision is a label, not proof of a clean checkout. The caller or platform
    adapter must verify that the running server uses the declared artifacts.
    Narrow does not infer that association from an executable or package version.
    """

    platform: str
    revision: str | None = None
    artifact_sha256: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:

        for name, maximum in (("platform", 64), ("revision", 256)):
            value = getattr(self, name)

            if name == "revision" and value is None:
                continue

            if (
                not isinstance(value, str) or not value.strip() or len(value) > maximum
                or any(ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF for character in value)
            ):
                raise ValueError(f"Subject {name} must be bounded nonempty text without control characters")

        if not isinstance(self.artifact_sha256, Mapping) or len(self.artifact_sha256) > 4096:
            raise ValueError("Subject artifact_sha256 must be a mapping of at most 4096 artifacts")

        artifacts = dict(self.artifact_sha256)

        for name, checksum in artifacts.items():

            if (
                not isinstance(name, str) or len(name) > 512
                or any(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) is None for part in name.split("/"))
            ):
                raise ValueError("Subject artifact names must be bounded relative names without traversal")

            if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
                raise ValueError("Subject artifacts require complete lowercase SHA-256 digests")

        object.__setattr__(self, "artifact_sha256", MappingProxyType(artifacts))

    def serialize(self) -> dict:
        """Return path-independent identity; changed bytes differ even with the same revision label."""
        data = {
            "platform": self.platform, "revision": self.revision,
            "artifact_sha256": dict(self.artifact_sha256),
        }
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return {**data, "fingerprint": hashlib.sha256(payload).hexdigest()}

def read_subject(value: object) -> dict | None:
    """Validate optional saved identity, including its content fingerprint; perform no I/O."""

    if value is None:
        return None

    if not isinstance(value, dict) or set(value) != {"platform", "revision", "artifact_sha256", "fingerprint"}:
        raise ValueError("Malformed saved subject identity")

    identity = SubjectSpec(value["platform"], value["revision"], value["artifact_sha256"]).serialize()

    if identity != value:
        raise ValueError("Saved subject fingerprint differs from its content")

    return identity
