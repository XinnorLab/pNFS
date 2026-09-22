# SPDX-License-Identifier: MIT
"""The connector runtime: instances, bounded workers, freshness, hold-down,
epochs/sequences and the published batch (CON-04, CON-10..16, CON-21..23).

Threading model: one worker thread per instance runs the collect/evaluate
cycle on a monotonic schedule; each collect executes in a helper thread the
worker joins with the deadline, so a hanging module cannot stack collects
(at most one in flight) and cannot block other instances. A worker that is
stuck past the deadline is abandoned (the helper is a daemon thread) and
restarted, up to ``worker_restart_limit`` times per hour; after that the
instance publishes UNKNOWN / WORKER_STUCK until a reload.

Publication is atomic: a worker builds a complete immutable
:class:`InstanceSnapshot` and swaps it in under a lock; a reader never sees
records from two cycles. ``GET /v1/assessments`` only reads snapshots and
does clock arithmetic — no network, no module call (LAT-04).
"""

from __future__ import annotations

import json
import os
import random
import socket
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import contract
from .config import Binding, Config, Instance, Profile
from .log import Logger
from .modules import create_module
from .modules.base import Assessment, CollectionError, Module, SourceBatch, unknown_assessment
from .modules.fixture import fixture_profile


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class PublishedAssessment:
    """One normalized assessment as published, with the facts the reader needs
    to age it: when it was fetched and the profile's max source age."""

    assessment: Assessment
    fetched_mono: float
    source_max_age_ms: int
    profile_id: str
    profile_version: str
    profile_digest: str

    def render(self, now_mono: float) -> Dict[str, Any]:
        a = self.assessment
        age: Optional[int]
        if a.evidence_age_ms is None:
            age = None
            ttl = 0
        else:
            age = a.evidence_age_ms + max(0, int((now_mono - self.fetched_mono) * 1000))
            ttl = max(0, min(contract.MAX_REMAINING_TTL_MS, self.source_max_age_ms - age))
        quality, allowed, ppm, reasons = a.quality, a.allowed, a.multiplier_ppm, list(a.reason_codes)
        if ttl == 0:
            if allowed or quality == contract.QUALITY_VALID:
                if "EVIDENCE_EXPIRED" not in reasons:
                    reasons = (["EVIDENCE_EXPIRED"] + reasons)[: contract.MAX_REASON_CODES]
            quality, allowed, ppm = contract.QUALITY_UNKNOWN, False, 0
        diagnostics = _bounded_diagnostics(a.diagnostics)
        return {
            "ds_id": a.ds_id,
            "binding_generation": a.binding_generation,
            "datastore_id": a.datastore_id,
            "target_id": a.target_id,
            "target_incarnation": a.target_incarnation,
            "endpoint": None,  # filled by the instance (binding endpoint)
            "scope": contract.SCOPE_NEW_ALLOCATION,
            "access_scope_id": contract.ACCESS_SCOPE_CLUSTER_DEFAULT,
            "quality": quality,
            "observed_at": a.observed_at,
            "evidence_age_ms": age,
            "remaining_ttl_ms": ttl,
            "profile": {"id": self.profile_id, "version": self.profile_version, "digest": self.profile_digest},
            "placement": {"allowed": allowed, "multiplier_ppm": ppm, "reason_codes": reasons},
            "resources": {
                "capacity_domain_id": a.capacity_domain_id,
                "shared_resource_ids": list(a.shared_resource_ids),
            },
            "coverage": list(a.coverage),
            "diagnostics": diagnostics,
        }


def _bounded_diagnostics(diag: Dict[str, Any]) -> Dict[str, Any]:
    text = json.dumps(diag, sort_keys=True, default=str)
    if len(text.encode("utf-8")) <= contract.MAX_DIAGNOSTICS_BYTES:
        return diag
    # Never silently truncated: the marker says the detail was dropped and how big it was.
    return {"truncated": True, "bytes": len(text.encode("utf-8")), "limit": contract.MAX_DIAGNOSTICS_BYTES}


@dataclass(frozen=True)
class InstanceSnapshot:
    epoch: str
    sequence: int
    snapshot_status: str
    assessments: Tuple[PublishedAssessment, ...]
    published_mono: float
    last_error: Optional[str] = None
    last_error_mono: Optional[float] = None
    last_success_mono: Optional[float] = None
    source_cycle: Optional[str] = None


class HoldDownTracker:
    """Re-entry hold-down per (ds_id, binding_generation) (CON-16)."""

    @dataclass
    class _State:
        start_mono: Optional[float] = None
        cycles: set = field(default_factory=set)
        lease_expiry_mono: Optional[float] = None

    def __init__(self) -> None:
        self._states: Dict[Tuple[int, int], HoldDownTracker._State] = {}

    def apply(self, a: Assessment, cycle_key: str, now_mono: float, fetched_mono: float, profile: Profile) -> Assessment:
        key = (a.ds_id, a.binding_generation)
        st = self._states.setdefault(key, HoldDownTracker._State())
        if not a.allowed:
            st.start_mono = None
            st.cycles = set()
            st.lease_expiry_mono = None
            return a
        # The previous allow lease expired before this cycle: start over.
        if st.lease_expiry_mono is not None and now_mono > st.lease_expiry_mono:
            st.start_mono = None
            st.cycles = set()
        if st.start_mono is None:
            st.start_mono = now_mono
            st.cycles = {cycle_key}
        else:
            st.cycles.add(cycle_key)
        remaining_ms = profile.source_max_age_ms - (a.evidence_age_ms or 0)
        st.lease_expiry_mono = fetched_mono + max(0, remaining_ms) / 1000.0
        elapsed_ms = int((now_mono - st.start_mono) * 1000)
        satisfied = len(st.cycles) >= profile.recovery_distinct_cycles and elapsed_ms >= profile.recovery_hold_down_ms
        diag = dict(a.diagnostics)
        diag["hold_down"] = {
            "completed": satisfied,
            "elapsed_ms": elapsed_ms,
            "distinct_cycles": len(st.cycles),
            "required_ms": profile.recovery_hold_down_ms,
            "required_cycles": profile.recovery_distinct_cycles,
        }
        if satisfied:
            return replace(a, diagnostics=diag)
        diag["hold_down"]["withheld_reason_codes"] = list(a.reason_codes)
        return replace(
            a,
            allowed=False,
            multiplier_ppm=0,
            reason_codes=(["RECOVERY_HOLD_DOWN"] + [r for r in a.reason_codes if r != "NORMAL"])[: contract.MAX_REASON_CODES],
            diagnostics=diag,
        )

    def forget(self, key: Tuple[int, int]) -> None:
        self._states.pop(key, None)


class InstanceRuntime:
    """One configured instance: its module, worker and published snapshot."""

    def __init__(
        self,
        instance: Instance,
        profile: Optional[Profile],
        runtime_cfg,
        runtime_epoch: str,
        logger: Logger,
        module: Optional[Module] = None,
        clock=time.monotonic,
        sleeper=None,
        jitter=None,
        incarnation: int = 0,
    ):
        self.instance = instance
        self.profile = profile
        self.cfg = runtime_cfg
        self.log = logger
        self._clock = clock
        self._sleep = sleeper or (lambda s: threading.Event().wait(s))
        self._jitter = jitter or (lambda: random.uniform(0, contract.MAX_JITTER_MS / 1000.0))
        self.module: Module = module or create_module(instance)
        self._restarts = 0
        # Epoch: instance id, runtime epoch, the instance's incarnation in this
        # runtime (bumped by every reload that recreates it) and the worker
        # restart count — any of them changing invalidates old sequences (CON-14).
        self._epoch_base = f"{instance.id}:{runtime_epoch}:{incarnation}"
        self.epoch = f"{self._epoch_base}:{self._restarts}"
        self._lock = threading.Lock()
        self._sequence = 0
        self._hold = HoldDownTracker()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._collect_generation = 0
        self._stuck = False
        self._restart_times: List[float] = []
        self._backoff_s = 0.0
        self._failures = 0
        self.snapshot: InstanceSnapshot = self._unknown_snapshot("NO_ASSESSMENT", contract.SNAPSHOT_FAILED, publish=False)

    # --- helpers ------------------------------------------------------------

    def _profile_triplet(self, batch: Optional[SourceBatch] = None) -> Tuple[str, str, str, int]:
        if self.profile is not None:
            return self.profile.id, self.profile.version, self.profile.digest, self.profile.source_max_age_ms
        doc_profile = None
        if batch is not None and isinstance(batch.payload, dict):
            doc_profile = batch.payload.get("profile")
        p = fixture_profile(doc_profile if isinstance(doc_profile, dict) else None)
        return p.id, p.version, p.digest, p.source_max_age_ms

    def _datastore_id(self, b: Binding) -> str:
        return self.instance.expected_controller_id or b.datastore_id or self.instance.id

    def _unknown_snapshot(self, code: str, status: str, publish: bool = True, error: Optional[str] = None) -> InstanceSnapshot:
        now = self._clock()
        pid, pver, pdig, max_age = self._profile_triplet()
        records = tuple(
            PublishedAssessment(
                assessment=unknown_assessment(b, self._datastore_id(b), code),
                fetched_mono=now,
                source_max_age_ms=max_age,
                profile_id=pid,
                profile_version=pver,
                profile_digest=pdig,
            )
            for b in self.instance.bindings
        )
        for b in self.instance.bindings:
            self._hold.forget((b.ds_id, b.binding_generation))
        snap = InstanceSnapshot(
            epoch=self.epoch,
            sequence=self._sequence + 1 if publish else 0,
            snapshot_status=status,
            assessments=records,
            published_mono=now,
            last_error=error or code,
            last_error_mono=now,
            last_success_mono=self.snapshot.last_success_mono if hasattr(self, "snapshot") else None,
            source_cycle=None,
        )
        if publish:
            self._publish(snap)
        return snap

    def _publish(self, snap: InstanceSnapshot) -> None:
        with self._lock:
            self._sequence = snap.sequence
            self.snapshot = snap

    # --- the cycle ----------------------------------------------------------

    def run_cycle(self) -> None:
        """One collect → evaluate → publish. Never raises."""
        deadline_s = self.cfg.collect_deadline_ms / 1000.0
        started = self._clock()
        try:
            batch = self._collect_bounded(deadline_s)
        except CollectionError as exc:
            self._on_collection_error(exc)
            return
        except Exception as exc:  # a module bug must not kill the runtime (CON-04)
            self._on_collection_error(CollectionError("MODULE_ERROR", f"{exc.__class__.__name__}: {exc}", retryable=False))
            return
        try:
            raw = self.module.evaluate(batch, self.profile, self.instance.bindings)
        except CollectionError as exc:
            self._on_collection_error(exc)
            return
        except Exception as exc:
            self._on_collection_error(CollectionError("MODULE_ERROR", f"evaluate: {exc.__class__.__name__}: {exc}", retryable=False))
            return
        now = self._clock()
        pid, pver, pdig, max_age = self._profile_triplet(batch)
        by_ds = {a.ds_id: a for a in raw}
        profile = self.profile or fixture_profile()
        records: List[PublishedAssessment] = []
        for b in self.instance.bindings:
            a = by_ds.get(b.ds_id)
            if a is None or a.binding_generation != b.binding_generation:
                a = unknown_assessment(b, self._datastore_id(b), "NO_ASSESSMENT")
            a = a.normalized()
            a = self._hold.apply(a, batch.cycle_key, now, batch.fetched_mono, profile)
            records.append(
                PublishedAssessment(
                    assessment=a,
                    fetched_mono=batch.fetched_mono,
                    source_max_age_ms=max_age,
                    profile_id=pid,
                    profile_version=pver,
                    profile_digest=pdig,
                )
            )
        self._failures = 0
        self._backoff_s = 0.0
        snap = InstanceSnapshot(
            epoch=self.epoch,
            sequence=self._sequence + 1,
            snapshot_status=batch.snapshot_status,
            assessments=tuple(records),
            published_mono=now,
            last_error=None,
            last_error_mono=self.snapshot.last_error_mono,
            last_success_mono=now,
            source_cycle=batch.cycle_key,
        )
        self._publish(snap)
        self.log.counters.inc("connector_collect_total", {"instance": self.instance.id, "result": "ok"})
        self.log.info(
            "collect_ok",
            instance=self.instance.id,
            sequence=snap.sequence,
            source_cycle=batch.cycle_key,
            snapshot_status=batch.snapshot_status,
            cycle_ms=int((now - started) * 1000),
            allowed=sum(1 for r in records if r.assessment.allowed),
            bound=len(records),
        )

    def _collect_bounded(self, deadline_s: float) -> SourceBatch:
        """Run module.collect in a helper thread; give up at the deadline."""
        self._collect_generation += 1
        generation = self._collect_generation
        result: Dict[str, Any] = {}
        done = threading.Event()

        def target() -> None:
            try:
                result["batch"] = self.module.collect(deadline_s)
            except BaseException as exc:  # noqa: BLE001 - forwarded as a typed error
                result["error"] = exc
            finally:
                done.set()

        t = threading.Thread(target=target, name=f"collect-{self.instance.id}-{generation}", daemon=True)
        t.start()
        if not done.wait(deadline_s + 0.5):
            self._stuck = True
            self.log.counters.inc("connector_worker_stuck_total", {"instance": self.instance.id})
            raise CollectionError("SOURCE_TIMEOUT", "collect did not return by the deadline (worker abandoned)", retryable=True, details={"stuck": True})
        self._stuck = False
        if "error" in result:
            err = result["error"]
            if isinstance(err, CollectionError):
                raise err
            raise CollectionError("MODULE_ERROR", f"{err.__class__.__name__}: {err}", retryable=False)
        return result["batch"]

    def _on_collection_error(self, exc: CollectionError) -> None:
        self._failures += 1
        self._backoff_s = min(contract.MAX_RETRY_BACKOFF_MS / 1000.0, (2 ** min(self._failures, 3)) * 0.5)
        self.log.counters.inc("connector_poll_errors_total", {"instance": self.instance.id, "code": exc.code})
        now = self._clock()
        if exc.retryable:
            # Transport-style failure: the last VALID decisions stay until their
            # own expiry (CON-12); nothing is refreshed.
            with self._lock:
                self.snapshot = replace(self.snapshot, last_error=exc.code, last_error_mono=now)
            self.log.limited("warn", "collect_failed_retained", f"{self.instance.id}:{exc.code}", instance=self.instance.id, code=exc.code, message=str(exc), retained=True)
            return
        # Auth / schema / explicit source verdict: revoke immediately and alert.
        self._unknown_snapshot(exc.code, contract.SNAPSHOT_FAILED, error=exc.code)
        self.log.limited("error", "collect_failed_revoked", f"{self.instance.id}:{exc.code}", instance=self.instance.id, code=exc.code, message=str(exc), retained=False, alert=True)

    # --- worker thread ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"worker-{self.instance.id}", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout_s)
        self._thread = None
        try:
            self.module.close()
        except Exception as exc:  # pragma: no cover
            self.log.warn("module_close_failed", instance=self.instance.id, error=str(exc))

    def _loop(self) -> None:
        interval = self.cfg.collect_interval_ms / 1000.0
        next_due = self._clock()
        while not self._stop.is_set():
            now = self._clock()
            if now < next_due:
                self._stop.wait(min(next_due - now, 1.0))
                continue
            if self._stuck_exhausted():
                self._stop.wait(1.0)
                continue
            self.run_cycle()
            if self._stuck:
                self._note_restart()
            delay = interval + self._jitter()
            if self._backoff_s > 0:
                delay = min(delay, self._backoff_s + self._jitter())
            next_due = self._clock() + delay

    def _note_restart(self) -> None:
        now = self._clock()
        self._restart_times = [t for t in self._restart_times if now - t < 3600.0] + [now]
        self._restarts += 1
        self.epoch = f"{self._epoch_base}:{self._restarts}"
        self.log.warn("worker_restarted", instance=self.instance.id, restarts_last_hour=len(self._restart_times))

    def _stuck_exhausted(self) -> bool:
        if len(self._restart_times) <= self.cfg.worker_restart_limit:
            return False
        if self.snapshot.last_error != "WORKER_STUCK":
            self._unknown_snapshot("WORKER_STUCK", contract.SNAPSHOT_FAILED, error="WORKER_STUCK")
            self.log.error("worker_restart_limit", instance=self.instance.id, limit=self.cfg.worker_restart_limit)
        return True

    # --- rendering ----------------------------------------------------------

    def render(self, now_mono: float) -> Dict[str, Any]:
        snap = self.snapshot
        endpoints = {b.ds_id: b.endpoint.as_dict() for b in self.instance.bindings}
        records = []
        for pa in snap.assessments:
            rec = pa.render(now_mono)
            rec["endpoint"] = endpoints.get(rec["ds_id"], {})
            records.append(rec)
        return {
            "connector_instance_id": self.instance.id,
            "module_type": self.instance.module,
            "epoch": snap.epoch,
            "sequence": snap.sequence,
            "snapshot_status": snap.snapshot_status,
            "assessments": records,
        }

    def health(self, now_mono: float) -> Dict[str, Any]:
        snap = self.snapshot
        return {
            "module": self.instance.module,
            "epoch": snap.epoch,
            "sequence": snap.sequence,
            "snapshot_status": snap.snapshot_status,
            "last_error": snap.last_error,
            "last_success_age_ms": None if snap.last_success_mono is None else int((now_mono - snap.last_success_mono) * 1000),
            "stuck": self._stuck,
            "restarts_last_hour": len(self._restart_times),
            "bound_ds": len(self.instance.bindings),
        }


class BatchTooLarge(Exception):
    def __init__(self, size: int, limit: int):
        super().__init__(f"batch is {size} bytes, limit {limit}")
        self.size = size
        self.limit = limit


class Runtime:
    """All instances of one configuration; owns the epoch and the batch."""

    def __init__(self, config: Config, logger: Optional[Logger] = None, clock=time.monotonic, module_factory=None):
        self.log = logger or Logger()
        self._clock = clock
        self._module_factory = module_factory
        self.started_at = utc_now()
        self.runtime_epoch = f"{socket.gethostname()}:{self.started_at}:{os.getpid()}"
        self._lock = threading.Lock()
        self.config = config
        self.instances: Dict[str, InstanceRuntime] = {}
        self._incarnations: Dict[str, int] = {}
        self._build_instances(config)
        self._running = False

    def _make(self, inst: Instance, config: Config) -> InstanceRuntime:
        module = self._module_factory(inst) if self._module_factory else None
        incarnation = self._incarnations.get(inst.id, -1) + 1
        self._incarnations[inst.id] = incarnation
        return InstanceRuntime(
            inst,
            config.profile_for(inst),
            config.runtime,
            self.runtime_epoch,
            self.log,
            module=module,
            clock=self._clock,
            incarnation=incarnation,
        )

    def _build_instances(self, config: Config) -> None:
        for inst in config.instances:
            self.instances[inst.id] = self._make(inst, config)

    def start(self) -> None:
        with self._lock:
            self._running = True
            for ir in self.instances.values():
                ir.start()
        self.log.info("runtime_started", runtime_epoch=self.runtime_epoch, config_digest=self.config.digest, instances=len(self.instances))

    def stop(self, timeout_s: float = 5.0) -> None:
        deadline = self._clock() + timeout_s
        with self._lock:
            self._running = False
            for ir in self.instances.values():
                ir.stop(max(0.1, deadline - self._clock()))
        self.log.info("runtime_stopped")

    def reload(self, config: Config) -> None:
        """Atomic switch to a validated configuration (CON-19).

        Unchanged instances keep their epoch, sequence and hold-down state;
        a changed or new instance gets a fresh runtime (new epoch, UNKNOWN
        until its first collect); a removed instance is stopped.
        """
        with self._lock:
            old = self.instances
            new: Dict[str, InstanceRuntime] = {}
            for inst in config.instances:
                prev = old.get(inst.id)
                if prev is not None and prev.instance == inst and prev.profile == config.profile_for(inst) and prev.cfg == config.runtime:
                    new[inst.id] = prev
                    continue
                ir = self._make(inst, config)
                if self._running:
                    ir.start()
                new[inst.id] = ir
                if prev is not None:
                    prev.stop(2.0)
            for iid, prev in old.items():
                if iid not in new:
                    prev.stop(2.0)
            self.instances = new
            self.config = config
        self.log.info("config_reloaded", config_digest=config.digest, instances=len(self.instances))

    # --- outputs ------------------------------------------------------------

    def batch(self, now_mono: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if now_mono is None else now_mono
        with self._lock:
            instances = list(self.instances.values())
            digest = self.config.digest
        return {
            "contract_version": contract.CONTRACT_VERSION,
            "runtime_epoch": self.runtime_epoch,
            "config_digest": digest,
            "generated_at": utc_now(),
            "instances": [ir.render(now) for ir in instances],
        }

    def batch_bytes(self) -> bytes:
        data = json.dumps(self.batch(), separators=(",", ":"), sort_keys=False).encode("utf-8")
        if len(data) > self.config.runtime.max_batch_bytes:
            raise BatchTooLarge(len(data), self.config.runtime.max_batch_bytes)
        return data

    def health(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            instances = {iid: ir.health(now) for iid, ir in self.instances.items()}
        # Readiness is about the cache (LAT-04): every instance has published
        # at least one real collection. Whether workers run is reported apart.
        ready = bool(instances) and all(i["last_success_age_ms"] is not None for i in instances.values())
        return {
            "ready": ready,
            "running": self._running,
            "runtime_epoch": self.runtime_epoch,
            "config_digest": self.config.digest,
            "started_at": self.started_at,
            "instances": instances,
        }

    def metrics_text(self) -> str:
        now = self._clock()
        lines: List[str] = []
        for name, series in sorted(self.log.counters.snapshot().items()):
            lines.append(f"# TYPE {name} counter")
            for labels, value in sorted(series.items()):
                label_text = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"{name}{{{label_text}}} {value}")
        lines.append("# TYPE connector_assessment_age_ms gauge")
        lines.append("# TYPE connector_assessment_allowed gauge")
        with self._lock:
            instances = list(self.instances.values())
        for ir in instances:
            for rec in ir.render(now)["assessments"]:
                labels = f'instance="{ir.instance.id}",ds_id="{rec["ds_id"]}"'
                age = rec["evidence_age_ms"]
                lines.append(f"connector_assessment_age_ms{{{labels}}} {age if age is not None else -1}")
                lines.append(f"connector_assessment_allowed{{{labels}}} {1 if rec['placement']['allowed'] else 0}")
        return "\n".join(lines) + "\n"
