import json

import pytest

from narrow import SubjectSpec
from narrow.targets.subjects import read_subject

def test_identity_is_order_independent_and_does_not_retain_mutable_inputs() -> None:
    artifacts = {"runtime/native.so": "a" * 64, "runtime/core": "b" * 64}
    subject = SubjectSpec("server", "unchanged-version", artifacts)
    expected = SubjectSpec("server", "unchanged-version", dict(reversed(artifacts.items()))).serialize()
    artifacts["runtime/core"] = "c" * 64

    identity = subject.serialize()
    restored = read_subject(json.loads(json.dumps(identity)))

    assert identity == expected == restored
    assert identity["artifact_sha256"]["runtime/core"] == "b" * 64
    assert len(identity["fingerprint"]) == 64
    assert read_subject(None) is None

@pytest.mark.parametrize("changes", [
    {"platform": "another-server"}, {"revision": "another-revision"},
    {"artifact_sha256": {"runtime": "b" * 64}},
])
def test_platform_revision_and_artifact_bytes_each_distinguish_subjects(changes: dict) -> None:
    fields = {"platform": "server", "revision": "unchanged-version", "artifact_sha256": {"runtime": "a" * 64}}

    original = SubjectSpec(**fields).serialize()
    changed = SubjectSpec(**(fields | changes)).serialize()

    assert original["fingerprint"] != changed["fingerprint"]

@pytest.mark.parametrize(("fields", "message"), [
    ({"platform": ""}, "platform"), ({"platform": True}, "platform"),
    ({"revision": "\nprivate"}, "revision"), ({"revision": "x" * 257}, "revision"),
    ({"revision": "\ud800"}, "revision"), ({"artifact_sha256": []}, "mapping"),
    ({"artifact_sha256": {"../runtime": "a" * 64}}, "artifact names"),
    ({"artifact_sha256": {"/runtime": "a" * 64}}, "artifact names"),
    ({"artifact_sha256": {"runtime": "a" * 63}}, "SHA-256"),
    ({"artifact_sha256": {"runtime": "A" * 64}}, "SHA-256"),
    ({"artifact_sha256": {"runtime": True}}, "SHA-256"),
    ({"artifact_sha256": {f"artifact-{index}": "a" * 64 for index in range(4097)}}, "at most 4096"),
])
def test_reject_invalid_subject_identity(fields: dict, message: str) -> None:

    with pytest.raises(ValueError, match=message):
        SubjectSpec(**({"platform": "server"} | fields))

@pytest.mark.parametrize("changes", [{"revision": "changed"}, {"fingerprint": "a" * 64}, {"unknown": True}])
def test_reject_changed_or_unknown_saved_identity(changes: dict) -> None:
    identity = SubjectSpec("server", "revision").serialize()

    with pytest.raises(ValueError, match="subject|fingerprint"):
        read_subject(identity | changes)
