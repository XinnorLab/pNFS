# SPDX-License-Identifier: MIT
"""The connector runtime: instances, bounded workers, freshness, hold-down,
epochs/sequences and the published batch (CON-04, CON-10..16, CON-21..23).

Threading model: one worker thread per instance runs the collect/evaluate
cycle on a monotonic schedule; each collect executes in a helper thread the
worker joins with the deadline, so a hanging module cannot stack collects
(at most one in flight) and cannot block other instances. A worker that is
stuck past the deadline is abandoned (the helper is a daemon thread) and
restarted, up to ``worker_restart_limit`` times per hour; after that the
instance publishes WORKER_STUCK (retained verdicts or UNKNOWN) until a reload.

Verdict retention (smart verdict retention design §3, §4.2, §4.3): the last
VALID record of every binding lives in the runtime's :class:`VerdictStore`
and stays in force for its hold (``critical_hold_ms`` for a deny,
``verdict_hold_ms`` otherwise), counted from its observation. A newer VALID
record replaces it; no new data -- an UNKNOWN record or a collection error of
any kind -- never does: the stored verdict is re-published with
``VERDICT_RETAINED`` until its hold runs out, then the binding reads UNKNOWN.

The state file (design §4.4, :mod:`.state`): the runtime writes the store
after the cycles that change it, at most once per ``collect_interval_ms``, and
once more on stop; at start it restores the verdicts still in force before
the first publication (``VERDICT_RETAINED``, ``RESTORED_FROM_STATE``). A bad
or unwritable file costs persistence, never the start or a cycle.

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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import contract, state
from .config import Binding, Config, Instance, Profile
from .log import Logger
from .modules import create_module
from .modules.base import Assessment, CollectionError, Module, SourceBatch, unknown_assessment
from .modules.fixture import fixture_profile
from .verdicts import StoredVerdict, VerdictKey, VerdictStore


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class PublishedAssessment:
    """One normalized assessment as published, with the facts the reader needs
    to age it: when it was fetched and, for a VALID record, the hold it stays
    in force for and the observation that hold counts from."""

    assessment: Assessment
    fetched_mono: float
    source_max_age_ms: int
    profile_id: str
    profile_version: str
    profile_digest: str
    hold_ms: int = 0                          # 0 = UNKNOWN record, no hold
    hold_origin_mono: Optional[float] = None  # the observation the hold counts from
    retained: bool = False                    # re-published without a new VALID record
    restored: bool = False                    # ... restored from the state file
    retained_cause: Optional[str] = None      # why there was no new VALID record

    def render(self, now_mono: float) -> Dict[str, Any]:
        a = self.assessment
        since_fetch_ms = max(0, int((now_mono - self.fetched_mono) * 1000))
        age: Optional[int] = None if a.evidence_age_ms is None else a.evidence_age_ms + since_fetch_ms
        ttl = 0
        if a.quality == contract.QUALITY_VALID and self.hold_origin_mono is not None:
            # Held since the hold origin, on the same millisecond basis as the
            # age: for a record whose hold counts from its own observation,
            # held == age and ttl + age == hold. A deny the policy emits
            # without an evidence age (SHARE_ABSENT, IDENTITY_MISMATCH) is
            # still a critical verdict: its hold counts from the fetch.
            lead_ms = int(round((self.fetched_mono - self.hold_origin_mono) * 1000))
            held = max(0, lead_ms + since_fetch_ms)
            ttl = max(0, min(contract.MAX_REMAINING_TTL_MS, self.hold_ms - held))
        quality, allowed, ppm, reasons = a.quality, a.allowed, a.multiplier_ppm, list(a.reason_codes)
        if ttl == 0:
            if allowed or quality == contract.QUALITY_VALID:
                cause = [self.retained_cause] if self.retained and self.retained_cause else []
                reasons = _lead(["EVIDENCE_EXPIRED"] + cause, reasons)
            quality, allowed, ppm = contract.QUALITY_UNKNOWN, False, 0
        elif self.retained:
            head = ["VERDICT_RETAINED"] + (["RESTORED_FROM_STATE"] if self.restored else [])
            if self.retained_cause:
                head.append(self.retained_cause)
            reasons = _lead(head, reasons)
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


def _lead(head: List[str], reasons: List[str]) -> List[str]:
    """``head`` first, then the other reasons in order, bounded."""
    return (head + [r for r in reasons if r not in head])[: contract.MAX_REASON_CODES]


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
    """Re-entry hold-down per (ds_id, binding_generation) (CON-16).

    It applies only while the verdict in force is a deny (design rule 6): the
    first allow after a start, an UNKNOWN period or a neutral period is
    published at once, while an allow that leaves a deny is withheld until
    ``recovery_distinct_cycles`` distinct source cycles and
    ``recovery_hold_down_ms`` agree."""

    @dataclass
    class _State:
        start_mono: Optional[float] = None
        cycles: set = field(default_factory=set)
        lease_expiry_mono: Optional[float] = None

    def __init__(self) -> None:
        self._states: Dict[Tuple[int, int], HoldDownTracker._State] = {}

    def apply(
        self,
        a: Assessment,
        cycle_key: str,
        now_mono: float,
        fetched_mono: float,
        profile: Profile,
        deny_in_force: bool,
    ) -> Assessment:
        key = (a.ds_id, a.binding_generation)
        if not a.allowed:
            self._states.pop(key, None)
            return a
        if not deny_in_force:
            # Rule 6: only leaving a deny is held down. No deny in force (a
            # start, an UNKNOWN or neutral period, an allow after an allow)
            # means there is nothing to protect the placement from.
            self._states.pop(key, None)
            return a
        st = self._states.setdefault(key, HoldDownTracker._State())
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
        store: Optional[VerdictStore] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.instance = instance
        #: The runtime's verdict store (shared by all instances; a private one
        #: when the instance runs on its own, e.g. in a unit test).
        self.store = store if store is not None else VerdictStore()
        #: Called after every cycle and collection error: the runtime saves
        #: the store to the state file (rate-bounded; it never raises here).
        self._on_change = on_change
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
        self._helper: Optional[threading.Thread] = None
        self._restart_times: List[float] = []
        self._backoff_s = 0.0
        self._failures = 0
        # Audit C-05: the last accepted source cycle; a replayed or regressed
        # generation within the same source epoch is ignored.
        self._last_source: Optional[Tuple[str, int]] = None
        self.snapshot: InstanceSnapshot = self._no_data_snapshot("NO_ASSESSMENT", contract.SNAPSHOT_FAILED, publish=False)

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

    @staticmethod
    def static_key(instance_id: str, b: Binding) -> VerdictKey:
        return VerdictKey(instance_id, b.ds_id, b.binding_generation, b.target_id, b.expected_target_incarnation)

    def key_for(self, b: Binding) -> VerdictKey:
        return InstanceRuntime.static_key(self.instance.id, b)

    def _retained_or_unknown(
        self, b: Binding, cause: str, now: float, fallback: Optional[PublishedAssessment] = None
    ) -> PublishedAssessment:
        """No new VALID record for ``b``: its verdict in force, re-published
        as retained (``cause`` says why), else ``fallback`` -- the cycle's own
        UNKNOWN record -- or a bare UNKNOWN record carrying ``cause``. A
        verdict whose hold ran out is dropped from the store."""
        key = self.key_for(b)
        held = self.store.get(key)
        if held is not None:
            if held.remaining_ms(now) > 0:
                return replace(held.published, retained=True, restored=held.restored, retained_cause=cause)
            self.store.drop(key, expected=held)
        if fallback is not None:
            return fallback
        pid, pver, pdig, max_age = self._profile_triplet()
        return PublishedAssessment(unknown_assessment(b, self._datastore_id(b), cause), now, max_age, pid, pver, pdig)

    def _no_data_snapshot(self, code: str, status: str, publish: bool = True, error: Optional[str] = None) -> InstanceSnapshot:
        """A snapshot without new data (start, collection error, WORKER_STUCK):
        every binding carries its retained verdict or the UNKNOWN record."""
        now = self._clock()
        records = tuple(self._retained_or_unknown(b, code, now) for b in self.instance.bindings)
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
            batch, raw = self._collect_bounded(deadline_s)
        except CollectionError as exc:
            self._on_collection_error(exc)
            return
        except Exception as exc:  # a module bug must not kill the runtime (CON-04)
            self._on_collection_error(CollectionError("MODULE_ERROR", f"{exc.__class__.__name__}: {exc}", retryable=False))
            return
        # Audit C-05 / CON-14: within one source epoch the generation must
        # advance; a late or replayed snapshot is not new evidence and takes
        # no part in the hold-down. A new source epoch (agent restart) resets.
        if self._last_source is not None and self._last_source[0] == batch.source_epoch and batch.source_generation <= self._last_source[1]:
            self.log.counters.inc("connector_source_replay_total", {"instance": self.instance.id})
            self.log.limited("warn", "source_replay_ignored", f"{self.instance.id}:replay", instance=self.instance.id, source_cycle=batch.cycle_key, last_accepted=f"{self._last_source[0]}:{self._last_source[1]}")
            return
        self._last_source = (batch.source_epoch, batch.source_generation)
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
            if batch.snapshot_status == contract.SNAPSHOT_FAILED and a.quality == contract.QUALITY_VALID:
                # A FAILED source snapshot is no new data (design §4.3): a VALID
                # record the policy still derived from it (IDENTITY_MISMATCH is
                # checked first) is not a fresh verdict, so a VALID record in a
                # FAILED snapshot is always a retained one.
                a = replace(a, quality=contract.QUALITY_UNKNOWN, allowed=False, multiplier_ppm=0)
            key = self.key_for(b)
            held = self.store.get(key)
            deny_in_force = held is not None and held.critical and held.remaining_ms(now) > 0
            a = self._hold.apply(a, batch.cycle_key, now, batch.fetched_mono, profile, deny_in_force)
            pa = PublishedAssessment(
                assessment=a,
                fetched_mono=batch.fetched_mono,
                source_max_age_ms=max_age,
                profile_id=pid,
                profile_version=pver,
                profile_digest=pdig,
            )
            if a.quality == contract.QUALITY_VALID:
                # A newer VALID record replaces the verdict at once (rule 2);
                # its hold counts from its observation (the fetch, when the
                # policy gave the deny no evidence age). A hold-down deny is
                # the exception (rule 6): it keeps the last critical
                # observation, so the hold-down never extends the critical hold.
                critical = not a.allowed
                withheld = deny_in_force and a.reason_codes[:1] == ["RECOVERY_HOLD_DOWN"]
                if withheld and held is not None:
                    origin = held.hold_origin_mono
                else:
                    origin = batch.fetched_mono - (a.evidence_age_ms or 0) / 1000.0
                pa = replace(pa, hold_ms=profile.hold_ms(critical), hold_origin_mono=origin)
                if not self._stop.is_set():
                    # A stopped worker stores nothing: a reload replaced it and
                    # its bounded stop may have returned while this cycle was
                    # still collecting -- the reload may have dropped this key.
                    self.store.put(key, StoredVerdict(pa, critical=critical, hold_origin_mono=origin, profile_id=pid))
            else:
                # An UNKNOWN record is no new data (rule 3): the verdict in force stays.
                pa = self._retained_or_unknown(b, (a.reason_codes or ["NO_ASSESSMENT"])[0], now, fallback=pa)
            records.append(pa)
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
        self._changed()

    def _changed(self) -> None:
        """Hand the (possibly) changed store to the runtime's state writer.
        A stopped worker has nothing to persist; a writer failure never
        reaches the cycle."""
        if self._on_change is None or self._stop.is_set():
            return
        try:
            self._on_change()
        except Exception as exc:  # pragma: no cover - save_state catches its own errors
            self.log.limited("warn", "state_save_failed", f"{self.instance.id}:state", instance=self.instance.id, error=f"{exc.__class__.__name__}: {exc}")

    # --- the state file -----------------------------------------------------

    def restore(self, b: Binding, entry: Dict[str, Any], observed_at: str, obs_age_ms: int, hold_age_ms: int) -> Optional[str]:
        """Put a verdict read from the state file into the store (design
        §4.4): the stored verdict as a VALID record whose evidence is
        ``obs_age_ms`` old and whose hold -- the current profile's for its
        class, so the published TTL and the store agree -- started
        ``hold_age_ms`` ago. ``entry`` passed ``state.check_entry``.

        Returns None when restored, else why not: PROFILE_MISMATCH (another
        profile id), DATASTORE_MISMATCH (the instance now names another data
        store), EXPIRED (the hold ran out by the wall clock), INVALID (the
        CON-06 invariants reject the record)."""
        now = self._clock()
        pid, pver, pdig, max_age = self._profile_triplet()
        if entry["profile_id"] != pid:
            return "PROFILE_MISMATCH"
        if entry["datastore_id"] != self._datastore_id(b):
            return "DATASTORE_MISMATCH"
        critical = bool(entry["critical"])
        hold_ms = (self.profile or fixture_profile()).hold_ms(critical)
        if hold_age_ms >= hold_ms:
            return "EXPIRED"
        a = Assessment(
            ds_id=b.ds_id,
            binding_generation=b.binding_generation,
            datastore_id=entry["datastore_id"],
            target_id=b.target_id,
            target_incarnation=entry.get("target_incarnation"),
            quality=contract.QUALITY_VALID,
            allowed=entry["allowed"],
            multiplier_ppm=entry["multiplier_ppm"],
            reason_codes=list(entry["reason_codes"]),
            observed_at=observed_at,
            evidence_age_ms=obs_age_ms,
            capacity_domain_id=entry.get("capacity_domain_id"),
            shared_resource_ids=list(entry["shared_resource_ids"]),
        ).normalized()
        if a.quality != contract.QUALITY_VALID or a.allowed != entry["allowed"]:
            return "INVALID"
        origin = now - hold_age_ms / 1000.0
        pa = PublishedAssessment(
            assessment=a,
            fetched_mono=now,
            source_max_age_ms=max_age,
            profile_id=pid,
            profile_version=pver,
            profile_digest=pdig,
            hold_ms=hold_ms,
            hold_origin_mono=origin,
            restored=True,
        )
        self.store.put(self.key_for(b), StoredVerdict(pa, critical=critical, hold_origin_mono=origin, profile_id=pid, restored=True))
        return None

    def republish_from_store(self) -> None:
        """Rebuild the start-up snapshot (sequence 0, never published by a
        cycle yet) from the store, so the first publication already carries
        the restored verdicts."""
        with self._lock:
            if self._sequence != 0:
                return
        self._publish(self._no_data_snapshot("NO_ASSESSMENT", contract.SNAPSHOT_FAILED, publish=False))

    def _collect_bounded(self, deadline_s: float) -> Tuple[SourceBatch, List[Assessment]]:
        """Run module.collect AND module.evaluate in one helper thread bounded
        by the deadline (audit C-07). At most one helper per instance is ever
        in flight: while a previous one has not returned, this tick is skipped
        (COLLECT_IN_FLIGHT) instead of stacking a second collect on the same
        module; the previous helper's result, when it finally arrives, is
        discarded (its result dict is private to that call)."""
        prev = self._helper
        if prev is not None and prev.is_alive():
            self._stuck = True
            self.log.counters.inc("connector_worker_stuck_total", {"instance": self.instance.id})
            raise CollectionError("COLLECT_IN_FLIGHT", "the previous collect is still running; tick skipped", retryable=True, details={"stuck": True})
        self._collect_generation += 1
        generation = self._collect_generation
        result: Dict[str, Any] = {}
        done = threading.Event()

        def target() -> None:
            try:
                batch = self.module.collect(deadline_s)
                result["batch"] = batch
                result["raw"] = self.module.evaluate(batch, self.profile, self.instance.bindings)
            except BaseException as exc:  # noqa: BLE001 - forwarded as a typed error
                result["error"] = exc
            finally:
                done.set()

        t = threading.Thread(target=target, name=f"collect-{self.instance.id}-{generation}", daemon=True)
        self._helper = t
        t.start()
        if not done.wait(deadline_s + 0.5):
            self._stuck = True
            self.log.counters.inc("connector_worker_stuck_total", {"instance": self.instance.id})
            raise CollectionError("SOURCE_TIMEOUT", "collect did not return by the deadline (helper left to finish alone)", retryable=True, details={"stuck": True})
        self._stuck = False
        self._helper = None
        if "error" in result:
            err = result["error"]
            if isinstance(err, CollectionError):
                raise err
            raise CollectionError("MODULE_ERROR", f"{err.__class__.__name__}: {err}", retryable=False)
        return result["batch"], list(result["raw"])

    def _on_collection_error(self, exc: CollectionError) -> None:
        """No new data of any kind revokes nothing (rule 3): a new FAILED
        snapshot re-publishes every verdict in force as retained (cause = the
        error code). Non-retryable errors still alert."""
        self._failures += 1
        self._backoff_s = min(contract.MAX_RETRY_BACKOFF_MS / 1000.0, (2 ** min(self._failures, 3)) * 0.5)
        self.log.counters.inc("connector_poll_errors_total", {"instance": self.instance.id, "code": exc.code})
        snap = self._no_data_snapshot(exc.code, contract.SNAPSHOT_FAILED, error=exc.code)
        self.log.limited(
            "warn" if exc.retryable else "error",
            "collect_failed_retained",
            f"{self.instance.id}:{exc.code}",
            instance=self.instance.id,
            code=exc.code,
            message=str(exc),
            retained=sum(1 for pa in snap.assessments if pa.retained),
            alert=not exc.retryable,
        )
        self._changed()

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
            self._no_data_snapshot("WORKER_STUCK", contract.SNAPSHOT_FAILED, error="WORKER_STUCK")
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

    def __init__(self, config: Config, logger: Optional[Logger] = None, clock=time.monotonic, module_factory=None, wall_clock=None):
        self.log = logger or Logger()
        self._clock = clock
        #: () -> aware UTC datetime; dates the state file (monotonic time does
        #: not survive a restart).
        self._wall = wall_clock or (lambda: datetime.now(timezone.utc))
        self._module_factory = module_factory
        self.started_at = utc_now()
        self.runtime_epoch = f"{socket.gethostname()}:{self.started_at}:{os.getpid()}"
        self._lock = threading.Lock()
        self.config = config
        self.instances: Dict[str, InstanceRuntime] = {}
        self._incarnations: Dict[str, int] = {}
        #: The verdicts of every binding; survives reloads (design §4.2) and,
        #: through the state file, restarts (§4.4).
        self.verdicts = VerdictStore()
        # The state writer: one write at a time, at most one per interval.
        self._save_lock = threading.Lock()
        self._saved_version = -1
        self._last_save_mono: Optional[float] = None
        self._build_instances(config)
        if config.runtime.state_path:
            self._restore_state()
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
            store=self.verdicts,
            on_change=self.save_state,
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
        # The last change a cycle made inside the rate bound is not lost.
        self.save_state(force=True)
        self.log.info("runtime_stopped")

    def reload(self, config: Config) -> None:
        """Atomic switch to a validated configuration (CON-19).

        Unchanged instances keep their epoch, sequence and hold-down state;
        a changed or new instance gets a fresh runtime (new epoch; until its
        first collect every binding carries its retained verdict or reads
        UNKNOWN); a removed instance is stopped. The verdict store is kept;
        a binding that is no longer configured as it was (removed, or a new
        generation, target or pinned incarnation) loses its verdict (rule 5).
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
            # After the old workers were told to stop. A bounded stop can return
            # while a worker is still collecting; it finishes that cycle but
            # stores nothing once stopped (run_cycle), and the state writer
            # takes configured bindings only.
            configured = {InstanceRuntime.static_key(inst.id, b) for inst in config.instances for b in inst.bindings}
            for k in self.verdicts.keys():
                if k not in configured:
                    self.verdicts.drop(k)
            self.instances = new
            self.config = config
        self.log.info("config_reloaded", config_digest=config.digest, instances=len(self.instances))

    # --- the state file (design §4.4) ----------------------------------------

    def _restore_state(self) -> None:
        """Load the state file once, before the first publication. An entry
        comes back only when it is well-formed, its key matches a configured
        binding exactly, its profile id is the binding's and its hold has not
        run out by the wall clock under the current profile. Nothing here can
        stop the start."""
        path = self.config.runtime.state_path
        outcome: Dict[str, int] = {}
        try:
            entries, warning = state.load(path)
            if warning:
                self.log.warn("state_file_ignored", path=path, reason=warning)
            now_wall = self._wall()
            bound = {InstanceRuntime.static_key(inst.id, b): (inst.id, b) for inst in self.config.instances for b in inst.bindings}
            invalid: Optional[str] = None
            for e in entries:
                why = state.check_entry(e)
                if why is not None:
                    invalid = invalid or why
                    why = "INVALID"
                else:
                    why = self._restore_entry(e, bound, now_wall)
                outcome[why or "RESTORED"] = outcome.get(why or "RESTORED", 0) + 1
            if outcome:
                restored = outcome.pop("RESTORED", 0)
                self.log.info("state_restored", path=path, restored=restored, skipped=outcome, first_invalid=invalid)
        except Exception as exc:  # noqa: BLE001 - a bad file costs the restore, never the start
            self.log.warn("state_restore_failed", path=path, error=f"{exc.__class__.__name__}: {exc}")
        for ir in self.instances.values():
            ir.republish_from_store()

    def _restore_entry(self, e: Dict[str, Any], bound: Dict[VerdictKey, Tuple[str, Binding]], now_wall: datetime) -> Optional[str]:
        key = VerdictKey(e["instance"], e["ds_id"], e["binding_generation"], e["target_id"], e.get("expected_target_incarnation"))
        if key not in bound:
            return "UNBOUND"                   # removed, rebound or another target/incarnation (rule 5)
        if self.verdicts.get(key) is not None:
            return "DUPLICATE"
        iid, b = bound[key]
        observed = state.parse_utc(e["observed_at"])
        # The hold of a critical verdict counts from its last critical
        # observation -- never later than its observation (a hold-down deny
        # carries the earlier one); any other verdict's from its observation.
        origin = observed
        if e["critical"]:
            crit = state.parse_utc(e.get("critical_observed_at"))
            if crit is not None and crit < observed:
                origin = crit
        # A stamp in the future (the clock stepped back) counts as age 0, so
        # the remaining hold is at most the full hold.
        obs_age_ms = max(0, int(round((now_wall - observed).total_seconds() * 1000)))
        hold_age_ms = max(0, int(round((now_wall - origin).total_seconds() * 1000)))
        return self.instances[iid].restore(b, e, state.iso(min(observed, now_wall)), obs_age_ms, hold_age_ms)

    def save_state(self, force: bool = False) -> None:
        """Write the store to the state file: after a change, at most once
        per ``collect_interval_ms`` (``force``: now, e.g. on stop). Only
        entries of configured bindings whose hold has not run out are
        written. A failure is a WARN and ``connector_state_write_errors_total``;
        the runtime carries on with its in-memory store. Called from every
        worker thread: one write at a time, and a worker never waits for
        another's write (the next cycle carries its change)."""
        path = self.config.runtime.state_path
        if not path:
            return
        if not (self._save_lock.acquire(timeout=2.0) if force else self._save_lock.acquire(blocking=False)):
            return
        try:
            now = self._clock()
            version = self.verdicts.version
            if not force:
                if version == self._saved_version:
                    return
                if self._last_save_mono is not None and (now - self._last_save_mono) * 1000 < self.config.runtime.collect_interval_ms:
                    return
            try:
                now_wall = self._wall()
                configured = {InstanceRuntime.static_key(inst.id, b) for inst in self.config.instances for b in inst.bindings}
                items = sorted(self.verdicts.items(), key=lambda kv: (kv[0].instance, kv[0].ds_id, kv[0].binding_generation))
                entries = [state.entry_from(k, v, now, now_wall) for k, v in items if k in configured and v.remaining_ms(now) > 0]
                state.save(path, entries, self.runtime_epoch, now_wall)
            except Exception as exc:  # noqa: BLE001 - OSError above all; nothing may reach a worker
                self.log.counters.inc("connector_state_write_errors_total", {})
                self.log.limited("warn", "state_write_failed", "state:write", path=path, error=f"{exc.__class__.__name__}: {exc}")
            self._saved_version = version
            self._last_save_mono = now
        finally:
            self._save_lock.release()

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
