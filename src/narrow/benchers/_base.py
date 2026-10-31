"""A small, single-attempt generator contract; scheduling belongs to narrow.runner."""

import hashlib
import math
import re
import shutil
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from .._base import Component
from ..configuration import LoadSpec
from ..exceptions import BencherException, NarrowException
from ..processes import ProcessHandle, run_command
from ..results import SampleResult

BENCHERS = {}


def register_bencher(cls: type) -> type:
    """Register a class without constructing a generator or running external commands."""
    BENCHERS[cls.alias] = cls
    return cls


class Bencher(Component):
    """Prepare explicitly, then run one bounded request-count workload."""

    version_flag = "--version"
    expected_version: str | None = None

    def __init__(self) -> None:
        self.binary = self.prc_name
        self.provenance: dict | None = None

    def prepare(self) -> dict:
        """Record the actual executable, version and digest without installing tools."""
        path = shutil.which(self.prc_name)

        if path is None:
            raise BencherException(f"Generator {self.prc_name} is not installed")

        self.binary = f"{Path(path).resolve()}"
        version = run_command([self.binary, self.version_flag])

        if self.expected_version is not None and version.strip() != self.expected_version:
            raise BencherException(f"Expected {self.expected_version}, found {version}")

        self.provenance = {
            "path": self.binary, "version": version,
            "sha256": hashlib.sha256(Path(self.binary).read_bytes()).hexdigest(),
        }
        return self.provenance

    def get_version(self) -> str:
        """Read the prepared executable's version."""
        return self.prepare()["version"]

    def validate_load(self, url: str, load: LoadSpec) -> None:
        """Legacy generators support count/plain HTTP only, with a process watchdog."""

        if urlsplit(url).scheme != "http" or load.duration is not None or load.qps is not None:
            raise BencherException(f"{self.alias}: unsupported TLS, duration or QPS workload")

        if load.timeout != 5.0 or load.connect_timeout != 2.0:
            raise BencherException(f"{self.alias}: custom request/connect timeouts are unsupported; use the watchdog")

    def build_argv(self, url: str, load: LoadSpec) -> list[str]:
        """Build arguments for one attempt without starting a process."""
        raise NotImplementedError

    def parse_report(self, raw: bytes, load: LoadSpec) -> SampleResult:
        """Parse legacy text, retaining partial HTTP counts and a numeric 2xx rate."""
        text = raw.decode("utf-8", errors="replace")
        rates = re.search(r"finished in (\d+(?:\.\d+)?)(us|ms|s),[^\n]*? (\d+(?:\.\d+)?) req/s,", text)
        statuses = re.search(r"status codes: (\d+) 2xx, (\d+) 3xx, (\d+) 4xx, (\d+) 5xx", text)

        if rates is None or statuses is None:
            raise BencherException(f"Cannot parse {self.alias} report")

        window = float(rates[1]) * {"us": 0.000001, "ms": 0.001, "s": 1}[rates[2]]
        counts = dict(zip(("2xx", "3xx", "4xx", "5xx"), map(int, statuses.groups()), strict=True))
        responses = sum(counts.values())

        reported_rps = float(rates[3])

        if responses > load.requests or window <= 0 or not math.isfinite(window) or not math.isfinite(reported_rps):
            raise BencherException(f"Inconsistent {self.alias} response counts or duration")

        complete = counts["2xx"] == load.requests
        return SampleResult(
            load.clients, status="ok" if complete else "http_error",
            reason=None if complete else f"{counts["2xx"]}/{load.requests} requests returned 2xx",
            rps=reported_rps if complete else counts["2xx"] / window,
            window_seconds=window, responses=responses, successful_responses=counts["2xx"], statuses=counts,
            errors={"unreported outcomes": load.requests - responses} if responses < load.requests else {},
        )

    def run_once(self, url: str, load: LoadSpec, *, timeout: float) -> SampleResult:
        """Keep raw evidence on cancellation, timeout, parse and generator failure."""
        self.validate_load(url, load)

        if self.provenance is None:
            self.prepare()

        process = ProcessHandle(self.build_argv(url, load))
        began = monotonic()
        result = None
        failure = None
        cleanup_error = None
        started = False

        try:
            process.start()
            started = True
            result = process.wait(timeout=timeout)

            if result.error or result.returncode:
                failure = result.error or f"Generator exited with code {result.returncode}"

        except KeyboardInterrupt as error:
            failure = "cancelled"
            cleanup_error = "; ".join(getattr(error, "__notes__", ())) or None

        except (OSError, NarrowException) as error:
            failure = f"{error}"
            cleanup_error = failure if started else None

        stdout, stderr = process.collect_output()

        try:
            sample = self.parse_report(stdout, load)

        except BencherException as error:
            sample = SampleResult(load.clients, status="invalid", reason=f"{error}")

        if failure:
            sample.status = "cancelled" if failure == "cancelled" else "error"
            sample.reason = failure

        sample.raw_stdout, sample.raw_stderr = stdout, stderr
        sample.argv = process.argv
        sample.returncode = result.returncode if result else process.get_returncode()
        sample.elapsed = monotonic() - began
        sample.cleanup_error = cleanup_error
        return sample
