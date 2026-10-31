"""Path-independent conditions; an identity is evidence, not proof of causality."""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict
from urllib.parse import urlsplit

from ..configuration import LoadSpec
from ..processes.limits import ProcessLimits
from ..requests import RequestSpec, validate_url

SECTIONS = ("workload", "tools", "environment", "policy")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def normalize_json(value: object, *, depth: int = 0) -> object:
    """Snapshot bounded JSON facts, rejecting implicit conversions and nonfinite numbers."""

    if depth > 16:
        raise ValueError("Measurement conditions exceed their nesting limit")

    if value is None or type(value) is bool:
        return value

    if type(value) in (int, float):

        if abs(value) > 2**63 - 1 or type(value) is float and not math.isfinite(value):
            raise ValueError("Measurement conditions require finite bounded numbers")

        return int(value) if type(value) is float and value.is_integer() else value

    if isinstance(value, str):

        if len(value) > 8192 or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("Measurement condition text exceeds its bounds")

        return value

    if isinstance(value, Mapping):

        if len(value) > 8192 or any(
            not isinstance(name, str) or not name or len(name) > 512
            or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in name)
            for name in value
        ):
            raise ValueError("Measurement conditions require bounded text keys")

        return {name: normalize_json(item, depth=depth + 1) for name, item in value.items()}

    if isinstance(value, (list, tuple)) and len(value) <= 8192:
        return [normalize_json(item, depth=depth + 1) for item in value]

    raise ValueError("Measurement conditions require bounded JSON facts")


class MeasurementConditions:
    """Snapshot nonsecret workload, tooling, environment and method facts without I/O.

    Target names, origins, revisions, timestamps and evidence paths do not belong
    here. Runtime identity remains a separate SubjectSpec. Caller-declared facts
    do not prove that a remote server or application satisfies those declarations.
    """

    def __init__(
        self, *, workload: Mapping, tools: Mapping, environment: Mapping, policy: Mapping,
    ) -> None:
        values = {"workload": workload, "tools": tools, "environment": environment, "policy": policy}

        if any(not isinstance(value, Mapping) for value in values.values()):
            raise ValueError("Measurement condition sections must be mappings")

        self._payload = json.dumps(normalize_json(values), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

        if len(self._payload) > 2 * 1024 * 1024:
            raise ValueError("Measurement conditions exceed the 2 MiB limit")

    def serialize(self) -> dict:
        """Return a fresh snapshot with its content fingerprint, not a version label."""
        return {**json.loads(self._payload), "fingerprint": hashlib.sha256(self._payload.encode("ascii")).hexdigest()}


def read_conditions(value: object) -> dict:
    """Validate saved conditions and their fingerprint without inventing missing facts."""

    if not isinstance(value, dict) or set(value) != {*SECTIONS, "fingerprint"}:
        raise ValueError("Missing or malformed measurement conditions")

    result = MeasurementConditions(**{name: value[name] for name in SECTIONS}).serialize()

    if result != value:
        raise ValueError("Measurement condition fingerprint differs from its content")

    return result


def compare_conditions(left: dict, right: dict) -> tuple[str, ...]:
    """Return differing field paths, never saved values or a guessed compatibility."""
    before, after = read_conditions(left), read_conditions(right)
    differences = []

    def compare(original: object, actual: object, path: str) -> None:

        if original == actual:
            return

        if isinstance(original, dict) and isinstance(actual, dict):

            for name in sorted(original.keys() | actual.keys()):

                if name not in original or name not in actual:
                    differences.append(f"{path}.{name}")

                else:
                    compare(original[name], actual[name], f"{path}.{name}")

        else:
            differences.append(path)

    for section in SECTIONS:
        compare(before[section], after[section], section)

    return tuple(differences[:128])


def require_known_conditions(value: dict) -> dict:
    """Require independent workload, tool bytes, machine facts and declared target policy.

    Equal unknown values cannot establish comparability. This checks the recorded
    contract, not exclusive CPU reservation or application semantic equivalence.
    """
    conditions = read_conditions(value)

    try:
        workload = conditions["workload"]
        tools = conditions["tools"]
        machine = conditions["environment"]["machine"]
        artifacts = workload["artifacts"]
        controller = tools["controller"]
        payload = controller["narrow"]["payload_sha256"]
        python = controller["python"]
        generator = tools["generator"]
        target_settings = conditions["policy"]["target_settings"]
        checksums = [*artifacts.values(), *payload.values(), python["sha256"], generator["sha256"]]

        if (
            not isinstance(artifacts, dict) or not artifacts or not isinstance(payload, dict) or not payload
            or not isinstance(target_settings, dict) or not target_settings
            or not isinstance(workload["loads"], list) or not workload["loads"]
            or any(not isinstance(checksum, str) or DIGEST.fullmatch(checksum) is None for checksum in checksums)
            or any(not isinstance(python[name], str) or not python[name] for name in ("version", "implementation", "abi"))
            or type(python["gil_enabled"]) is not bool
            or any(not isinstance(generator[name], str) or not generator[name] for name in ("name", "version"))
        ):
            raise ValueError("Unknown workload, tool, machine or target conditions")

        _require_known_http(workload["http"])
        _require_known_machine(machine)
        limits = conditions["environment"]["generator_limits"]

        if not isinstance(limits, dict) or ProcessLimits(**limits).serialize() != limits:
            raise ValueError("Unknown generator limit conditions")

        if type(conditions["policy"]["repeats"]) is not int or conditions["policy"]["repeats"] <= 0:
            raise ValueError("Unknown measurement repeat policy")

        for load in workload["loads"]:
            LoadSpec(**load)

    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("Unknown workload, tool, machine or target conditions") from error

    return conditions


def _require_known_http(value: dict) -> None:
    required = {
        "scheme", "path_query_sha256", "method", "headers_sha256", "body", "ca_file",
        "expected_status", "expected_response_bytes",
    }

    if not isinstance(value, dict) or set(value) != required or value["scheme"] not in ("http", "https"):
        raise ValueError("Unknown HTTP workload conditions")

    RequestSpec(
        method=value["method"], expected_status=value["expected_status"],
        expected_response_bytes=value["expected_response_bytes"],
    )

    for name in ("path_query_sha256", "headers_sha256"):

        if not isinstance(value[name], str) or DIGEST.fullmatch(value[name]) is None:
            raise ValueError("Unknown HTTP workload digest")

    for name in ("body", "ca_file"):
        item = value[name]

        if item is not None and (
            not isinstance(item, dict) or set(item) != {"sha256", "bytes"}
            or not isinstance(item["sha256"], str) or DIGEST.fullmatch(item["sha256"]) is None
            or type(item["bytes"]) is not int or item["bytes"] < 0
        ):
            raise ValueError("Unknown HTTP input conditions")


def _require_known_machine(machine: dict) -> None:

    if not isinstance(machine, dict) or machine.get("unavailable") != {}:
        raise ValueError("Unknown machine conditions")

    for name in ("kernel", "architecture"):

        if not isinstance(machine[name], str) or not machine[name]:
            raise ValueError("Unknown machine identity")

    for name in ("clock_ticks", "page_size"):

        if type(machine[name]) is not int or machine[name] <= 0:
            raise ValueError("Unknown machine resolution")

    affinity, roles = machine["affinity"], machine["cpu_roles"]

    if (
        not isinstance(affinity, list) or not affinity or len(set(affinity)) != len(affinity)
        or any(type(cpu) is not int or cpu < 0 for cpu in affinity)
        or not isinstance(roles, dict) or not roles or any(type(cpu) is not int or cpu < 0 for cpu in roles.values())
    ):
        raise ValueError("Unknown machine CPU placement")

    cpus = {f"{cpu}" for cpu in roles.values()}

    for name in ("processors", "topology", "governors"):

        if not isinstance(machine[name], dict) or set(machine[name]) != cpus:
            raise ValueError("Unknown selected CPU identity")

    for cpu in cpus:
        processor, topology = machine["processors"][cpu], machine["topology"][cpu]

        if (
            not isinstance(processor, dict) or not processor
            or any(not isinstance(fact, str) or not fact for fact in processor.values())
            or not isinstance(topology, dict) or topology.get("unavailable") != {}
            or any(not isinstance(topology[name], str) or not topology[name] for name in (
                "package", "core", "siblings", "max_frequency_khz", "governor",
            ))
            or topology["governor"] != machine["governors"][cpu]
        ):
            raise ValueError("Unknown selected CPU facts")


def build_http_workload(
    url: str, request: RequestSpec, input_metadata: Mapping, loads: tuple[LoadSpec, ...], artifacts: Mapping[str, str],
) -> dict:
    """Record actual request inputs without origin, local filenames or query secrets."""
    files = {}

    for name in ("body", "ca_file"):
        metadata = input_metadata[name]

        if metadata is not None:

            if (
                not isinstance(metadata, Mapping) or not isinstance(metadata.get("sha256"), str)
                or DIGEST.fullmatch(metadata["sha256"]) is None
                or type(metadata.get("bytes")) is not int or metadata["bytes"] < 0
            ):
                raise ValueError("HTTP workload input has no verified content digest")

            files[name] = {"sha256": metadata["sha256"], "bytes": metadata["bytes"]}

        else:
            files[name] = None

    return {
        "http": {
            **describe_http_endpoint(request.build_url(url)),
            "method": request.method, "headers_sha256": request.serialize()["headers_sha256"], **files,
            "expected_status": request.expected_status, "expected_response_bytes": request.expected_response_bytes,
        },
        "loads": [asdict(load) for load in loads], "artifacts": dict(artifacts),
    }


def describe_http_endpoint(url: str) -> dict[str, str]:
    """Fingerprint the exact relative URL without disclosing query values or comparing origins."""
    validate_url(url)
    parsed = urlsplit(url)
    relative = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    return {"scheme": parsed.scheme, "path_query_sha256": hashlib.sha256(relative.encode()).hexdigest()}
