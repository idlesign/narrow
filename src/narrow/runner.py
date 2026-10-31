"""Sequential HTTP runs with explicit workloads, bounded phases and durable evidence."""

import json
import logging
import math
import platform
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from .apps import APPS
from .reports.artifacts import RunStore
from .benchers import BENCHERS
from .configuration import LoadSpec, Settings, bootstrap
from .exceptions import NarrowException
from .requests import RequestSpec, snapshot_request
from .requests import redact_url
from .stands import STANDS
from .targets import StandTarget, Target

LOG = logging.getLogger(__name__)
CLEANUP_RESERVE = 15.0

@dataclass(frozen=True)
class RunOptions:
    """Select explicit built-in or user targets, never an implicit all-server benchmark."""

    stands: tuple[str, ...] = ()
    loads: tuple[LoadSpec, ...] = ()
    apps: tuple[str, ...] = ("py",)
    targets: tuple[Target, ...] = ()
    request: RequestSpec = RequestSpec()
    bencher: str = "oha"
    repeats: int = 1
    warmup_requests: int = 0
    attempt_timeout: float = 30.0
    ready_timeout: float = 5.0
    budget: float = 120.0

    def __post_init__(self) -> None:

        if not (self.stands or self.targets) or not self.loads or (self.stands and not self.apps):
            raise ValueError("Select at least one target, workload and application for built-in stands")

        if len(set(self.stands)) != len(self.stands) or len(set(self.apps)) != len(self.apps):
            raise ValueError("Duplicate stand/application selection")

        if len({load.clients for load in self.loads}) != len(self.loads):
            raise ValueError("Duplicate concurrency point")

        for name, minimum in (("repeats", 1), ("warmup_requests", 0)):
            value = getattr(self, name)

            if type(value) is not int or value < minimum:
                raise ValueError(f"Invalid {name}")

        for name in ("attempt_timeout", "ready_timeout", "budget"):
            value = getattr(self, name)

            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

        for load in self.loads:

            if load.duration is not None and load.duration + load.timeout + 1 > self.attempt_timeout:
                raise ValueError("attempt_timeout must cover duration, request timeout and reporting")

        if self.budget <= self.ready_timeout + CLEANUP_RESERVE:
            raise ValueError("budget must reserve time for readiness and cleanup")

    def describe(self) -> dict:
        """Serialize selection without opening input files or persisting secret values."""
        return {
            "stands": self.stands, "apps": self.apps, "loads": [asdict(load) for load in self.loads],
            "targets": [target.describe() for target in self.targets], "request": self.request.serialize(),
            **{name: getattr(self, name) for name in (
                "bencher", "repeats", "warmup_requests", "attempt_timeout", "ready_timeout", "budget",
            )},
        }

class Runner:
    """One executor for external URLs, foreground commands and selected built-in stands."""

    def __init__(self, options: RunOptions) -> None:
        self.options = options

    def create_targets(self) -> list[Target]:
        """Resolve selections without inspecting tools, touching files or starting processes."""
        options = self.options

        for selected, registry, title in (
            (options.stands, STANDS, "stand"), (options.apps if options.stands else (), APPS, "application"),
            ((options.bencher,), BENCHERS, "bencher"),
        ):

            for alias in selected:

                if alias not in registry:
                    raise NarrowException(f"Unknown {title}: {alias}")

        targets = [
            StandTarget(alias, app_alias) for alias in options.stands
            for app_alias in (options.apps if STANDS[alias].uses_app else (None,))
        ] + list(options.targets)
        identities = [
            (target.alias, target.app_alias, json.dumps(target.describe().get("subject"), sort_keys=True)) for target in targets
        ]

        if len(set(identities)) != len(identities):
            raise NarrowException("Duplicate target name/application")

        return targets

    def plan(self) -> dict:
        """Show the finite plan without tool execution, file reads or HTTP probes."""
        targets = self.create_targets()
        options = self.options
        bencher = BENCHERS[options.bencher]()

        for target in targets:
            request = replace(options.request, ca_file=options.request.ca_file or target.ca_file)

            for load in options.loads:
                bencher.validate_load(request.build_url(target.url), load, request=request)

        return {
            "status": "planned", "runtime_tools_verified": False, "input_files_verified": False,
            "options": options.describe(), "cases": [
                {**target.describe(), "request_url": redact_url(options.request.build_url(target.url))} for target in targets
            ],
            "generator_attempts": len(targets) * len(options.loads) * (options.repeats + bool(options.warmup_requests)),
            "upper_time_estimate_seconds": len(targets) * (
                2 * options.ready_timeout + CLEANUP_RESERVE
                + len(options.loads) * (options.repeats + bool(options.warmup_requests)) * options.attempt_timeout
            ),
        }

    def run(self, output: Path) -> dict:
        """Preflight all cases; freeze inputs before the budget clock; checkpoint every attempt."""
        options = self.options
        targets = self.create_targets()
        bencher = BENCHERS[options.bencher]()
        tools = {}
        app_info = {}
        manifests = []

        for target in targets:
            request = replace(options.request, ca_file=options.request.ca_file or target.ca_file)

            for load in options.loads:
                bencher.validate_load(request.build_url(target.url), load, request=request)

            manifest = target.prepare()
            manifests.append(manifest)
            tools.update(manifest.get("servers", {}))
            app_info.update(manifest.get("apps", {}))

        generator = bencher.prepare()
        estimate = self.plan()["upper_time_estimate_seconds"]
        store = RunStore(Path(output), {
            "date": datetime.now(UTC).isoformat(), "python": sys.version, "platform": platform.platform(),
            "options": options.describe(), "generator": generator, "servers": tools, "apps": app_info,
            "targets": manifests, "environment": bootstrap(), "upper_time_estimate_seconds": estimate,
            "server_settings": {
                "workers": Settings.WORKERS, "threads": Settings.THREADS,
                "max_connections": Settings.MAX_CONNECTIONS, "access_logs": Settings.LOG_WRITE,
                "listen_backlog": Settings.LISTEN_BACKLOG,
            } if options.stands else None,
            "legacy_timeout_policy": "process watchdog only" if options.bencher != "oha" else None,
        })
        store.prepare()

        try:
            base_request, input_metadata = snapshot_request(options.request, store.path / "inputs")

            for name in ("body", "ca_file"):

                if input_metadata[name] is not None:
                    input_metadata[name]["artifact"] = f"inputs/{input_metadata[name]['name']}"

            store.data["meta"]["request"] = input_metadata
            store.checkpoint()
            deadline = monotonic() + options.budget

            if estimate > options.budget:
                LOG.warning("Worst-case run time %.1fs exceeds budget %.1fs; the run may stop early", estimate, options.budget)

            for target in targets:
                request = replace(base_request, ca_file=base_request.ca_file or target.ca_file)
                identity = {
                    "stand": target.alias, "app": target.app_alias, "session": len(store.data["stands"]),
                    "subject": target.describe().get("subject"),
                }

                def take_sample(load: LoadSpec, phase: str, repeat: int) -> None:
                    remaining = deadline - monotonic() - CLEANUP_RESERVE

                    if remaining <= 0:
                        raise NarrowException("Run budget exhausted (cleanup time reserved)")

                    if load.duration is not None and remaining < load.duration + load.timeout + 1:
                        raise NarrowException("Insufficient run budget for the requested duration and drain")

                    sample = bencher.run_once(
                        target.url, load, request=request, timeout=min(options.attempt_timeout, remaining),
                    )
                    store.save_attempt(sample, {
                        **identity, "phase": phase, "repeat": repeat, "load": asdict(load),
                        "date": datetime.now(UTC).isoformat(),
                    })

                    if sample.status == "cancelled":
                        raise KeyboardInterrupt

                    if sample.cleanup_error:
                        raise NarrowException(f"Generator cleanup failed: {sample.cleanup_error}")

                    if phase == "warmup" and sample.status != "ok":
                        raise NarrowException("Warmup failed; measurement not started")

                    target.check_running()

                try:

                    if deadline - monotonic() <= 2 * options.ready_timeout + CLEANUP_RESERVE:
                        raise NarrowException("Insufficient run budget for readiness and cleanup")

                    with target.setup(timeout=options.ready_timeout, ca_file=request.ca_file):

                        for load in options.loads:

                            if options.warmup_requests:
                                take_sample(replace(load, requests=options.warmup_requests, duration=None), "warmup", 0)

                            for repeat in range(1, options.repeats + 1):
                                take_sample(load, "measurement", repeat)

                finally:
                    primary = sys.exception()

                    try:
                        store.save_target(target, identity, request=request)

                    except OSError as error:

                        if primary is None:
                            raise

                        primary.add_note(f"Could not save target evidence: {error}")
                        LOG.error("Could not save target evidence: %s", error)

                if any(item["cleanup_error"] for item in store.data["stands"][-1]["processes"]):
                    raise NarrowException("Stand cleanup failed; further workloads will not run")

                if any(item["output_truncated"] for item in store.data["stands"][-1]["processes"]):
                    raise NarrowException("Stand output limit exceeded; captured diagnostics are truncated")

            store.data["status"] = "complete" if all(attempt["status"] == "ok" for attempt in store.data["attempts"]) else "failed"

        except KeyboardInterrupt:
            store.data.update(status="cancelled", reason="Interrupted by user")
            raise

        except (OSError, ValueError, NarrowException) as error:
            store.data.update(status="failed", reason=f"{error}")
            raise

        finally:
            primary = sys.exception()

            try:
                store.checkpoint()

            except OSError as error:

                if primary is None:
                    raise

                primary.add_note(f"Could not save final checkpoint: {error}")
                LOG.error("Could not save final checkpoint: %s", error)

        return store.data
