"""Strict offline comparisons; saved runs remain independent, never pooled trials."""

import json
import math
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path

from ..configuration import LoadSpec
from ..exceptions import NarrowException
from ..measurement.conditions import compare_conditions, require_known_conditions
from ..processes.limits import ProcessLimits
from ..targets.subjects import read_subject
from .artifacts import stats_load
from .asv import summarize_results
from .comparison import ComparisonReport, collect_rows
from .metrics import MetricSpec
from .plotting import RunReport, format_target_label
from .scalars import ScalarSample, ScalarSeries, compare_scalar_series

__all__ = ["ComparisonReport", "RunSelection", "ScalarSample", "ScalarSeries", "compare_runs", "compare_scalar_series"]

METRICS = {
    "rps": MetricSpec("Successful responses/s (2xx)", higher_is_better=True),
    "p50": MetricSpec("Latency p50 (ms; median of attempt p50)", higher_is_better=False),
    "p95": MetricSpec("Latency p95 (ms; median of attempt p95)", higher_is_better=False),
    "p99": MetricSpec("Latency p99 (ms; median of attempt p99)", higher_is_better=False),
    "unsuccessful_fraction": MetricSpec("Unsuccessful observed outcome fraction", higher_is_better=False),
    "failed_or_missing_repeats": MetricSpec("Failed or missing repeats", higher_is_better=False),
}

@dataclass(frozen=True)
class RunSelection:
    """Select one saved stand/app/subject session, without reading its checkpoint."""

    source: Path
    stand: str | None = None
    app: str | None = None
    subject: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Path(self.source))

        for name in ("stand", "app", "subject"):

            if (value := getattr(self, name)) is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"Comparison {name} selector must be nonempty text")

def _require_request(request: dict, expected: dict) -> None:

    for name in ("scheme", "path_query_sha256", "method", "headers_sha256", "expected_status", "expected_response_bytes"):

        if name not in request or request[name] != expected[name]:
            raise NarrowException(f"Saved request differs from measurement conditions: {name}")

    for name in ("body", "ca_file"):
        actual = request[name]

        if actual is not None:
            actual = {key: actual[key] for key in ("sha256", "bytes")}

        if actual != expected[name]:
            raise NarrowException(f"Saved request differs from measurement conditions: {name}")

def _read_applied_limits(attempt: dict, requested: dict) -> dict | None:
    execution = attempt.get("execution")
    guards = execution.get("limits") if isinstance(execution, dict) else None

    if not isinstance(guards, dict) or guards.get("error") or guards.get("requested") != requested:
        return None

    applied = guards.get("applied")
    fields = {
        "cpus", "core_dump_bytes", "address_space_bytes", "address_space_hard_bytes", "file_size_bytes", "file_size_hard_bytes",
    }

    if not isinstance(applied, dict) or set(applied) != fields or type(applied["core_dump_bytes"]) is not int:
        return None

    try:
        actual = ProcessLimits(**{name: applied[name] for name in ("cpus", "address_space_bytes", "file_size_bytes")})
        ProcessLimits(address_space_bytes=applied["address_space_hard_bytes"], file_size_bytes=applied["file_size_hard_bytes"])

    except ValueError:
        return None

    if applied["cpus"] is None or applied["core_dump_bytes"] != 0 or any(
        requested[name] is not None and (
            actual.serialize()[name] != requested[name]
            or name != "cpus" and applied[name.replace("_bytes", "_hard_bytes")] != requested[name]
        ) for name in ("cpus", "address_space_bytes", "file_size_bytes")
    ):
        return None

    return {**applied, "cpus": list(actual.cpus)}

def _read_selection(selection: RunSelection, index: int) -> tuple[dict, dict, list[dict], dict]:
    data = stats_load(selection.source)
    json.dumps(data, allow_nan=False)
    sessions = [
        stand for stand in data["stands"]
        if (selection.stand is None or stand["stand"] == selection.stand)
        and (selection.app is None or stand["app"] == selection.app)
        and (
            selection.subject is None or (read_subject(stand.get("subject")) or {}).get("fingerprint") == selection.subject
        )
    ]

    if not sessions:
        raise NarrowException("No saved session matches the comparison selection")

    if len(sessions) != 1:
        raise NarrowException("Ambiguous saved sessions; select one stand/app/subject per source")

    stand = sessions[0]
    subject = read_subject(stand.get("subject"))

    if type(stand["session"]) is not int or stand["session"] < 0:
        raise NarrowException("Invalid saved session identity")

    if subject is None or not subject["artifact_sha256"]:
        raise NarrowException("Comparison requires saved subject artifact hashes, not only a revision label")

    conditions = require_known_conditions(stand.get("conditions"))
    options = data["meta"]["options"]

    if (
        data["meta"]["measurement"]["controller"] != conditions["tools"]["controller"]
        or data["meta"]["measurement"]["machine"] != conditions["environment"]["machine"]
        or data["meta"]["measurement"]["conditions"][stand["session"]] != conditions
    ):
        raise NarrowException("Saved session differs from preflight measurement conditions")

    if [asdict(LoadSpec(**load)) for load in options["loads"]] != conditions["workload"]["loads"]:
        raise NarrowException("Saved loads differ from measurement conditions")

    for name in ("repeats", "warmup_requests"):

        if options[name] != conditions["policy"][name]:
            raise NarrowException(f"Saved run policy differs from measurement conditions: {name}")

    if options["generator_limits"] != conditions["environment"]["generator_limits"]:
        raise NarrowException("Saved generator limits differ from measurement conditions")

    generator = conditions["tools"]["generator"]

    if options["bencher"] != generator["name"] or any(
        data["meta"]["generator"][name] != generator[name] for name in ("version", "sha256")
    ):
        raise NarrowException("Saved generator differs from measurement conditions")

    attempts = [attempt for attempt in data["attempts"] if attempt["session"] == stand["session"]]

    for attempt in attempts:

        if (
            attempt["conditions_fingerprint"] != conditions["fingerprint"]
            or (attempt["stand"], attempt["app"]) != (stand["stand"], stand["app"])
        ):
            raise NarrowException("Saved attempt differs from its session measurement conditions")

        _require_request(attempt["request"], conditions["workload"]["http"])

    metrics = summarize_results(data, stand=stand["stand"], app=stand["app"], subject=subject["fingerprint"])
    summary = {
        clients: {
            name: (
                converted if math.isfinite(converted := value * 1000 if name in ("p50", "p95", "p99") else value) else None
            )
            for name, value in values.items()
        }
        for clients, values in metrics.items()
    }
    applied, missing_guards = [], []

    for attempt in attempts:

        if attempt["phase"] != "measurement" or attempt["status"] not in ("ok", "http_error") or attempt["cleanup_error"]:
            continue

        guards = _read_applied_limits(attempt, options["generator_limits"])

        if guards is None:
            missing_guards.append(attempt)

        elif guards not in applied:
            applied.append(guards)

    if len(applied) > 1:
        raise NarrowException("Changed applied generator limits within one saved run")

    for clients in {attempt["clients"] for attempt in missing_guards}:

        for name in METRICS.keys() - {"failed_or_missing_repeats"}:
            summary[clients][name] = None

        summary[clients]["failed_or_missing_repeats"] = min(
            options["repeats"], summary[clients]["failed_or_missing_repeats"]
            + sum(attempt["clients"] == clients and attempt["status"] == "ok" for attempt in missing_guards),
        )

    populations = {}

    for clients, values in summary.items():

        if any(values[name] is not None for name in ("p50", "p95", "p99")):
            population = next(attempt["latency_population"] for attempt in attempts if (
                attempt["phase"] == "measurement" and attempt["clients"] == clients
                and attempt["status"] in ("ok", "http_error")
            ))

            if not isinstance(population, str) or not population:
                raise NarrowException("Unknown saved latency population")

            populations[clients] = population

    series = {
        "id": f"R{index}", "source": f"{selection.source.absolute()}", "stand": stand["stand"], "app": stand["app"],
        "session": stand["session"], "subject": subject, "conditions": conditions,
        "status": data["status"], "reason": data.get("reason"), "metrics": summary,
        "attempts": attempts, "processes": stand["processes"],
        "applied_generator_limits": applied[0] if applied else None,
        "latency_populations": populations, "host_observations": stand.get("host_observations"),
        "label": format_target_label(stand["stand"], stand["app"], subject), "comparability": conditions["fingerprint"],
        "evidence": {"processes": stand["processes"], "host_observations": stand.get("host_observations")},
    }
    return series, data, attempts, stand


def compare_runs(selections: Sequence[RunSelection | Path], *, factor: str) -> ComparisonReport:
    """Read at least two selections; require known equal conditions and one varying factor."""

    if factor not in ("revision", "platform"):
        raise ValueError("Comparison factor must be revision or platform")

    if isinstance(selections, (str, bytes, Path)) or len(selections) < 2:
        raise ValueError("Comparison requires at least two saved runs")

    series, chart_attempts, issues = [], [], []
    applied = None
    populations = {}

    try:

        for index, selection in enumerate(selections, 1):
            selected, data, attempts, stand = _read_selection(
                selection if isinstance(selection, RunSelection) else RunSelection(selection), index,
            )

            if series and (differences := compare_conditions(series[0]["conditions"], selected["conditions"])):
                raise NarrowException(f"Incompatible saved measurement conditions: {', '.join(differences)}")

            guards = selected["applied_generator_limits"]

            if applied is not None and guards is not None and applied != guards:
                raise NarrowException("Different applied generator limits between saved runs")

            if guards is not None:
                applied = guards

            for clients, population in selected["latency_populations"].items():

                if clients in populations and populations[clients] != population:
                    raise NarrowException("Different saved latency populations between runs")

                populations[clients] = population

            series.append(selected)

            if data["status"] != "complete" or data.get("reason"):
                issues.append({"series": selected["id"], "scope": "run", "detail": data.get("reason") or data["status"]})

            for process in stand["processes"]:

                if process.get("cleanup_error") or process.get("output_truncated"):
                    issues.append({
                        "series": selected["id"], "scope": "target",
                        "detail": process.get("cleanup_error") or "Process output truncated",
                    })

            for clients, metrics in selected["metrics"].items():

                if metrics["rps"] is None:
                    issues.append({
                        "series": selected["id"], "scope": f"clients={clients}",
                        "detail": "Missing or invalid repeat set, cleanup or applied generator limits",
                    })

            for attempt in attempts:
                copy = deepcopy(attempt)
                copy.update(
                    stand=f"{selected['id']} · {selected['stand']}", session=index,
                    request=selected["conditions"]["workload"]["http"],
                )

                if attempt["status"] != "ok" or attempt.get("reason") or attempt.get("cleanup_error"):
                    issues.append({
                        "series": selected["id"],
                        "scope": f"{attempt['phase']} repeat={attempt['repeat']} clients={attempt['clients']}",
                        "detail": "; ".join(f"{value}" for value in (
                            attempt["status"], attempt.get("reason"), attempt.get("cleanup_error"),
                        ) if value),
                    })

                if attempt["phase"] == "measurement":
                    metrics = selected["metrics"][attempt["clients"]]
                    copy["rps"] = attempt["rps"] if metrics["rps"] is not None else None
                    copy["latency_ms"] = {
                        name: attempt["latency_ms"].get(name) if metrics[name] is not None else None
                        for name in ("p50", "p95", "p99")
                    }

                chart_attempts.append(copy)

            measured = {attempt["clients"] for attempt in attempts if attempt["phase"] == "measurement"}
            population = next((
                attempt["latency_population"] for attempt in attempts if attempt["phase"] == "measurement"
            ), "unavailable")

            for load in data["meta"]["options"]["loads"]:

                if load["clients"] not in measured:
                    chart_attempts.append({
                        "stand": f"{selected['id']} · {selected['stand']}", "app": stand["app"], "session": index,
                        "subject": selected["subject"], "phase": "measurement", "repeat": 0, "clients": load["clients"],
                        "load": load, "request": selected["conditions"]["workload"]["http"], "status": "missing",
                        "latency_ms": {}, "rps": None, "latency_population": population,
                    })

        platforms = {item["subject"]["platform"] for item in series}
        identities = {item["subject"]["fingerprint"] for item in series}

        if factor == "revision" and (len(platforms) != 1 or len(identities) < 2):
            raise NarrowException("Revision comparison requires one platform and different saved subject identities")

        if factor == "platform" and len(platforms) < 2:
            raise NarrowException("Platform comparison requires different saved platforms")

    except (KeyError, IndexError, TypeError, ValueError, OverflowError, RecursionError) as error:
        raise NarrowException(f"Malformed or unknown saved comparison conditions: {error}") from error

    result = {
        "factor": factor, "baseline": "R1", "series": series,
        "rows": collect_rows(series, METRICS, coordinate_key="clients"), "issues": issues,
        "title": f"narrow saved-run comparison · {factor}", "coordinate_label": "Clients",
        "notes": [
            "HTTP comparability requires known, equal saved MeasurementConditions and confirmed generator guards.",
            "Process-lifetime facts are not server CPU measurements or proof that the generator is not a bottleneck.",
            "Caller-supplied subject/workload facts must be bound to the running application by its platform adapter.",
        ],
    }
    chart_stats = {
        "status": "complete", "meta": {"generator": {"version": series[0]["conditions"]["tools"]["generator"]["version"]}},
        "attempts": chart_attempts,
    }
    return ComparisonReport(result, RunReport(chart_stats))
