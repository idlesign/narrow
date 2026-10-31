"""Sequential narrow runs: explicit workloads, bounded phases, durable evidence."""

import hashlib
import logging
import math
import platform
import shutil
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from time import monotonic

from .apps import APPS
from .artifacts import RunStore
from .benchers import BENCHERS
from .configuration import LoadSpec, Settings, bootstrap
from .exceptions import NarrowException
from .stands import STANDS

LOG = logging.getLogger(__name__)
CLEANUP_RESERVE = 15.0


@dataclass(frozen=True)
class RunOptions:
    """An explicit matrix; no implicit all-stands or all-frameworks benchmark."""

    stands: tuple[str, ...]
    loads: tuple[LoadSpec, ...]
    apps: tuple[str, ...] = ("py",)
    bencher: str = "oha"
    repeats: int = 1
    warmup_requests: int = 0
    attempt_timeout: float = 30.0
    ready_timeout: float = 5.0
    budget: float = 120.0

    def __post_init__(self) -> None:

        if not self.stands or not self.loads or not self.apps:
            raise ValueError("Select at least one stand, workload and application")

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


class Runner:
    """Run only the selected local topologies, without installing any runtime tools."""

    def __init__(self, options: RunOptions) -> None:
        self.options = options

    def run(self, output: Path) -> dict:
        """Preflight all combinations before creating output; checkpoint every attempt."""
        options = self.options

        for selected, registry, title in (
            (options.stands, STANDS, "stand"), (options.apps, APPS, "application"), ((options.bencher,), BENCHERS, "bencher"),
        ):

            for alias in selected:

                if alias not in registry:
                    raise NarrowException(f"Unknown {title}: {alias}")

        bencher = BENCHERS[options.bencher]()
        tools = {}
        app_info = {}
        combinations = []

        for alias in options.stands:
            stand = STANDS[alias]()

            for load in options.loads:
                bencher.validate_load(f"{stand.protocol}://{stand.address}/", load)

            stand.check_tools()
            current = stand

            while current is not None:
                name = current.prc_name

                if name not in tools:
                    binary = Path(shutil.which(name)).resolve()
                    tools[name] = {
                        "path": f"{binary}", "version": current.get_version(),
                        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    }

                current = current.child

            for app_alias in options.apps if stand.uses_app else (None,):

                if app_alias is not None and app_alias not in app_info:
                    app = APPS[app_alias]()

                    try:
                        version = app.get_version()

                    except PackageNotFoundError as error:
                        raise NarrowException(f"Application {app_alias} is not installed") from error

                    entrypoint = Path(app.get_entrypoint_path())
                    app_info[app_alias] = {"version": version, "entrypoint_sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest()}

                combinations.append((alias, app_alias))

        generator = bencher.prepare()
        estimate = len(combinations) * (
            2 * options.ready_timeout + CLEANUP_RESERVE
            + len(options.loads) * (options.repeats + bool(options.warmup_requests)) * options.attempt_timeout
        )
        store = RunStore(Path(output), {
            "date": datetime.now(UTC).isoformat(), "python": sys.version, "platform": platform.platform(),
            "options": asdict(options), "generator": generator, "servers": tools, "apps": app_info,
            "environment": bootstrap(), "upper_time_estimate_seconds": estimate,
            "server_settings": {
                "workers": Settings.WORKERS, "threads": Settings.THREADS,
                "max_connections": Settings.MAX_CONNECTIONS, "access_logs": Settings.LOG_WRITE,
                "listen_backlog": Settings.LISTEN_BACKLOG,
            },
            "legacy_timeout_policy": "process watchdog only" if options.bencher != "oha" else None,
        })
        store.prepare()
        deadline = monotonic() + options.budget

        if estimate > options.budget:
            LOG.warning("Worst-case run time %.1fs exceeds budget %.1fs; the run may stop early", estimate, options.budget)

        try:

            for alias, app_alias in combinations:
                stand = STANDS[alias]()
                app = APPS[app_alias]() if app_alias is not None else None
                identity = {"stand": alias, "app": app_alias, "session": len(store.data["stands"])}
                current = stand

                while current is not None:
                    current.prc_name = tools[current.prc_name]["path"]
                    current = current.child

                def take_sample(load: LoadSpec, phase: str, repeat: int) -> None:
                    remaining = deadline - monotonic() - CLEANUP_RESERVE

                    if remaining <= 0:
                        raise NarrowException("Run budget exhausted (cleanup time reserved)")

                    if load.duration is not None and remaining < load.duration + load.timeout + 1:
                        raise NarrowException("Insufficient run budget for the requested duration and drain")

                    sample = bencher.run_once(
                        f"{stand.protocol}://{stand.address}/", load, timeout=min(options.attempt_timeout, remaining),
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

                    stand.check_running()

                try:

                    if deadline - monotonic() <= 2 * options.ready_timeout + CLEANUP_RESERVE:
                        raise NarrowException("Insufficient run budget for readiness and cleanup")

                    with stand.setup(app, timeout=options.ready_timeout):

                        for load in options.loads:

                            if options.warmup_requests:
                                take_sample(replace(load, requests=options.warmup_requests, duration=None), "warmup", 0)

                            for repeat in range(1, options.repeats + 1):
                                take_sample(load, "measurement", repeat)

                finally:
                    primary = sys.exception()

                    try:
                        store.save_stand(stand, identity)

                    except OSError as error:

                        if primary is None:
                            raise

                        primary.add_note(f"Could not save stand evidence: {error}")
                        LOG.error("Could not save stand evidence: %s", error)

                if any(item["cleanup_error"] for item in store.data["stands"][-1]["processes"]):
                    raise NarrowException("Stand cleanup failed; further workloads will not run")

                if any(item["output_truncated"] for item in store.data["stands"][-1]["processes"]):
                    raise NarrowException("Stand output limit exceeded; captured diagnostics are truncated")

            store.data["status"] = "complete" if all(
                attempt["status"] == "ok" for attempt in store.data["attempts"]
            ) else "failed"

        except KeyboardInterrupt:
            store.data.update(status="cancelled", reason="Interrupted by user")
            raise

        except (OSError, NarrowException) as error:
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
