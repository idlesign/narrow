"""Read-only ASV tracks; the caller selects the report and binds its subject revision."""

import math
import json
from abc import ABC, abstractmethod
from collections import defaultdict
from pathlib import Path
from statistics import median

from .artifacts import stats_load
from ..configuration import LoadSpec
from ..exceptions import NarrowException
from ..targets.subjects import read_subject

type RunMetrics = dict[int, dict[str, float]]

def _read_number(value: object, name: str) -> float:
    """Read a finite nonnegative observation, not a boolean or missing value."""

    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise NarrowException(f"Invalid saved metric: {name}")

    return float(value)

def _summarize_attempts(attempts: list[dict], repeats: int, clean_sessions: set[int]) -> dict[str, float]:
    """Keep incomplete trials as gaps and count HTTP errors separately from throughput."""
    parsed = [
        attempt for attempt in attempts
        if attempt["status"] in ("ok", "http_error") and not attempt["cleanup_error"]
        and attempt["session"] in clean_sessions
    ]
    result = dict.fromkeys(("rps", "p50", "p95", "p99", "unsuccessful_fraction"), math.nan)
    result["failed_or_missing_repeats"] = float(repeats - sum(attempt["status"] == "ok" for attempt in parsed))

    if len(parsed) != repeats:
        return result

    if len({json.dumps(attempt.get("request", {}), sort_keys=True) for attempt in parsed}) != 1:
        raise NarrowException("Mixed saved HTTP requests")

    result["rps"] = median(_read_number(attempt["rps"], "rps") for attempt in parsed)
    fractions = []

    for attempt in parsed:
        responses = attempt["responses"]
        successes = attempt["successful_responses"]
        errors = attempt["errors"]

        if (
            type(responses) is not int or type(successes) is not int or not 0 <= successes <= responses
            or not isinstance(errors, dict) or any(type(count) is not int or count < 0 for count in errors.values())
        ):
            raise NarrowException("Invalid saved outcome counts")

        outcomes = responses + sum(errors.values())
        fractions.append((outcomes - successes) / outcomes if outcomes else math.nan)

    result["unsuccessful_fraction"] = median(fractions) if all(math.isfinite(value) for value in fractions) else math.nan

    if len({attempt["latency_population"] for attempt in parsed}) != 1:
        raise NarrowException("Mixed saved latency populations")

    if any(not isinstance(attempt["latency_ms"], dict) for attempt in parsed):
        raise NarrowException("Invalid saved latency metrics")

    for percentile in ("p50", "p95", "p99"):
        values = [attempt["latency_ms"].get(percentile) for attempt in parsed]

        if all(value is not None for value in values):
            result[percentile] = median(_read_number(value, percentile) for value in values) / 1000

    return result

def read_results(path: Path, *, stand: str, app: str | None = None, subject: str | None = None) -> RunMetrics:
    """Read one saved stand/app; incomplete repeats yield NaN, never a partial median."""
    return summarize_results(stats_load(path), stand=stand, app=app, subject=subject)

def summarize_results(data: dict, *, stand: str, app: str | None = None, subject: str | None = None) -> RunMetrics:
    """Validate and summarize one in-memory checkpoint without reading any input files.

    Latency tracks are medians of per-attempt quantiles, not pooled percentiles.
    Their population and QPS correction remain those of the saved experiment.
    This reader does not establish comparability or assign a Git revision.
    Select a subject fingerprint when the same stand/app contains different code.
    """
    try:

        if data["status"] not in ("complete", "failed", "cancelled"):
            raise NarrowException("ASV requires a terminal narrow checkpoint")

        options = data["meta"]["options"]
        repeats = options["repeats"]

        if type(repeats) is not int or repeats <= 0:
            raise NarrowException("Invalid saved repeat count")

        loads = [LoadSpec(**item) for item in options["loads"]]
        by_clients = {load.clients: load for load in loads}

        if not loads or len(by_clients) != len(loads):
            raise NarrowException("Ambiguous saved concurrency points")

        sessions = [item for item in data["stands"] if (item["stand"], item["app"]) == (stand, app)]
        all_session_ids = {item["session"] for item in sessions}

        if len(all_session_ids) != len(sessions):
            raise NarrowException("Duplicate saved stand session")

        identities = {item["session"]: read_subject(item.get("subject")) for item in sessions}

        if subject is not None:
            sessions = [item for item in sessions if (identities[item["session"]] or {}).get("fingerprint") == subject]

        if not sessions:
            raise NarrowException(f"No saved stand/application: {stand}/{app}")

        session_ids = {item["session"] for item in sessions}

        if len({json.dumps(identities[item["session"]], sort_keys=True) for item in sessions}) > 1:
            raise NarrowException("Different saved subjects; select a subject fingerprint")

        clean_sessions = {
            item["session"] for item in sessions if (item["processes"] or item.get("ownership") == "external")
            and all(not process["cleanup_error"] and not process["output_truncated"] for process in item["processes"])
        }
        selected = defaultdict(list)
        seen = set()

        for attempt in data["attempts"]:

            if attempt["phase"] != "measurement" or (attempt["stand"], attempt["app"]) != (stand, app):
                continue

            session = attempt["session"]

            if session not in all_session_ids:
                raise NarrowException("Missing saved stand session evidence")

            if read_subject(attempt.get("subject")) != identities[session]:
                raise NarrowException("Saved attempt subject differs from its stand session")

            if session not in session_ids:
                continue

            load = LoadSpec(**attempt["load"])
            clients = attempt["clients"]
            repeat = attempt["repeat"]

            if type(clients) is not int or load != by_clients.get(clients):
                raise NarrowException("Saved attempt differs from its declared load policy")

            if type(repeat) is not int or not 1 <= repeat <= repeats or (clients, repeat) in seen:
                raise NarrowException("Invalid or duplicate saved repeat")

            seen.add((clients, repeat))
            selected[clients].append(attempt)

        return {clients: _summarize_attempts(selected[clients], repeats, clean_sessions) for clients in by_clients}

    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise NarrowException("Malformed narrow checkpoint for ASV") from error

class SavedRunBenchmark(ABC):
    """Subclass with explicit clients params and a report-only setup_cache.

    ASV skips this abstract class during discovery. Tracks only read the shared
    cache; they never start workloads. Throughput is higher-is-better, so ASV's
    default lower-is-better regression judgement must not be used for that track.
    """

    param_names = ["clients"]

    @abstractmethod
    def setup_cache(self) -> RunMetrics:
        """Return read_results() for the explicitly selected saved experiment."""
        raise NotImplementedError

    def track_successful_responses_per_second(self, results: RunMetrics, clients: int) -> float:
        """Median attempt HTTP 2xx throughput over the whole generator window."""
        return results[clients]["rps"]

    track_successful_responses_per_second.unit = "responses/s"
    track_successful_responses_per_second.pretty_name = "HTTP 2xx throughput (median of attempts)"

    def track_latency_p50_seconds(self, results: RunMetrics, clients: int) -> float:
        """Median of attempt p50 values; not a pooled response percentile."""
        return results[clients]["p50"]

    track_latency_p50_seconds.unit = "seconds"
    track_latency_p50_seconds.pretty_name = "Latency p50 seconds (median of attempt p50; not pooled)"

    def track_latency_p95_seconds(self, results: RunMetrics, clients: int) -> float:
        """Median of attempt p95 values; not a pooled response percentile."""
        return results[clients]["p95"]

    track_latency_p95_seconds.unit = "seconds"
    track_latency_p95_seconds.pretty_name = "Latency p95 seconds (median of attempt p95; not pooled)"

    def track_latency_p99_seconds(self, results: RunMetrics, clients: int) -> float:
        """Median of attempt p99 values; not a pooled response percentile."""
        return results[clients]["p99"]

    track_latency_p99_seconds.unit = "seconds"
    track_latency_p99_seconds.pretty_name = "Latency p99 seconds (median of attempt p99; not pooled)"

    def track_unsuccessful_outcome_fraction(self, results: RunMetrics, clients: int) -> float:
        """Median attempt fraction of observed non-2xx and transport/deadline outcomes."""
        return results[clients]["unsuccessful_fraction"]

    track_unsuccessful_outcome_fraction.unit = "fraction"
    track_unsuccessful_outcome_fraction.pretty_name = "Unsuccessful observed outcome fraction (median of attempts)"

    def track_failed_or_missing_repeats(self, results: RunMetrics, clients: int) -> float:
        """Count failed or absent measurements, including unconfirmed cleanup."""
        return results[clients]["failed_or_missing_repeats"]

    track_failed_or_missing_repeats.unit = "attempts"
    track_failed_or_missing_repeats.pretty_name = "Failed or missing measurement repeats"
