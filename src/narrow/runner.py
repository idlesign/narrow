"""Sequential HTTP runs with explicit workloads, bounded phases and durable evidence."""

import json
import logging
import math
import os
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from .apps import APPS
from .reports.artifacts import RunStore
from .benchers import BENCHERS
from .configuration import LoadSpec, Settings, bootstrap
from .exceptions import NarrowException
from .measurement.budget import Budget
from .measurement.conditions import MeasurementConditions, build_http_workload, normalize_json, read_conditions
from .measurement.identity import collect_controller_identity, hash_artifacts, hash_payload
from .measurement.system import collect_machine, observe_host
from .processes.limits import ProcessLimits
from .requests import RequestSpec, snapshot_request
from .requests import redact_url
from .reports.results import SampleResult
from .stands import STANDS
from .targets import StandTarget, Target

LOG = logging.getLogger(__name__)
CLEANUP_RESERVE = 15.0

def read_cpu_roles(limits: ProcessLimits) -> dict[str, int]:
    """Read inherited placement without pinning the controller or reserving CPUs."""
    controller = sorted(os.sched_getaffinity(0))
    return {
        **{f"controller-{index}": cpu for index, cpu in enumerate(controller)},
        **{f"generator-{index}": cpu for index, cpu in enumerate(limits.cpus or controller)},
    }

@dataclass(frozen=True)
class RunOptions:
    """Select explicit built-in or user targets, never an implicit all-server benchmark."""

    stands: tuple[str, ...] = ()
    loads: tuple[LoadSpec, ...] = ()
    apps: tuple[str, ...] = ("py",)
    targets: tuple[Target, ...] = ()
    request: RequestSpec = RequestSpec()
    generator_limits: ProcessLimits = ProcessLimits()
    bencher: str = "oha"
    repeats: int = 1
    warmup_requests: int = 0
    attempt_timeout: float = 30.0
    ready_timeout: float = 5.0
    budget: float = 120.0
    budget_file: Path | None = None
    workload_files: Mapping[str, Path] = field(default_factory=dict)
    target_settings: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:

        if not isinstance(self.generator_limits, ProcessLimits):
            raise ValueError("generator_limits must be a ProcessLimits instance")

        if self.budget_file is not None and not isinstance(self.budget_file, Path):
            raise ValueError("budget_file must be a Path or None")

        if not isinstance(self.workload_files, Mapping) or len(self.workload_files) > 4096 or any(
            not isinstance(name, str) or not name or not isinstance(path, Path) for name, path in self.workload_files.items()
        ):
            raise ValueError("workload_files must be a mapping of bounded artifact names to Path objects")

        if not isinstance(self.target_settings, Mapping):
            raise ValueError("target_settings must be a nonsecret settings mapping")

        object.__setattr__(self, "workload_files", MappingProxyType(dict(self.workload_files)))
        object.__setattr__(self, "target_settings", MappingProxyType(normalize_json(self.target_settings)))

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
            "generator_limits": self.generator_limits.serialize(),
            "budget_file": f"{self.budget_file}" if self.budget_file is not None else None,
            "workload_files": {name: f"{path}" for name, path in self.workload_files.items()},
            "target_settings": dict(self.target_settings),
            **{name: getattr(self, name) for name in (
                "bencher", "repeats", "warmup_requests", "attempt_timeout", "ready_timeout", "budget",
            )},
        }

class Runner:
    """One executor for external URLs, foreground commands and selected built-in stands."""

    def __init__(self, options: RunOptions) -> None:
        self.options = options

    @staticmethod
    def run_series(
        store: RunStore, *, identity: dict, conditions: dict, attempt_timeout: float,
        remaining: Callable[[], float], measure: Callable[[LoadSpec, str, int, float], SampleResult],
        check_running: Callable[[], None] | None = None,
    ) -> None:
        """Run prepared HTTP phases without acquiring a second target or budget.

        The owner prepares inputs and conditions, owns target cleanup and supplies
        the remaining work time after its cleanup reserve. measure executes one
        attempt; this runner alone selects phases/repeats and checkpoints them.
        """
        conditions = read_conditions(conditions)

        try:
            loads = tuple(LoadSpec(**load) for load in conditions["workload"]["loads"])
            repeats = conditions["policy"]["repeats"]
            warmup_requests = conditions["policy"]["warmup_requests"]

        except (KeyError, TypeError) as error:
            raise ValueError("Prepared series requires loads and a repeat/warmup policy") from error

        if not loads or len({load.clients for load in loads}) != len(loads):
            raise ValueError("Prepared series requires distinct concurrency points")

        if type(repeats) is not int or repeats < 1 or type(warmup_requests) is not int or warmup_requests < 0:
            raise ValueError("Invalid prepared series repeat/warmup policy")

        if type(attempt_timeout) not in (int, float) or not math.isfinite(attempt_timeout) or attempt_timeout <= 0:
            raise ValueError("attempt_timeout must be finite and positive")

        if any(load.duration is not None and load.duration + load.timeout + 1 > attempt_timeout for load in loads):
            raise ValueError("attempt_timeout must cover duration, request timeout and reporting")

        def take_sample(load: LoadSpec, phase: str, repeat: int) -> None:
            available = remaining()

            if type(available) not in (int, float) or not math.isfinite(available):
                raise ValueError("Remaining series work time must be finite")

            if available <= 0:
                raise NarrowException("Run budget exhausted (cleanup time reserved)")

            if load.duration is not None and available < load.duration + load.timeout + 1:
                raise NarrowException("Insufficient run budget for the requested duration and drain")

            sample = measure(load, phase, repeat, min(attempt_timeout, available))

            try:
                store.save_attempt(sample, {
                    **identity, "phase": phase, "repeat": repeat, "load": asdict(load),
                    "date": datetime.now(UTC).isoformat(), "conditions_fingerprint": conditions["fingerprint"],
                })

            except OSError as error:

                if sample.status != "cancelled":
                    raise

                interrupted = KeyboardInterrupt("HTTP generator was interrupted; stop the whole series")
                interrupted.add_note("Cancelled attempt checkpoint could not be saved")
                raise interrupted from error

            if sample.status == "cancelled":
                raise KeyboardInterrupt("HTTP generator was interrupted; stop the whole series")

            if sample.cleanup_error:
                raise NarrowException(f"Generator cleanup failed: {sample.cleanup_error}")

            if phase == "warmup" and sample.status != "ok":
                raise NarrowException("Warmup failed; measurement not started")

            if sample.status in ("error", "invalid"):
                raise NarrowException(sample.reason or "Generator attempt did not complete")

            if check_running is not None:
                check_running()

        for load in loads:

            if warmup_requests:
                take_sample(replace(load, requests=warmup_requests, duration=None, drain=False), "warmup", 0)

            for repeat in range(1, repeats + 1):
                take_sample(load, "measurement", repeat)

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
            controller = collect_controller_identity()

            try:
                machine = collect_machine(read_cpu_roles(options.generator_limits))

            except (OSError, ValueError, AttributeError) as error:
                machine = {"unavailable": {"machine": type(error).__name__}}
                LOG.warning("Machine identity is unavailable; strict comparison will refuse this run")

            store.data["meta"]["measurement"] = {"controller": controller, "machine": machine}
            explicit_artifacts = hash_artifacts(options.workload_files) if options.workload_files else {}
            builtin_artifacts = hash_payload(Path(__file__).parent / "apps", prefix="narrow/apps") if options.stands else {}
            prepared_cases = []

            for index, target in enumerate(targets):
                request = base_request
                inputs = input_metadata

                if request.ca_file is None and target.ca_file is not None:
                    directory = store.path / "inputs" / f"target-{index + 1:04d}"
                    ca_request, ca_metadata = snapshot_request(RequestSpec(ca_file=target.ca_file), directory)
                    request = replace(base_request, ca_file=ca_request.ca_file)
                    inputs = {**input_metadata, "ca_file": {
                        **ca_metadata["ca_file"], "artifact": f"inputs/{directory.name}/ca.pem",
                    }}

                workload = build_http_workload(
                    target.url, request, inputs, options.loads,
                    explicit_artifacts or (builtin_artifacts if isinstance(target, StandTarget) else {}),
                )
                settings = dict(options.target_settings)

                if isinstance(target, StandTarget):
                    workload["builtin_application"] = {"name": target.app_alias, **app_info.get(target.app_alias, {})}
                    actual = store.data["meta"]["server_settings"]

                    if any(name in actual and actual[name] != value for name, value in settings.items()):
                        raise ValueError("Declared target_settings conflict with actual built-in settings")

                    settings = {**settings, **actual}

                conditions = MeasurementConditions(
                    workload=workload,
                    tools={"controller": controller, "generator": {
                        "name": options.bencher, "version": generator["version"], "sha256": generator["sha256"],
                    }},
                    environment={
                        "machine": machine, "generator_limits": options.generator_limits.serialize(),
                        "host_limits": store.data["meta"]["environment"],
                    },
                    policy={
                        "repeats": options.repeats, "warmup_requests": options.warmup_requests,
                        "attempt_timeout": options.attempt_timeout, "ready_timeout": options.ready_timeout,
                        "target_lifecycle": target.ownership, "target_settings": settings,
                    },
                ).serialize()
                prepared_cases.append((request, conditions))

            store.data["meta"]["measurement"]["conditions"] = [conditions for request, conditions in prepared_cases]
            store.checkpoint()
            budget = Budget(options.budget_file or store.path / "budget.json", options.budget).open()
            store.data["meta"]["budget"] = budget.data.copy()
            store.checkpoint()

            if estimate > options.budget:
                LOG.warning("Worst-case run time %.1fs exceeds budget %.1fs; the run may stop early", estimate, options.budget)

            for target, (request, conditions) in zip(targets, prepared_cases, strict=True):
                identity = {
                    "stand": target.alias, "app": target.app_alias, "session": len(store.data["stands"]),
                    "subject": target.describe().get("subject"),
                }

                def measure(load: LoadSpec, phase: str, repeat: int, timeout: float) -> SampleResult:
                    return bencher.run_once(
                        target.url, load, request=request, timeout=timeout, limits=options.generator_limits,
                    )

                observations = {"scope": "target_lifecycle_including_readiness_warmup_and_cleanup", "before": observe_host()}

                try:

                    if budget.remaining() <= 2 * options.ready_timeout + CLEANUP_RESERVE:
                        raise NarrowException("Insufficient run budget for readiness and cleanup")

                    with target.setup(timeout=options.ready_timeout, ca_file=request.ca_file):
                        self.run_series(
                            store, identity=identity, conditions=conditions,
                            attempt_timeout=options.attempt_timeout,
                            remaining=lambda: budget.remaining() - CLEANUP_RESERVE,
                            measure=measure, check_running=target.check_running,
                        )

                finally:
                    primary = sys.exception()

                    try:
                        observations["after"] = observe_host()
                        store.save_target(target, {
                            **identity, "conditions": conditions, "host_observations": observations,
                        }, request=request)

                    except OSError as error:

                        if primary is None:
                            raise

                        primary.add_note(f"Could not save target evidence: {error}")
                        LOG.error("Could not save target evidence: %s", error)

                if any(item["cleanup_error"] for item in store.data["stands"][-1]["processes"]):
                    raise NarrowException("Stand cleanup failed; further workloads will not run")

                if any(item["output_truncated"] for item in store.data["stands"][-1]["processes"]):
                    raise NarrowException("Stand output limit exceeded; captured diagnostics are truncated")

            remaining = budget.remaining()
            store.data["meta"]["budget_elapsed_including_cleanup_seconds"] = options.budget - remaining

            if remaining < 0:
                raise NarrowException("Run budget exhausted during cleanup")

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
