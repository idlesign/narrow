"""Normalized metrics, with raw process output kept separately."""

from dataclasses import asdict, dataclass, field


@dataclass
class SampleResult:
    """One attempt; absent metrics remain None, never synthetic zero measurements."""

    clients: int
    status: str = "error"
    reason: str | None = None
    rps: float | None = None
    completed_rps: float | None = None
    response_body_bytes: int | None = None
    request: dict = field(default_factory=dict)
    artifacts_redacted: bool = False
    window_seconds: float | None = None
    responses: int | None = None
    successful_responses: int | None = None
    statuses: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    deadline_outcomes: int | None = None
    latency_ms: dict[str, float | None] = field(default_factory=dict)
    latency_population: str = "all HTTP responses, including non-2xx"
    argv: tuple[str, ...] = ()
    returncode: int | None = None
    elapsed: float = 0.0
    cleanup_error: str | None = None
    execution: dict = field(default_factory=dict)
    raw_stdout: bytes = b""
    raw_stderr: bytes = b""

    def serialize(self) -> dict:
        """Return JSON metadata without duplicating raw output in the checkpoint."""
        data = asdict(self)
        data.pop("raw_stdout")
        data.pop("raw_stderr")
        data["argv"] = list(self.argv)
        return data
