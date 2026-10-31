import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from narrow.configuration import LoadSpec
from narrow.measurement.conditions import (
    MeasurementConditions, build_http_workload, compare_conditions, read_conditions, require_known_conditions,
)
from narrow.requests import RequestSpec


def test_conditions_snapshot_inputs_and_return_independent_canonical_values(measurement_sections: dict) -> None:
    measurement_sections["policy"]["timeout"] = 1.0
    conditions = MeasurementConditions(**measurement_sections)
    original = conditions.serialize()
    measurement_sections["policy"]["timeout"] = 99
    changed = conditions.serialize()
    changed["workload"]["artifacts"].clear()

    assert conditions.serialize() == original
    assert original["policy"]["timeout"] == 1 and type(original["policy"]["timeout"]) is int
    assert require_known_conditions(original) == original
    assert read_conditions(json.loads(json.dumps(original))) == original


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**4096, b"secret", "\ud800", "x" * 8193, {"bad\nkey": 1}])
def test_conditions_reject_unbounded_or_non_json_facts(measurement_sections: dict, value: object) -> None:
    measurement_sections["policy"]["bad"] = value

    with pytest.raises(ValueError, match="Measurement condition"):
        MeasurementConditions(**measurement_sections)


@pytest.mark.parametrize("corruption", ["missing", "hash", "modified", "extra"])
def test_saved_conditions_reject_incomplete_or_corrupted_evidence(measurement_sections: dict, corruption: str) -> None:
    saved = MeasurementConditions(**measurement_sections).serialize()

    if corruption == "missing":
        del saved["environment"]

    elif corruption == "hash":
        saved["fingerprint"] = "0" * 64

    elif corruption == "modified":
        saved["policy"]["repeats"] = 2

    else:
        saved["schema"] = 1

    with pytest.raises(ValueError, match="conditions|fingerprint"):
        read_conditions(saved)


@pytest.mark.parametrize(("section", "field", "value"), [
    ("workload", "artifacts", {}), ("workload", "http", {}), ("workload", "loads", []),
    ("environment", "machine", {"unavailable": {}}), ("environment", "generator_limits", {}),
    ("policy", "target_settings", {}), ("policy", "target_settings", "unknown"), ("policy", "repeats", True),
    ("tools", "controller", {"narrow": {"payload_sha256": {}}, "python": {}}),
])
def test_equal_unknown_facts_do_not_prove_comparability(
    measurement_sections: dict, section: str, field: str, value: object,
) -> None:
    measurement_sections[section][field] = value
    saved = MeasurementConditions(**measurement_sections).serialize()

    assert compare_conditions(saved, saved) == ()

    with pytest.raises(ValueError, match="Unknown"):
        require_known_conditions(saved)


@pytest.mark.parametrize(("field", "value"), [
    ("unavailable", {"microcode": "unreadable"}), ("processors", {"1": {"microcode": None}}),
    ("affinity", []), ("clock_ticks", 0), ("cpu_roles", {}), ("architecture", ""),
])
def test_partial_machine_facts_remain_unknown(measurement_sections: dict, field: str, value: object) -> None:
    measurement_sections["environment"]["machine"][field] = value

    with pytest.raises(ValueError, match="Unknown"):
        require_known_conditions(MeasurementConditions(**measurement_sections).serialize())


def test_http_workload_ignores_origins_and_input_paths_but_not_actual_request_bytes(tmp_path: Path) -> None:
    request = RequestSpec(method="POST", headers=(("Authorization", "PRIVATE"),), body=tmp_path / "first")
    inputs = {"body": {"sha256": "a" * 64, "bytes": 7, "name": "private-file"}, "ca_file": None}
    loads = (LoadSpec(requests=3), LoadSpec(requests=3, clients=2))
    workload = build_http_workload("http://one.test/api?token=PRIVATE", request, inputs, loads, {"scenario": "b" * 64})
    moved = build_http_workload(
        "http://other.test:9000/api?token=PRIVATE", replace(request, body=tmp_path / "second"), inputs, loads,
        {"scenario": "b" * 64},
    )

    assert workload == moved
    assert all(secret not in json.dumps(workload) for secret in ("PRIVATE", "private-file", "one.test", f"{tmp_path}"))

    for changed_url, changed_request, changed_inputs, changed_loads in (
        ("https://one.test/api?token=PRIVATE", request, inputs, loads),
        ("http://one.test/api?token=CHANGED", request, inputs, loads),
        ("http://one.test/api?token=PRIVATE", replace(request, method="PUT"), inputs, loads),
        ("http://one.test/api?token=PRIVATE", replace(request, headers=(("Authorization", "CHANGED"),)), inputs, loads),
        ("http://one.test/api?token=PRIVATE", request, {**inputs, "body": {"sha256": "c" * 64, "bytes": 7}}, loads),
        ("http://one.test/api?token=PRIVATE", request, inputs, (LoadSpec(requests=4),)),
    ):
        assert workload != build_http_workload(changed_url, changed_request, changed_inputs, changed_loads, {"scenario": "b" * 64})


def test_conditions_differences_explain_fields_without_secret_values(measurement_sections: dict) -> None:
    before = MeasurementConditions(**measurement_sections).serialize()
    after_sections = deepcopy(measurement_sections)
    after_sections["workload"]["artifacts"]["scenario"] = "e" * 64
    after_sections["tools"]["generator"]["sha256"] = "f" * 64
    after_sections["environment"]["machine"]["governors"]["1"] = "powersave"
    after_sections["policy"]["target_settings"]["workers"] = 2
    after = MeasurementConditions(**after_sections).serialize()

    assert compare_conditions(before, after) == (
        "workload.artifacts.scenario", "tools.generator.sha256", "environment.machine.governors.1",
        "policy.target_settings.workers",
    )


@pytest.mark.parametrize("metadata", [{"sha256": None, "bytes": 1}, {"sha256": "a" * 64, "bytes": True}])
def test_http_inputs_require_verified_hash_and_size(metadata: dict) -> None:

    with pytest.raises(ValueError, match="verified content digest"):
        build_http_workload("http://test.example/", RequestSpec(), {"body": metadata, "ca_file": None}, (), {})
