import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from narrow.measurement import identity
from narrow.measurement.identity import collect_controller_identity, hash_artifacts, hash_payload

def test_payload_identity_includes_resources_not_location_or_bytecode(tmp_path: Path) -> None:
    original = tmp_path / "original"
    original.mkdir()
    (original / "module.py").write_bytes(b'VERSION = "unchanged"\n')
    (original / "report.html").write_bytes(b"<html>original</html>\n")
    (original / "module.pyc").write_bytes(b"ignored bytecode")
    cache = original / "__pycache__"
    cache.mkdir()
    (cache / "generated.data").write_bytes(b"ignored cache")
    relocated = Path(shutil.copytree(original, tmp_path / "relocated"))

    expected = {
        "example/module.py": hashlib.sha256(b'VERSION = "unchanged"\n').hexdigest(),
        "example/report.html": hashlib.sha256(b"<html>original</html>\n").hexdigest(),
    }
    assert hash_payload(original, prefix="example") == hash_payload(relocated, prefix="example") == expected

    (relocated / "report.html").write_bytes(b"<html>changed</html>\n")

    assert hash_payload(relocated, prefix="example") != expected

def test_named_artifacts_hash_large_and_empty_files_without_retaining_their_paths(tmp_path: Path) -> None:
    content = b"streamed bytes" * 16384
    first = tmp_path / "private-workload-name"
    second = tmp_path / "another-name"
    empty = tmp_path / "empty"
    first.write_bytes(content)
    second.write_bytes(content)
    empty.write_bytes(b"")

    expected = {"case": hashlib.sha256(content).hexdigest(), "empty": hashlib.sha256(b"").hexdigest()}

    assert hash_artifacts({"empty": empty, "case": first}) == hash_artifacts({"case": second, "empty": empty}) == expected
    assert f"{tmp_path}" not in json.dumps(expected)

@pytest.mark.parametrize("name", [
    "", " ", "/absolute", "../parent", "a/../b", "a//b", "a\\b", "a\n", "\ud800", "a" * 513,
])
def test_artifact_names_and_prefixes_are_validated_before_opening_files(tmp_path: Path, name: str) -> None:

    with pytest.raises(ValueError, match="Artifact names"):
        hash_artifacts({name: tmp_path / "missing"})

    with pytest.raises(ValueError, match="Artifact names"):
        hash_payload(tmp_path / "missing", prefix=name)

@pytest.mark.parametrize("target", ["module.py", "directory"])
def test_payload_rejects_file_and_directory_symlinks(tmp_path: Path, target: str) -> None:
    (tmp_path / "module.py").write_bytes(b"code")
    (tmp_path / "directory").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / target)

    with pytest.raises(ValueError, match="symlinks"):
        hash_payload(tmp_path, prefix="example")

    with pytest.raises(ValueError, match="regular files"):
        hash_artifacts({"linked": tmp_path / "link"})

def test_payload_rejects_special_files_without_blocking_on_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="special files"):
        hash_payload(tmp_path, prefix="example")

    with pytest.raises(ValueError, match="regular files"):
        hash_artifacts({"fifo": fifo})

def test_empty_missing_or_linked_payload_is_not_an_identity(tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        hash_payload(linked, prefix="example")

    with pytest.raises(ValueError, match="real directory"):
        hash_payload(tmp_path / "missing", prefix="example")

    linked.unlink()

    with pytest.raises(ValueError, match="named files"):
        hash_payload(tmp_path, prefix="example")

def test_artifact_and_payload_bounds_apply_to_the_whole_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"12345")
    second.write_bytes(b"67890")
    monkeypatch.setattr(identity, "MAX_ARTIFACT_BYTES", 8)

    with pytest.raises(ValueError, match="size bound"):
        hash_artifacts({"first": first, "second": second})

    with pytest.raises(ValueError, match="size bound"):
        hash_payload(tmp_path, prefix="example")

    monkeypatch.setattr(identity, "MAX_ARTIFACTS", 1)

    with pytest.raises(ValueError, match="named files"):
        hash_artifacts({"first": first, "second": second})

    with pytest.raises(ValueError, match="file count bound"):
        hash_payload(tmp_path, prefix="example")

    monkeypatch.setattr(identity, "MAX_PAYLOAD_ENTRIES", 1)

    with pytest.raises(ValueError, match="entry count bound"):
        hash_payload(tmp_path, prefix="example")

def test_artifact_changes_during_reading_are_not_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "workload"
    path.write_bytes(b"original")
    observed = path.stat()
    fields = {
        name: getattr(observed, name)
        for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    }
    changed = SimpleNamespace(**(fields | {"st_ctime_ns": observed.st_ctime_ns + 1}))
    monkeypatch.setattr(identity, "os", SimpleNamespace(
        open=os.open, fdopen=os.fdopen, O_RDONLY=os.O_RDONLY, O_NONBLOCK=os.O_NONBLOCK, O_NOFOLLOW=os.O_NOFOLLOW,
        fstat=Mock(side_effect=[observed, changed]),
    ))

    with pytest.raises(ValueError, match="changed while being hashed"):
        hash_artifacts({"workload": path})

def test_controller_tracks_current_narrow_resources_and_python_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "narrow"
    (package / "measurement").mkdir(parents=True)
    module = package / "measurement/identity.py"
    module.write_bytes(b"# current controller\n")
    resource = package / "report.html"
    resource.write_bytes(b"first")
    binary = tmp_path / "actual-python"
    binary.write_bytes(b"actual interpreter bytes")
    linked = tmp_path / "python"
    linked.symlink_to(binary)
    monkeypatch.setattr(identity, "__file__", f"{module}")
    monkeypatch.setattr(identity, "sys", SimpleNamespace(
        executable=f"{linked}", implementation=SimpleNamespace(name="cpython"),
    ))
    monkeypatch.setattr(identity, "platform", SimpleNamespace(python_version=lambda: "3.12.0"))
    config = {"SOABI": "cpython-312", "Py_GIL_DISABLED": 0}
    monkeypatch.setattr(identity, "sysconfig", SimpleNamespace(get_config_var=lambda name: config[name]))

    original = collect_controller_identity()

    assert original == {
        "narrow": {"payload_sha256": {
            "narrow/measurement/identity.py": hashlib.sha256(b"# current controller\n").hexdigest(),
            "narrow/report.html": hashlib.sha256(b"first").hexdigest(),
        }},
        "python": {
            "version": "3.12.0", "implementation": "cpython", "abi": "cpython-312", "gil_enabled": True,
            "sha256": hashlib.sha256(b"actual interpreter bytes").hexdigest(),
        },
    }
    assert f"{tmp_path}" not in json.dumps(original)

    resource.write_bytes(b"second")
    changed = collect_controller_identity()

    assert changed["narrow"] != original["narrow"] and changed["python"] == original["python"]

    binary.write_bytes(b"changed interpreter")

    assert collect_controller_identity()["python"]["sha256"] != original["python"]["sha256"]
