"""oha 1.16.0 adapter; preparation never installs or builds the generator."""

import json
import math
from urllib.parse import urlsplit

from ..configuration import LoadSpec
from ..exceptions import BencherException
from ..reports.results import SampleResult
from ..requests import RequestSpec, validate_url
from ._base import Bencher, register_bencher

DEADLINE_ERROR = "aborted due to deadline"


def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, rejecting repeated keys rather than losing data."""
    result = {}

    for name, value in pairs:

        if name in result:
            raise ValueError(f"Duplicate JSON key: {name}")

        result[name] = value

    return result


def reject_constant(value: str) -> None:
    """Reject non-JSON constants accepted by Python's decoder by default."""
    raise ValueError(f"Invalid JSON constant: {value}")


def read_number(value: object, name: str) -> float:
    """Read a finite, nonnegative metric, excluding booleans."""

    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid nonnegative finite number: {name}")

    return float(value)


def read_distribution(value: object, *, http_statuses: bool = False) -> dict[str, int]:
    """Read integer outcome counts without inventing missing observations."""

    if not isinstance(value, dict):
        raise ValueError("Invalid count distribution")

    for name, count in value.items():

        if not isinstance(name, str) or type(count) is not int or count < 0:
            raise ValueError("Distribution entries must be names and nonnegative integer counts")

        if http_statuses and (not name.isdecimal() or len(name) != 3 or not 100 <= int(name) <= 599):
            raise ValueError("Invalid HTTP status")

    return value


def parse_oha_report(raw: bytes, load: LoadSpec, *, request: RequestSpec = RequestSpec()) -> SampleResult:
    """Calculate whole-run 2xx throughput and preserve the latency population."""

    try:
        data = json.loads(raw, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
        summary = data["summary"]
        statuses = read_distribution(data["statusCodeDistribution"], http_statuses=True)
        errors = read_distribution(data["errorDistribution"])
        responses = sum(statuses.values())
        successful_responses = sum(count for code, count in statuses.items() if code.startswith("2"))
        outcomes = responses + sum(errors.values())
        deadline_outcomes = errors.get(DEADLINE_ERROR, 0)
        window_seconds = read_number(summary["total"], "summary.total")
        body_bytes = summary["totalData"]

        if type(body_bytes) is not int or body_bytes < 0 or (not responses and body_bytes):
            raise ValueError("Invalid response body byte accounting")

        if window_seconds <= 0:
            raise ValueError("Whole-run window must be positive")

        reported_rps = read_number(summary["requestsPerSec"], "summary.requestsPerSec")

        if not math.isclose(reported_rps, outcomes / window_seconds, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("Inconsistent oha outcome rate")

        # oha counts all HTTP statuses as responses and excludes deadline cancellations here.
        denominator = outcomes - deadline_outcomes
        success_rate = summary["successRate"]

        if denominator:

            if not math.isclose(read_number(success_rate, "successRate"), responses / denominator, rel_tol=1e-6):
                raise ValueError("Inconsistent oha successRate")

        elif success_rate is not None:
            raise ValueError("Empty successRate must be null")

        if load.requests is not None and (outcomes != load.requests or deadline_outcomes):
            raise ValueError("Incomplete count workload")

        if load.drain and deadline_outcomes:
            raise ValueError("Unexpected deadline outcomes with drain enabled")

        latency_ms = {}
        percentiles = data["latencyPercentiles"]

        for percentile in ("p50", "p95", "p99"):
            value = percentiles[percentile]

            if responses:
                latency_ms[percentile] = read_number(read_number(value, percentile) * 1000, f"latency_ms.{percentile}")

            elif value is not None:
                raise ValueError("Latency without HTTP responses must be null")

            else:
                latency_ms[percentile] = None

        if responses and not latency_ms["p50"] <= latency_ms["p95"] <= latency_ms["p99"]:
            raise ValueError("Unordered latency percentiles")

    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as error:
        raise BencherException(f"Invalid oha JSON: {error}") from error

    status = "ok" if responses and successful_responses == responses and not errors else "http_error"
    reason = None

    if request.expected_status is not None:
        status = (
            "ok" if responses and statuses == {f"{request.expected_status}": responses} and not errors else "http_error"
        )

        if responses and statuses != {f"{request.expected_status}": responses}:
            status, reason = "response_error", "Unexpected HTTP status"

    if request.expected_response_bytes is not None and body_bytes != responses * request.expected_response_bytes:
        status, reason = "response_error", "Unexpected total response body size"

    return SampleResult(
        clients=load.clients,
        status=status, reason=reason,
        rps=successful_responses / window_seconds,
        completed_rps=responses / window_seconds, response_body_bytes=body_bytes,
        window_seconds=window_seconds,
        responses=responses,
        successful_responses=successful_responses,
        statuses=statuses,
        errors=errors,
        deadline_outcomes=deadline_outcomes,
        latency_ms=latency_ms,
        latency_population="all HTTP responses; latency-corrected" if load.latency_correction else "all HTTP responses",
    )


@register_bencher
class OhaBencher(Bencher):
    """Explicit HTTP/1.1 policies and a checked oha version; lifecycle is inherited."""

    alias = "oha"
    prc_name = "oha"
    expected_version = "oha 1.16.0"
    description = "oha 1.16.0 (HTTP/1.1, JSON)"

    def build_argv(self, url: str, load: LoadSpec, *, request: RequestSpec = RequestSpec()) -> list[str]:
        """Specify policies explicitly instead of relying on generator defaults."""
        argv = [
            self.binary, "--no-tui", "--output-format", "json", "--http-version", "1.1",
            "--worker-threads", f"{load.worker_threads}", "-c", f"{load.clients}",
            "-t", f"{load.timeout}s", "--connect-timeout", f"{load.connect_timeout}s",
            "--redirect", "0", "--disable-compression", "-m", request.method,
        ]
        argv.extend(["-n", f"{load.requests}"] if load.requests is not None else ["-z", f"{load.duration}s"])

        if load.qps is not None:
            argv.extend(["-q", f"{load.qps}"])

        if load.latency_correction:
            argv.append("--latency-correction")

        if load.drain:
            argv.append("-w")

        if not load.keepalive:
            argv.append("--disable-keepalive")

        if urlsplit(url).scheme == "https" and request.ca_file is not None:
            argv.extend(["--cacert", f"{request.ca_file}"])

        for name, value in request.headers:
            argv.extend(["-H", f"{name}: {value}"])

        if request.body is not None:
            argv.extend(["-D", f"{request.body}"])

        argv.append(request.build_url(url))
        return argv

    def validate_load(self, url: str, load: LoadSpec, *, request: RequestSpec = RequestSpec()) -> None:
        """Accept HTTP/HTTPS; numeric combinations are validated by LoadSpec."""

        validate_url(url)

        if request.ca_file is not None and urlsplit(url).scheme != "https":
            raise BencherException("A custom CA requires HTTPS")

    def parse_report(self, raw: bytes, load: LoadSpec, *, request: RequestSpec = RequestSpec()) -> SampleResult:
        """Normalize the checked report while raw bytes stay in the attempt artifacts."""
        return parse_oha_report(raw, load, request=request)
