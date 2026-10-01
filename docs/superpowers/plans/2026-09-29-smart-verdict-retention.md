# `smart` Verdict Retention and the Neutral Multiplier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In `placement_mode = smart` the connector only steers: a data store never reported on is neutral (multiplier 1), a reported verdict stays in force without new data for 20 min (critical) / 10 min (other) from its observation, then the data store is neutral again; no data loss on any link makes a data store unavailable.

**Architecture:** The connector (`connectors/lattice-ds-connector`) owns the policy: a `Runtime`-level verdict store, hold times in the profile, retained verdicts re-published with a long `remaining_ttl_ms`, recovery hold-down only after a deny, and a state file that survives restarts. The MDS (fork `XinnorLab/pnfs-lattice`) keeps its own per-DS verdict store across batches so a record without new data never overwrites a verdict in force, turns "nothing in force" into neutral in the gate, and reports verdict/hold per data store. The helper (`tools/lattice-placement`) turns steering outages into warnings.

**Tech Stack:** Python 3.9 stdlib + pytest (connector, helper); C11 with the upstream `ASSERT_*`/`RUN_TEST` harness, CMake, node225 build loop `mds/scripts/pm-run.sh` (fork); GitHub Actions on both repos.

**Spec:** `docs/superpowers/specs/2026-09-29-smart-verdict-retention-design.md` (approved 2026-09-29). Background: `docs/superpowers/specs/2026-09-23-placement-modes-design.md`.

## Global Constraints

- English in every artifact; Conventional Commits; commits end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; author `XinnorLab <135218967+XinnorLab@users.noreply.github.com>`; push via the `github-xinnorlab` SSH alias.
- Branches: pNFS `feat/smart-verdict-retention` (exists, holds the spec); fork `xinnor/smart-verdict-retention` cut from `xinnor/placement-modes` @ `639b6c5`. Each lands by PR with `--merge` (never squash).
- Spec-first: every behaviour change updates the live doc it touches in the same task (listed per task).
- Defaults: `critical_hold_ms = 1200000`, `verdict_hold_ms = 600000`; range of both `source_max_age_ms .. 3600000`; `contract_version = "1.1"`; `remaining_ttl_ms` ≤ 3 600 000; new reason codes `VERDICT_RETAINED`, `RESTORED_FROM_STATE`; state file default `/var/lib/lattice-ds-connector/verdicts.json`, mode 0600.
- `source_max_age_ms` keeps its old upper bound 20 000 (a new constant `MAX_SOURCE_MAX_AGE_MS`).
- Rollout order on the lab: MDS first, connector second (spec §8).
- Tests: connector `cd connectors/lattice-ds-connector && python -m pytest -q`; helper `cd tools/lattice-placement && python -m pytest -q` (use `~/Documents/GitHub/xiNAS/.venv/bin/python`); fork `bash mds/scripts/pm-run.sh <regex|all>` from the pNFS checkout (it syncs the local fork working tree; `--no-sync` tests the pushed base only).
- The lab MDS and connectors restart only in an announced window with Sergey's go-ahead (Task R10).

---

## File structure

| File | Responsibility |
|---|---|
| `connectors/lattice-ds-connector/lattice_ds_connector/contract.py` | contract 1.1, TTL cap, source-age cap, hold defaults, new reason codes |
| `connectors/lattice-ds-connector/contracts/connector-batch.schema.json` | `remaining_ttl_ms` maximum 3 600 000 |
| `connectors/lattice-ds-connector/lattice_ds_connector/config.py` | `Profile.critical_hold_ms/verdict_hold_ms`, `RuntimeConfig.state_path` |
| `connectors/lattice-ds-connector/lattice_ds_connector/verdicts.py` (new) | `VerdictKey`, `StoredVerdict`, `VerdictStore` |
| `connectors/lattice-ds-connector/lattice_ds_connector/state.py` (new) | state file load/save (atomic, tolerant) |
| `connectors/lattice-ds-connector/lattice_ds_connector/runtime.py` | hold-based TTL, retained rendering, errors keep verdicts, hold-down only after deny, store wiring, state save/restore |
| `connectors/lattice-ds-connector/lattice_ds_connector/preflight.py` | per-DS `verdict` (fresh/retained/restored) and `hold_left_ms` |
| `connectors/lattice-ds-connector/systemd/lattice-ds-connector.service` | `StateDirectory` |
| `connectors/lattice-ds-connector/tests/test_retention.py` (new), `tests/test_state.py` (new), `tests/test_runtime.py`, `tests/test_config.py`, `tests/test_preflight.py` | tests |
| connector docs: `docs/profile-xinas-mvp.md`, `docs/troubleshooting.md`, `README.md`, `docs/compatibility-manifest.json` | live docs |
| fork `include/ds_connector.h`, `src/mds/ds_connector.c` | MDS verdict store, FAILED-snapshot rule, view from the store |
| fork `include/placement_gate.h`, `src/fsal_obj/placement_gate.c` | neutral rule, no `MODE_NOT_READY`, live-row domain, readiness counts, per-DS verdict/hold |
| fork `src/cluster/cluster_transport.c`, `include/mds_metrics.h`, `src/common/mds_metrics.c` | `config show` rows, gauges, counter |
| fork tests `tests/unit/test_ds_connector.c`, `test_placement_gate.c`, `test_placement_create_boundary.c`, `test_cluster_transport.c`; fork `docs/placement-modes.md` | tests, live doc |
| `tools/lattice-placement/lattice_placement/{live,verify,validate}.py`, tests + fixtures | helper |
| `docs/placement-modes/contract-manifest.json` (+ bundled copy), `docs/superpowers/specs/2026-09-23-placement-modes-design.md`, `docs/placement-modes/operations.md`, `docs/TODO.md`, `mds/manifest.json` + patches | cross-cutting docs |
| `docs/placement-modes/stand-<date>-retention.md` (new) | stand report |

---

### Task R1: Contract 1.1 and the profile hold fields (connector)

**Files:**
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/contract.py`, `contracts/connector-batch.schema.json`, `lattice_ds_connector/config.py`, `docs/profile-xinas-mvp.md`, `docs/compatibility-manifest.json`
- Test: `tests/test_config.py`, `tests/test_server.py`

**Interfaces:**
- Produces: `contract.CONTRACT_VERSION = "1.1"`, `contract.MAX_REMAINING_TTL_MS = 3_600_000`, `contract.MAX_SOURCE_MAX_AGE_MS = 20_000`, `contract.DEFAULT_CRITICAL_HOLD_MS = 1_200_000`, `contract.DEFAULT_VERDICT_HOLD_MS = 600_000`, reason codes `VERDICT_RETAINED`, `RESTORED_FROM_STATE` in `contract.RUNTIME_REASONS`; `Profile.critical_hold_ms: int`, `Profile.verdict_hold_ms: int` (both in `placement_fields()` and therefore in the profile digest); `Profile.hold_ms(critical: bool) -> int`.

- [ ] **Step 1: Failing tests** in `tests/test_config.py`:

```python
def test_profile_hold_fields_default_and_digest(token_file, tmp_path):
    from lattice_ds_connector.config import Profile
    from lattice_ds_connector import contract
    p = make_profile()
    assert p.critical_hold_ms == contract.DEFAULT_CRITICAL_HOLD_MS == 1_200_000
    assert p.verdict_hold_ms == contract.DEFAULT_VERDICT_HOLD_MS == 600_000
    assert p.hold_ms(True) == 1_200_000 and p.hold_ms(False) == 600_000
    q = make_profile(critical_hold_ms=180_000)
    assert q.digest != p.digest                      # part of placement_fields


@pytest.mark.parametrize("field,value,code", [
    ("critical_hold_ms", 3_600_001, "RANGE"),
    ("verdict_hold_ms", 999, "RANGE"),
    ("critical_hold_ms", 15_000, "HOLD_RELATION"),   # below source_max_age_ms (20 000)
    ("verdict_hold_ms", 19_999, "HOLD_RELATION"),
    ("source_max_age_ms", 20_001, "RANGE"),          # the source-age cap did not move
])
def test_profile_hold_ranges(token_file, tmp_path, field, value, code):
    doc = example(token_file, tmp_path)              # the file's valid-config builder
    doc["profiles"][0][field] = value
    assert code in errors_of(doc)                    # the file's helper: codes from validate_config_dict
```

In `tests/test_server.py` add:

```python
def test_contract_1_1_and_long_ttl_validate(schemas):
    from lattice_ds_connector import contract
    assert contract.CONTRACT_VERSION == "1.1"
    batch = sample_batch()                           # the batch helper used by the other server tests
    batch["instances"][0]["assessments"][0]["remaining_ttl_ms"] = 3_600_000
    validate(schemas["batch"], batch)
    batch["instances"][0]["assessments"][0]["remaining_ttl_ms"] = 3_600_001
    with pytest.raises(Exception):
        validate(schemas["batch"], batch)
```

- [ ] **Step 2: Run** `python -m pytest -q tests/test_config.py tests/test_server.py` → the new tests FAIL (attribute / schema errors).

- [ ] **Step 3: Implement.** `contract.py`:

```python
CONTRACT_VERSION = "1.1"
...
MAX_REMAINING_TTL_MS = 3_600_000     # a verdict stays in force at most one hour without a new one
MAX_SOURCE_MAX_AGE_MS = 20_000       # the oldest evidence a policy may call fresh (unchanged)
DEFAULT_CRITICAL_HOLD_MS = 1_200_000
DEFAULT_VERDICT_HOLD_MS = 600_000
```

and in `RUNTIME_REASONS`:

```python
    "VERDICT_RETAINED": "the source gave no new verdict; the last observed one is repeated until its hold runs out",
    "RESTORED_FROM_STATE": "the retained verdict was restored from the state file after a connector restart",
```

`connector-batch.schema.json`: `"remaining_ttl_ms": { "type": "integer", "minimum": 0, "maximum": 3600000 }`.

`config.py` `Profile`: add after `recovery_distinct_cycles`:

```python
    critical_hold_ms: int = contract.DEFAULT_CRITICAL_HOLD_MS
    verdict_hold_ms: int = contract.DEFAULT_VERDICT_HOLD_MS

    def hold_ms(self, critical: bool) -> int:
        return self.critical_hold_ms if critical else self.verdict_hold_ms
```

add both keys to `placement_fields()`; in `_parse_profile` use `contract.MAX_SOURCE_MAX_AGE_MS` for the `source_max_age_ms` upper bound, then:

```python
    crit = _int(c, raw, "critical_hold_ms", path, d.critical_hold_ms, 1000, contract.MAX_REMAINING_TTL_MS)
    vhold = _int(c, raw, "verdict_hold_ms", path, d.verdict_hold_ms, 1000, contract.MAX_REMAINING_TTL_MS)
    for name, val in (("critical_hold_ms", crit), ("verdict_hold_ms", vhold)):
        if val is not None and max_age is not None and val < max_age:
            c.error("HOLD_RELATION", f"{path}.{name}", "must be at least source_max_age_ms")
```

add `crit, vhold` to the `None in (...)` guard and pass them to `Profile(...)`.

- [ ] **Step 4: Docs.** `docs/profile-xinas-mvp.md`: a "Verdict holds" section (the two fields, defaults, ranges, "counted from the observation; the MDS keeps a verdict exactly this long without a new one; after that the data store is neutral"). `docs/compatibility-manifest.json`: `contract_version` 1.1, the two fields, the two reason codes.

- [ ] **Step 5: Run** the whole connector suite → PASS except tests that assert `CONTRACT_VERSION == "1.0"` or a 20 000 TTL cap: update those expectations to 1.1 / the new constants (they are contract assertions, not behaviour).

- [ ] **Step 6: Commit** `feat(connector): contract 1.1 -- verdict holds in the profile, TTL up to one hour`.

### Task R2: Verdict store, hold-based TTL, retained verdicts, errors keep verdicts (connector)

**Files:**
- Create: `connectors/lattice-ds-connector/lattice_ds_connector/verdicts.py`, `tests/test_retention.py`
- Modify: `lattice_ds_connector/runtime.py`, `lattice_ds_connector/preflight.py`, `tests/test_runtime.py`, `tests/test_preflight.py`, `docs/troubleshooting.md`

**Interfaces:**
- Consumes: R1's `Profile.hold_ms`, `contract.MAX_REMAINING_TTL_MS`, the new reason codes.
- Produces:

```python
# verdicts.py
@dataclass(frozen=True)
class VerdictKey:
    instance: str
    ds_id: int
    binding_generation: int
    target_id: str
    expected_target_incarnation: Optional[str]

@dataclass(frozen=True)
class StoredVerdict:
    published: "PublishedAssessment"   # the VALID record as published (post hold-down)
    critical: bool                     # not published.assessment.allowed
    hold_origin_mono: float            # the observation the hold counts from (monotonic)
    profile_id: str
    restored: bool = False

    def remaining_ms(self, profile, now_mono: float) -> int: ...

class VerdictStore:                    # thread-safe, one per Runtime
    def get(self, key: VerdictKey) -> Optional[StoredVerdict]: ...
    def put(self, key: VerdictKey, v: StoredVerdict) -> None: ...
    def drop(self, key: VerdictKey) -> None: ...
    def keys(self) -> List[VerdictKey]: ...
    def items(self) -> List[Tuple[VerdictKey, StoredVerdict]]: ...
    @property
    def version(self) -> int: ...      # bumps on every put/drop (the state writer watches it)

# runtime.py
PublishedAssessment gains: hold_ms: int = 0, hold_origin_mono: Optional[float] = None, retained: bool = False,
    restored: bool = False, retained_cause: Optional[str] = None
InstanceRuntime.__init__(..., store: Optional[VerdictStore] = None, profile_for_key=None)
InstanceRuntime.key_for(b: Binding) -> VerdictKey
Runtime.verdicts: VerdictStore
```

- [ ] **Step 1: Failing tests** `tests/test_retention.py` (reuse `FakeClock`, `ScriptedModule`, `make_instance`, `make_config`, `make_runtime`, `records`, `settle` from `tests/test_runtime.py` — move them to `tests/runtime_helpers.py`, import them from both files, and give `make_runtime` a `wall_clock=None` parameter passed through to `Runtime` (R4 uses it)):

```python
from runtime_helpers import FakeClock, ScriptedModule, make_runtime, records, settle
import source_builder as sb
from lattice_ds_connector import contract
from lattice_ds_connector.modules.base import CollectionError

HOLD_OTHER = contract.DEFAULT_VERDICT_HOLD_MS
HOLD_CRIT = contract.DEFAULT_CRITICAL_HOLD_MS


def healthy_rt():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    settle(rt, clock, cycles=3)                # allowed with or without the R3 hold-down change
    return clock, mod, rt


def test_valid_record_ttl_is_the_hold_minus_age():
    clock, mod, rt = healthy_rt()
    _, recs = records(rt)
    a = recs[0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"]
    assert HOLD_OTHER - 5_000 <= a["remaining_ttl_ms"] <= HOLD_OTHER
    clock.advance(100.0)
    _, recs = records(rt)
    assert HOLD_OTHER - 106_000 <= recs[0]["remaining_ttl_ms"] <= HOLD_OTHER - 99_000


@pytest.mark.parametrize("err", [
    CollectionError("SOURCE_TIMEOUT", "t", retryable=True),
    CollectionError("SOURCE_AUTH_FAILED", "a", retryable=False),
    CollectionError("SOURCE_SCHEMA_INVALID", "s", retryable=False),
    CollectionError("SOURCE_NOT_READY", "n", retryable=False),
])
def test_collection_errors_retain_the_verdict(err):
    clock, mod, rt = healthy_rt()
    seq0 = records(rt)[0]["sequence"]
    mod.push(error=err)
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "FAILED" and inst["sequence"] == seq0 + 1
    a = recs[0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is True
    assert a["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", err.code]
    assert a["target_incarnation"] == "training-a:7"          # the stored identity, not null
    assert HOLD_OTHER - 12_000 <= a["remaining_ttl_ms"] < HOLD_OTHER


def test_unknown_record_does_not_replace_a_verdict():
    clock, mod, rt = healthy_rt()
    mod.push(sb.with_array_states(sb.base_result(), "data", ["bogus-word"]))   # a successful collect that evaluates UNKNOWN
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is True
    assert a["placement"]["reason_codes"][0] == "VERDICT_RETAINED"
    assert a["placement"]["reason_codes"][1] != "NORMAL"     # the UNKNOWN record's first reason is the cause


def test_retained_verdict_expires_to_unknown_after_its_hold():
    clock, mod, rt = healthy_rt()
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    rt.instances["xi-01"].run_cycle()
    clock.advance(HOLD_OTHER / 1000.0 + 1.0)
    a = records(rt)[1][0]
    assert a["quality"] == "UNKNOWN" and a["placement"]["reason_codes"][0] == "EVIDENCE_EXPIRED"
    assert a["remaining_ttl_ms"] == 0


def test_critical_verdict_is_held_twenty_minutes():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    mod.push(sb.with_array_states(sb.base_result(), "data", ["offline"]))       # ARRAY_UNAVAILABLE: a veto
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False
    assert HOLD_CRIT - 5_000 <= a["remaining_ttl_ms"] <= HOLD_CRIT
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    clock.advance(HOLD_OTHER / 1000.0 + 60.0)                                   # past the 10-min hold
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False       # still held (20 min)
    clock.advance((HOLD_CRIT - HOLD_OTHER) / 1000.0)
    assert records(rt)[1][0]["quality"] == "UNKNOWN"


def rebind(cfg, ds_id, generation):
    """A copy of cfg whose binding for ds_id carries a new generation."""
    from dataclasses import replace
    inst = cfg.instances[0]
    bindings = tuple(replace(b, binding_generation=generation) if b.ds_id == ds_id else b for b in inst.bindings)
    return replace(cfg, instances=(replace(inst, bindings=bindings),))


def test_rebind_drops_the_verdict_and_reload_keeps_it():
    clock, mod, rt = healthy_rt()
    cfg = rt.config
    rt.reload(cfg)                                            # same bindings: kept
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    rt.instances["xi-01"].run_cycle()
    assert records(rt)[1][0]["placement"]["reason_codes"][0] == "VERDICT_RETAINED"
    rt.reload(rebind(cfg, ds_id=0, generation=2))
    assert not any(k.ds_id == 0 and k.binding_generation == 1 for k in rt.verdicts.keys())
    a = records(rt)[1][0]
    assert a["quality"] == "UNKNOWN"                          # neutral until the new binding reports


def test_preflight_reports_fresh_and_retained(schemas):
    from lattice_ds_connector.preflight import evaluate
    clock, mod, rt = healthy_rt()
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    rt.instances["xi-01"].run_cycle()
    rep = evaluate({"ready": True}, rt.batch(), expect_ds=[0, 1, 2])
    assert rep["ready"] is True
    row = next(r for r in rep["ds"] if r["ds_id"] == 0)
    assert row["verdict"] == "retained" and row["hold_left_ms"] > 0
```

In `tests/test_runtime.py` change the expectations that encoded fail-closed behaviour:
- `test_t19_non_retryable_failure_revokes_immediately` → rename `test_t19_non_retryable_failure_retains_and_alerts`: after the error every record is `VALID` with `reason_codes[:2] == ["VERDICT_RETAINED", code]`, `snapshot_status == "FAILED"`, and the `collect_failed_revoked` log line is renamed `collect_failed_retained` with `alert=True`.
- `test_t19_transport_timeout_retains_until_original_expiry`: the expiry happens after `HOLD_OTHER`, not after 25 s.
- `test_t18_ages_grow_and_expire_without_a_new_collect`: expiry after the hold.

- [ ] **Step 2: Run** `python -m pytest -q tests/test_retention.py tests/test_runtime.py` → FAIL.

- [ ] **Step 3: Implement `verdicts.py`:**

```python
# SPDX-License-Identifier: MIT
"""The verdict store (smart verdict retention design §4.2): the last VALID
verdict per binding, kept across cycles and configuration reloads, dropped
on a rebind or when its hold runs out."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class VerdictKey:
    instance: str
    ds_id: int
    binding_generation: int
    target_id: str
    expected_target_incarnation: Optional[str]


@dataclass(frozen=True)
class StoredVerdict:
    published: Any                 # runtime.PublishedAssessment (VALID)
    critical: bool
    hold_origin_mono: float
    profile_id: str
    restored: bool = False

    def remaining_ms(self, profile, now_mono: float) -> int:
        held = int((now_mono - self.hold_origin_mono) * 1000)
        return max(0, profile.hold_ms(self.critical) - max(0, held))


class VerdictStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._v: Dict[VerdictKey, StoredVerdict] = {}
        self._version = 0

    def get(self, key: VerdictKey) -> Optional[StoredVerdict]:
        with self._lock:
            return self._v.get(key)

    def put(self, key: VerdictKey, v: StoredVerdict) -> None:
        with self._lock:
            self._v[key] = v
            self._version += 1

    def drop(self, key: VerdictKey) -> None:
        with self._lock:
            if self._v.pop(key, None) is not None:
                self._version += 1

    def keys(self) -> List[VerdictKey]:
        with self._lock:
            return list(self._v)

    def items(self) -> List[Tuple[VerdictKey, StoredVerdict]]:
        with self._lock:
            return list(self._v.items())

    @property
    def version(self) -> int:
        with self._lock:
            return self._version
```

- [ ] **Step 4: Implement in `runtime.py`.**

`PublishedAssessment` gets the new fields and a hold-based TTL:

```python
@dataclass(frozen=True)
class PublishedAssessment:
    assessment: Assessment
    fetched_mono: float
    source_max_age_ms: int
    profile_id: str
    profile_version: str
    profile_digest: str
    hold_ms: int = 0                          # 0 = UNKNOWN record, no hold
    hold_origin_mono: Optional[float] = None  # the observation the hold counts from
    retained: bool = False
    restored: bool = False
    retained_cause: Optional[str] = None

    def render(self, now_mono: float) -> Dict[str, Any]:
        a = self.assessment
        age: Optional[int] = None
        ttl = 0
        if a.evidence_age_ms is not None:
            age = a.evidence_age_ms + max(0, int((now_mono - self.fetched_mono) * 1000))
            if a.quality == contract.QUALITY_VALID and self.hold_origin_mono is not None:
                held = max(0, int((now_mono - self.hold_origin_mono) * 1000))
                ttl = max(0, min(contract.MAX_REMAINING_TTL_MS, self.hold_ms - held))
        quality, allowed, ppm, reasons = a.quality, a.allowed, a.multiplier_ppm, list(a.reason_codes)
        if self.retained and quality == contract.QUALITY_VALID:
            head = ["VERDICT_RETAINED"] + (["RESTORED_FROM_STATE"] if self.restored else [])
            if self.retained_cause:
                head.append(self.retained_cause)
            reasons = (head + [r for r in reasons if r not in head])[: contract.MAX_REASON_CODES]
        if ttl == 0:
            if allowed or quality == contract.QUALITY_VALID:
                if "EVIDENCE_EXPIRED" not in reasons:
                    reasons = (["EVIDENCE_EXPIRED"] + [r for r in reasons if r not in ("VERDICT_RETAINED", "RESTORED_FROM_STATE")])[: contract.MAX_REASON_CODES]
            quality, allowed, ppm = contract.QUALITY_UNKNOWN, False, 0
        ...   # the returned dict is unchanged
```

`InstanceRuntime.__init__` takes `store: Optional[VerdictStore] = None` (`self.store = store or VerdictStore()`) and adds:

```python
    def key_for(self, b: Binding) -> VerdictKey:
        return VerdictKey(self.instance.id, b.ds_id, b.binding_generation, b.target_id, b.expected_target_incarnation)

    def _retained_or_unknown(self, b: Binding, cause: str, now: float) -> PublishedAssessment:
        held = self.store.get(self.key_for(b))
        profile = self.profile or fixture_profile()
        if held is not None and held.remaining_ms(profile, now) > 0:
            return replace(held.published, retained=True, restored=held.restored, retained_cause=cause)
        if held is not None:
            self.store.drop(self.key_for(b))
        pid, pver, pdig, max_age = self._profile_triplet()
        return PublishedAssessment(unknown_assessment(b, self._datastore_id(b), cause), now, max_age, pid, pver, pdig)
```

In `run_cycle`, for each binding after `a = a.normalized()` and the hold-down (R3 changes `apply`'s signature; here keep the current call):

```python
            key = self.key_for(b)
            if a.quality == contract.QUALITY_VALID:
                origin = batch.fetched_mono - (a.evidence_age_ms or 0) / 1000.0
                pa = PublishedAssessment(assessment=a, fetched_mono=batch.fetched_mono, source_max_age_ms=max_age,
                                         profile_id=pid, profile_version=pver, profile_digest=pdig,
                                         hold_ms=profile.hold_ms(not a.allowed), hold_origin_mono=origin)
                self.store.put(key, StoredVerdict(pa, critical=not a.allowed, hold_origin_mono=origin, profile_id=pid))
            else:
                pa = self._retained_or_unknown(b, (a.reason_codes or ["NO_ASSESSMENT"])[0], now)
            records.append(pa)
```

Replace `_on_collection_error`'s two branches by one that publishes the store:

```python
    def _on_collection_error(self, exc: CollectionError) -> None:
        self._failures += 1
        self._backoff_s = min(contract.MAX_RETRY_BACKOFF_MS / 1000.0, (2 ** min(self._failures, 3)) * 0.5)
        self.log.counters.inc("connector_poll_errors_total", {"instance": self.instance.id, "code": exc.code})
        now = self._clock()
        records = tuple(self._retained_or_unknown(b, exc.code, now) for b in self.instance.bindings)
        snap = InstanceSnapshot(epoch=self.epoch, sequence=self._sequence + 1, snapshot_status=contract.SNAPSHOT_FAILED,
                                assessments=records, published_mono=now, last_error=exc.code, last_error_mono=now,
                                last_success_mono=self.snapshot.last_success_mono, source_cycle=None)
        self._publish(snap)
        self.log.limited("error" if not exc.retryable else "warn", "collect_failed_retained",
                         f"{self.instance.id}:{exc.code}", instance=self.instance.id, code=exc.code,
                         message=str(exc), retained=True, alert=not exc.retryable)
```

`_unknown_snapshot` (start and `WORKER_STUCK`) builds records with `_retained_or_unknown(b, code, now)` and no longer calls `self._hold.forget`. `Runtime.__init__` creates `self.verdicts = VerdictStore()` and passes it in `_make`; `reload` keeps it and drops the keys of bindings that are no longer configured:

```python
            configured = {InstanceRuntime.static_key(inst.id, b) for inst in config.instances for b in inst.bindings}
            for k in self.verdicts.keys():
                if k not in configured:
                    self.verdicts.drop(k)
```

(`InstanceRuntime.static_key(instance_id, binding) -> VerdictKey` is the static form of `key_for`.)

`preflight.evaluate`: per row add

```python
                "verdict": ("restored" if "RESTORED_FROM_STATE" in reasons else
                            "retained" if "VERDICT_RETAINED" in reasons else
                            "fresh" if quality == contract.QUALITY_VALID else "none"),
                "hold_left_ms": a.get("remaining_ttl_ms"),
```

(where `reasons = list(placement.get("reason_codes") or [])`) and keep `ready` unaffected by `retained`; `render()` prints `verdict=… hold_left_ms=…` on each DS line.

- [ ] **Step 5: Docs** `docs/troubleshooting.md`: rows for `VERDICT_RETAINED` ("the source is not answering; the last verdict holds for its hold time — look at the second reason code") and the changed meaning of `EVIDENCE_EXPIRED` ("the hold ran out; the MDS now places this data store neutrally").

- [ ] **Step 6: Run** the connector suite → PASS. **Commit** `feat(connector): verdict store -- a verdict holds for its hold time; collection errors never revoke`.

### Task R3: Recovery hold-down only after a deny (connector)

**Files:**
- Modify: `lattice_ds_connector/runtime.py` (`HoldDownTracker.apply`, its call in `run_cycle`), `docs/profile-xinas-mvp.md` (the hold-down section)
- Test: `tests/test_retention.py`, `tests/test_runtime.py`

**Interfaces:**
- Consumes: R2's `VerdictStore`, `StoredVerdict`.
- Produces: `HoldDownTracker.apply(a, cycle_key, now_mono, fetched_mono, profile, deny_in_force: bool) -> Assessment`; a hold-down deny is stored with `hold_origin_mono` = the last critical observation.

- [ ] **Step 1: Failing tests** (`tests/test_retention.py`):

```python
def test_first_collect_after_start_allows_at_once():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["placement"]["allowed"] is True and a["placement"]["reason_codes"] == ["NORMAL"]


def test_hold_down_follows_a_deny_and_does_not_extend_the_critical_clock():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    mod.push(sb.with_array_states(sb.base_result(), "data", ["offline"]))
    rt.instances["xi-01"].run_cycle()                        # critical observed at t0
    crit_ttl = records(rt)[1][0]["remaining_ttl_ms"]
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()                        # healthy again: hold-down deny
    a = records(rt)[1][0]
    assert a["placement"]["allowed"] is False and a["placement"]["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    assert crit_ttl - 6_000 <= a["remaining_ttl_ms"] <= crit_ttl - 4_000   # counted from t0, not refreshed
    clock.advance(6.0)
    rt.instances["xi-01"].run_cycle()                        # 2 distinct cycles and >= 10 s
    assert records(rt)[1][0]["placement"]["allowed"] is True


def test_unknown_period_without_a_deny_needs_no_hold_down():
    clock, mod, rt = healthy_rt()
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()                        # healthy again
    assert records(rt)[1][0]["placement"]["reason_codes"] == ["NORMAL"]
```

In `tests/test_runtime.py`: `test_first_collect_publishes_with_hold_down_then_allows` becomes `test_first_collect_publishes_at_once` (first cycle allowed, `reason_codes == ["NORMAL"]`); `test_t21_flap_resets_the_hold_down` and `test_t21_expired_lease_requires_a_fresh_hold_down` start from a deny (push an `offline` result first) so the hold-down they test still exists; `test_hold_down_parameters_from_profile` likewise.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** `HoldDownTracker.apply(..., deny_in_force: bool)`:

```python
        key = (a.ds_id, a.binding_generation)
        if not a.allowed:
            self._states.pop(key, None)
            return a
        if not deny_in_force:
            # spec rule 6: only leaving a deny is held down
            self._states.pop(key, None)
            return a
        st = self._states.setdefault(key, HoldDownTracker._State())
        ...   # the existing lease / cycle counting and the withheld-deny result
```

In `run_cycle`:

```python
            key = self.key_for(b)
            held = self.store.get(key)
            deny_in_force = held is not None and held.critical and held.remaining_ms(profile, now) > 0
            a = self._hold.apply(a, batch.cycle_key, now, batch.fetched_mono, profile, deny_in_force)
            if a.quality == contract.QUALITY_VALID:
                withheld = (not a.allowed) and a.reason_codes[:1] == ["RECOVERY_HOLD_DOWN"]
                origin = held.hold_origin_mono if (withheld and held is not None) else batch.fetched_mono - (a.evidence_age_ms or 0) / 1000.0
                ...   # the R2 put, with hold_origin_mono=origin
```

- [ ] **Step 4: Docs** `docs/profile-xinas-mvp.md` hold-down section: "applies only when the verdict in force is a deny; the deny it keeps publishing counts its hold from the last critical observation; after a start, an UNKNOWN period or a neutral period the first verdict is published at once".

- [ ] **Step 5: Run** the suite → PASS. **Commit** `feat(connector): recovery hold-down only after a deny; a restart no longer denies`.

### Task R4: The state file (connector)

**Files:**
- Create: `lattice_ds_connector/state.py`, `tests/test_state.py`
- Modify: `lattice_ds_connector/config.py` (`RuntimeConfig.state_path`, parser), `lattice_ds_connector/runtime.py` (restore at init, save after cycles), `systemd/lattice-ds-connector.service`, `README.md`, `docs/troubleshooting.md`, `examples/connector-config*.json`

**Interfaces:**
- Consumes: R2/R3's store, `PublishedAssessment`, `StoredVerdict`.
- Produces:

```python
# state.py
STATE_VERSION = 1
DEFAULT_STATE_PATH = "/var/lib/lattice-ds-connector/verdicts.json"
def entry_from(key: VerdictKey, v: StoredVerdict, now_mono: float, now_wall: datetime) -> Dict[str, Any]: ...
def save(path: str, entries: List[Dict[str, Any]], runtime_epoch: str, now_wall: datetime) -> None: ...   # atomic
def load(path: str) -> Tuple[List[Dict[str, Any]], Optional[str]]: ...   # (entries, warning); renames a corrupt file
# config.py
RuntimeConfig.state_path: Optional[str] = None     # the dataclass default is None (tests); the parser defaults to DEFAULT_STATE_PATH
# runtime.py
Runtime.__init__(..., wall_clock=None)             # () -> aware UTC datetime
Runtime.save_state(force: bool = False) -> None
```

- [ ] **Step 1: Failing tests** `tests/test_state.py`:

```python
import json, os
from datetime import datetime, timedelta, timezone
import pytest
from runtime_helpers import FakeClock, ScriptedModule, make_runtime, records
from lattice_ds_connector import contract
from lattice_ds_connector.config import RuntimeConfig
from lattice_ds_connector.modules.base import CollectionError
import source_builder as sb


class FakeWall:
    def __init__(self, clock):
        self.clock, self.base = clock, datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    def __call__(self):
        return self.base + timedelta(seconds=self.clock.t)


def rt_with_state(tmp_path, clock, mod):
    cfg = RuntimeConfig(collect_deadline_ms=200, collect_interval_ms=1000, state_path=str(tmp_path / "verdicts.json"))
    return make_runtime(clock, {"xi-01": mod}, runtime_cfg=cfg, wall_clock=FakeWall(clock))


def test_round_trip_restores_a_critical_verdict_after_restart(tmp_path):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    mod.push(sb.with_array_states(sb.base_result(), "data", ["offline"]))
    rt.instances["xi-01"].run_cycle(); rt.save_state(force=True)
    data = json.load(open(tmp_path / "verdicts.json"))
    assert data["version"] == 1 and any(e["ds_id"] == 0 and e["critical"] for e in data["verdicts"])
    assert oct(os.stat(tmp_path / "verdicts.json").st_mode & 0o777) == "0o600"
    clock.advance(60.0)
    rt2 = rt_with_state(tmp_path, clock, ScriptedModule(clock))   # a restart: nothing collected yet
    a = records(rt2)[1][0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False
    assert a["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "RESTORED_FROM_STATE"]
    assert contract.DEFAULT_CRITICAL_HOLD_MS - 70_000 <= a["remaining_ttl_ms"] <= contract.DEFAULT_CRITICAL_HOLD_MS - 55_000


def test_foreign_expired_and_future_entries(tmp_path):
    clock = FakeClock(); wall = FakeWall(clock)
    now = wall()
    entry = lambda **o: {**{"instance": "xi-01", "ds_id": 0, "binding_generation": 1, "target_id": "training-a",
                           "expected_target_incarnation": "training-a:7", "profile_id": "xinas-mvp", "allowed": False,
                           "multiplier_ppm": 0, "reason_codes": ["ARRAY_UNAVAILABLE"], "capacity_domain_id": "d",
                           "shared_resource_ids": [], "datastore_id": sb.CONTROLLER, "target_incarnation": "training-a:7",
                           "observed_at": now.isoformat().replace("+00:00", "Z"), "critical": True,
                           "critical_observed_at": now.isoformat().replace("+00:00", "Z")}, **o}
    (tmp_path / "verdicts.json").write_text(json.dumps({"version": 1, "written_at": "x", "runtime_epoch": "r", "verdicts": [
        entry(ds_id=1, binding_generation=9),                                            # foreign binding: dropped
        entry(ds_id=2, target_id="training-c", expected_target_incarnation="training-c:7",
              observed_at=(now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")),  # expired: dropped
        entry(observed_at=(now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")),  # future: age 0, full hold
    ]}))
    rt = rt_with_state(tmp_path, clock, ScriptedModule(clock))
    recs = records(rt)[1]
    assert recs[0]["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "RESTORED_FROM_STATE"]
    assert recs[0]["remaining_ttl_ms"] == contract.DEFAULT_CRITICAL_HOLD_MS
    assert recs[1]["quality"] == "UNKNOWN" and recs[2]["quality"] == "UNKNOWN"


def test_corrupt_file_is_renamed_and_the_connector_starts(tmp_path):
    clock = FakeClock()
    (tmp_path / "verdicts.json").write_text("{not json")
    rt = rt_with_state(tmp_path, clock, ScriptedModule(clock))
    assert all(r["quality"] == "UNKNOWN" for r in records(rt)[1].values())
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("verdicts.json.corrupt-")]


def test_write_failure_is_tolerated_and_rate_bounded(tmp_path, monkeypatch):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    calls = []
    import lattice_ds_connector.state as state
    monkeypatch.setattr(state, "save", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError(28, "no space")))
    for _ in range(3):
        rt.instances["xi-01"].run_cycle(); clock.advance(0.2)       # 3 cycles within one collect interval
    assert len(calls) == 1                                          # at most once per collect_interval_ms
    assert rt.log.counters.snapshot()["connector_state_write_errors_total"]
```

`tests/test_config.py`: `state_path` absent → `DEFAULT_STATE_PATH`; `null` → `None`; a relative path → `RANGE`.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement `state.py`:**

```python
# SPDX-License-Identifier: MIT
"""The verdict state file (smart verdict retention design §4.4)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

STATE_VERSION = 1
DEFAULT_STATE_PATH = "/var/lib/lattice-ds-connector/verdicts.json"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def entry_from(key, v, now_mono: float, now_wall: datetime) -> Dict[str, Any]:
    a = v.published.assessment
    def wall_of(mono: float) -> str:
        from datetime import timedelta
        return _iso(now_wall - timedelta(seconds=max(0.0, now_mono - mono)))
    return {
        "instance": key.instance, "ds_id": key.ds_id, "binding_generation": key.binding_generation,
        "target_id": key.target_id, "expected_target_incarnation": key.expected_target_incarnation,
        "profile_id": v.profile_id, "allowed": a.allowed, "multiplier_ppm": a.multiplier_ppm,
        "reason_codes": list(a.reason_codes), "capacity_domain_id": a.capacity_domain_id,
        "shared_resource_ids": list(a.shared_resource_ids), "datastore_id": a.datastore_id,
        "target_incarnation": a.target_incarnation,
        "observed_at": wall_of(v.published.fetched_mono - (a.evidence_age_ms or 0) / 1000.0),
        "critical": v.critical, "critical_observed_at": wall_of(v.hold_origin_mono),
    }


def save(path: str, entries: List[Dict[str, Any]], runtime_epoch: str, now_wall: datetime) -> None:
    d = os.path.dirname(os.path.abspath(path))
    body = json.dumps({"version": STATE_VERSION, "written_at": _iso(now_wall),
                       "runtime_epoch": runtime_epoch, "verdicts": entries}, sort_keys=True).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".verdicts.", dir=d)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, body)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, path)
        dfd = os.open(d, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(path: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION or not isinstance(data.get("verdicts"), list):
            raise ValueError("unsupported state file shape or version")
        return [e for e in data["verdicts"] if isinstance(e, dict)], None
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as exc:
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        try:
            os.replace(path, "%s.corrupt-%s" % (path, stamp))
        except OSError:
            pass
        return [], "STATE_FILE_UNREADABLE: %s" % exc
```

`Runtime.__init__(..., wall_clock=None)`: `self._wall = wall_clock or (lambda: datetime.now(timezone.utc))`, `self._last_save_mono = None`, `self._saved_version = -1`; after `_build_instances`, when `config.runtime.state_path` is set, call `self._restore_state()`:

```python
    def _restore_state(self) -> None:
        entries, warning = state.load(self.config.runtime.state_path)
        if warning:
            self.log.warn("state_file_ignored", path=self.config.runtime.state_path, reason=warning)
        now_mono, now_wall = self._clock(), self._wall()
        by_key = {}
        for inst in self.config.instances:
            for b in inst.bindings:
                by_key[InstanceRuntime.static_key(inst.id, b)] = (inst, b)
        for e in entries:
            key = VerdictKey(e.get("instance"), e.get("ds_id"), e.get("binding_generation"), e.get("target_id"), e.get("expected_target_incarnation"))
            if key not in by_key:
                continue
            inst, b = by_key[key]
            ir = self.instances[inst.id]
            profile = ir.profile or fixture_profile()
            if e.get("profile_id") != profile.id:
                continue
            obs = _parse_iso(e.get("observed_at")); crit_obs = _parse_iso(e.get("critical_observed_at")) or obs
            if obs is None:
                continue
            origin_wall = crit_obs if e.get("critical") else obs
            age_s = max(0.0, (now_wall - origin_wall).total_seconds())     # a future stamp counts as age 0
            if age_s * 1000 >= profile.hold_ms(bool(e.get("critical"))):
                continue
            ir.restore(b, e, hold_origin_mono=now_mono - age_s, obs_age_ms=int(max(0.0, (now_wall - obs).total_seconds()) * 1000))
        for ir in self.instances.values():
            ir.republish_from_store()
```

`InstanceRuntime.restore(b, entry, hold_origin_mono, obs_age_ms)` rebuilds an `Assessment` (quality VALID, the stored fields, `evidence_age_ms=obs_age_ms`, `observed_at=entry["observed_at"]`), wraps it in a `PublishedAssessment` with `fetched_mono=now`, `hold_ms=profile.hold_ms(critical)`, `hold_origin_mono`, and puts a `StoredVerdict(..., restored=True)`. `republish_from_store()` replaces the initial unknown snapshot (sequence 0, `FAILED`, publish=False) by one whose records come from `_retained_or_unknown(b, "NO_ASSESSMENT", now)`.

`Runtime.save_state(force=False)`: returns when `state_path` is None; when not `force`, returns if `self.verdicts.version == self._saved_version` or less than `collect_interval_ms` passed since `_last_save_mono`; otherwise builds entries with `state.entry_from` for every live store item, calls `state.save`, catches `OSError` (`self.log.counters.inc("connector_state_write_errors_total", {})` + a limited WARN), and records `_saved_version`/`_last_save_mono` in both cases. `InstanceRuntime` gets an `on_change` callback (`Runtime` passes `self.save_state`) invoked at the end of `run_cycle` and `_on_collection_error`.

`config.py`: `RuntimeConfig.state_path: Optional[str] = None`; the parser: key absent → `state.DEFAULT_STATE_PATH`, `null` → `None`, a string that is not absolute → `RANGE`.

systemd unit: after `RuntimeDirectoryMode=0750` add

```ini
StateDirectory=lattice-ds-connector
StateDirectoryMode=0750
```

and `ReadWritePaths=/run/lattice-ds-connector /var/lib/lattice-ds-connector`.

- [ ] **Step 4: Docs** `README.md` (state file, `runtime.state_path`, what survives a restart), `docs/troubleshooting.md` (`RESTORED_FROM_STATE`, `STATE_FILE_UNREADABLE` → the renamed `.corrupt-<ts>` file, `connector_state_write_errors_total`), `examples/connector-config*.json` (`"state_path": "/var/lib/lattice-ds-connector/verdicts.json"`).

- [ ] **Step 5: Run** the connector suite → PASS; `python -m lattice_ds_connector validate-config --config examples/connector-config.json` → OK. **Commit** `feat(connector): persist verdicts in a state file; a restart restores them`.

### Task R5: The MDS verdict store (fork, `ds_connector.c`)

**Files:**
- Modify: `include/ds_connector.h`, `include/placement_gate.h` (`struct placement_assessment_row` gains `bool retained;`), `src/mds/ds_connector.c`, `tests/unit/test_ds_connector.c`, `docs/placement-modes.md`

**Interfaces:**
- Produces:

```c
struct ds_connector_verdict {
    bool     live;
    bool     allowed;
    bool     retained;
    uint32_t ppm;
    uint32_t binding_generation;
    uint64_t received_mono_ms;
    uint64_t expires_mono_ms;
    char     domain[PM_DOMAIN_ID_MAX];
    char     reasons[PA_REASONS_MAX][PA_REASON_LEN];
    uint32_t reason_count;
};
/* struct ds_connector_state gains: */
    struct ds_connector_verdict verdicts[MDS_MAX_DS_NODES];
    uint64_t verdicts_expired_total;
```

Row semantics after this task: `present == valid ==` "a live, unexpired verdict"; `retained` copied from the verdict.

- [ ] **Step 1: Failing tests** in `tests/unit/test_ds_connector.c` (use the existing `rec()`, `batch()`, `apply_at()` helpers; `rec(..., "VALID", ttl, …)` with a VALID record whose `reason_codes` must be settable — extend `rec()` with a `reasons_json` argument defaulting to `"[\"NORMAL\"]"` via a `rec_r()` variant):

```c
static void test_unknown_record_keeps_the_verdict(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply_at(batch("rt-1", "e1", 1, "2026-09-29T10:00:01Z", "c", "COMPLETE", rec_ok(0)), 1000000, &v, &rep), DC_OK);
    ASSERT_EQ(v.rows[0].present, true);
    const char *unk = rec(0, 2, "mnt/data", "null", "192.168.64.51", "/mnt/data", 2049,
                          "NEW_ALLOCATION", "cluster-default", "UNKNOWN", 0, "sha256:p", "false", 0, "null");
    ASSERT_EQ(apply_at(batch("rt-1", "e1", 2, "2026-09-29T10:00:02Z", "c", "COMPLETE", unk), 1005000, &v, &rep), DC_OK);
    ASSERT_EQ(v.rows[0].present, true);                       /* still the first verdict */
    ASSERT_EQ(v.rows[0].allowed, true);
    ASSERT_TRUE(v.rows[0].expires_mono_ms == 1015000ull);     /* its own TTL, not refreshed */
}

static void test_verdict_expires_on_its_ttl_and_is_counted(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply_at(batch("rt-1", "e1", 1, "2026-09-29T10:00:01Z", "c", "COMPLETE", rec_ok(0)), 1000000, &v, &rep), DC_OK);
    ASSERT_EQ(apply_at(batch("rt-1", "e1", 2, "2026-09-29T10:00:30Z", "c", "COMPLETE", ""), 1020000, &v, &rep), DC_OK);
    ASSERT_EQ(v.rows[0].present, false);                      /* 15 s TTL ran out */
    ASSERT_TRUE(ST.verdicts_expired_total == 1ull);
}

static void test_failed_snapshot_only_carries_retained_verdicts(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply_at(batch("rt-1", "e1", 1, "2026-09-29T10:00:01Z", "c", "FAILED", rec_ok(0)), 1000000, &v, &rep), DC_OK);
    ASSERT_EQ(v.rows[0].present, false);                      /* a fresh VALID in a FAILED snapshot: no */
    const char *ret = rec_r(0, 2, "mnt/data", "\"mnt/data:0:u\"", "192.168.64.51", "/mnt/data", 2049,
                            "NEW_ALLOCATION", "cluster-default", "VALID", 600000, "sha256:p", "false", 0, "null",
                            "[\"VERDICT_RETAINED\",\"SOURCE_TIMEOUT\",\"ARRAY_UNAVAILABLE\"]");
    ASSERT_EQ(apply_at(batch("rt-1", "e1", 2, "2026-09-29T10:00:02Z", "c", "FAILED", ret), 1001000, &v, &rep), DC_OK);
    ASSERT_EQ(v.rows[0].present, true);
    ASSERT_EQ(v.rows[0].allowed, false);
    ASSERT_EQ(v.rows[0].retained, true);
    ASSERT_TRUE(v.rows[0].expires_mono_ms == 1601000ull);
}

static void test_rebind_clears_and_epoch_reset_keeps(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply_at(batch("rt-1", "e1", 1, "2026-09-29T10:00:01Z", "c", "COMPLETE", rec_ok(0)), 1000000, &v, &rep), DC_OK);
    ASSERT_EQ(apply_at(batch("rt-2", "e1", 1, "2026-09-29T10:00:02Z", "c", "COMPLETE", ""), 1001000, &v, &rep), DC_OK);
    ASSERT_EQ(v.rows[0].present, true);                       /* connector restart: kept */
    const char *gen3 = rec(0, 3, "mnt/data", "null", "192.168.64.51", "/mnt/data", 2049,
                           "NEW_ALLOCATION", "cluster-default", "UNKNOWN", 0, "sha256:p", "false", 0, "null");
    ASSERT_EQ(apply_at(batch("rt-2", "e1", 2, "2026-09-29T10:00:03Z", "c", "COMPLETE", gen3), 1002000, &v, &rep), DC_OK);
    ASSERT_EQ(v.rows[0].present, false);                      /* rebind announced: cleared */
}
```

Update the existing tests whose expectations encoded "an UNKNOWN / FAILED record makes the row present but invalid" (`test_unknown_duplicate_failed_and_shape`, `test_binding_rules_in_a_batch` rebound part, `test_unobserved_incarnation_on_unknown_records`, `test_steady_state_refreshes_the_ttl`): a row is now `present` only with a live verdict; an UNKNOWN record after a VALID one leaves the VALID verdict in place.

- [ ] **Step 2: Run** `bash mds/scripts/pm-run.sh test_ds_connector` → FAIL.

- [ ] **Step 3: Implement** in `ds_connector_apply_batch`: allocate `struct ds_connector_verdict *new_verdicts = calloc(MDS_MAX_DS_NODES, sizeof(*new_verdicts))`, `memcpy` from `st->verdicts` (also on an epoch reset); first expire: for every id with `live && now_mono_ms >= expires_mono_ms` clear it and count `expired++`. In the record loop, after the pin/rebind checks where the code now does `rep->accepted++`:

```c
            if (rebound) {
                memset(&new_verdicts[r.ds_id], 0, sizeof(new_verdicts[r.ds_id]));   /* spec rule 5 */
            }
            {
                bool retained = rec_has_reason(&r, "VERDICT_RETAINED");
                bool counts = r.valid && !rebound && (!upd[i].failed_snapshot || retained);

                if (counts) {
                    struct ds_connector_verdict *vd = &new_verdicts[r.ds_id];
                    memset(vd, 0, sizeof(*vd));
                    vd->live = true;
                    vd->allowed = r.allowed;
                    vd->retained = retained;
                    vd->ppm = (uint32_t)r.ppm;
                    vd->binding_generation = (uint32_t)r.binding_generation;
                    vd->received_mono_ms = now_mono_ms;
                    vd->expires_mono_ms = now_mono_ms + r.remaining_ttl_ms;
                    memcpy(vd->domain, r.domain, sizeof(vd->domain));
                    memcpy(vd->reasons, r.reasons, sizeof(vd->reasons));
                    vd->reason_count = r.reason_count;
                }
            }
```

(`rec_has_reason` is a new static helper over `r.reasons[0..reason_count)`; `struct rec` must keep reasons, which it already does; a VALID record with `remaining_ttl_ms == 0` is not live.) Remove the old per-record `row->…` writes; after the loop build every `out->rows[i]` for registry DS from `new_verdicts[reg->ds[i].ds_id]`: `present = valid = live && now < expires`, plus `allowed`, `multiplier_ppm`, `expires_mono_ms`, `received_mono_ms`, `domain`, `reasons`, `reason_count`, `retained`. On `DC_OK` commit `memcpy(st->verdicts, new_verdicts, …)` and `st->verdicts_expired_total += expired`; free `new_verdicts` on both paths.

- [ ] **Step 4: Docs** fork `docs/placement-modes.md` connector-client section: "the MDS keeps the last VALID verdict per data store across batches; an UNKNOWN record, a rejected record or a dropped batch never replaces it; a FAILED instance snapshot contributes only `VERDICT_RETAINED` records; a rebind clears; a connector restart keeps".

- [ ] **Step 5: Run** `pm-run.sh "test_ds_connector"` then `pm-run.sh all` → PASS. **Commit** `feat(ds_connector): keep the last verdict per data store; no-new-data never overwrites it`.

### Task R6: Neutral instead of refusal in the gate (fork)

**Files:**
- Modify: `src/fsal_obj/placement_gate.c`, `include/placement_gate.h` (`struct placement_reject_counts` gains `uint32_t neutral; uint32_t retained;`), `docs/placement-modes.md`
- Test: `tests/unit/test_placement_gate.c`, `tests/unit/test_placement_create_boundary.c`, `tests/unit/test_ds_connector.c` (the poll test)

**Interfaces:**
- Consumes: R5's `placement_assessment_row.retained`, `present` = live.
- Produces: in `smart` no data store is refused for lack of a verdict; `placement_reject_counts.neutral/retained` filled per decision.

- [ ] **Step 1: Failing tests** (`tests/unit/test_placement_gate.c`, with the existing `reset_smart`, `add_assess`, `smart_ctx`, `add_row`, `mk_ds` fixtures):

```c
static void test_smart_without_a_verdict_is_neutral(void)
{
    struct mds_ds_info ds[2];
    reset_smart();
    mk_ds(&ds[0], 0, DS_ONLINE, "a"); mk_ds(&ds[1], 1, DS_ONLINE, "b");
    add_row(0, "a", 1000, 800, 1, 1000000); add_row(1, "b", 1000, 800, 2, 1000000);
    add_assess(0, true, true, 250000, 15000, "d-a");            /* ds 0 degraded, ds 1 never reported */
    struct placement_ctx c = smart_ctx();
    struct placement_candidate out[2]; struct placement_reject_counts why;
    memset(&why, 0, sizeof(why));
    ASSERT_EQ(placement_candidates(&c, ds, 2, out, &why), 2u);
    ASSERT_TRUE(out[1].weight == placement_weight(80, 1000000, 1, NULL));   /* neutral = fill weight */
    ASSERT_TRUE(out[0].weight == placement_weight(80, 250000, 1, NULL));
    ASSERT_EQ(why.neutral, 1u);
    ASSERT_EQ(why.by_reason[PR_NO_BINDING], 0u);
}

static void test_smart_expired_verdict_is_neutral_and_live_deny_excludes(void)
{
    struct mds_ds_info ds[2];
    reset_smart();
    mk_ds(&ds[0], 0, DS_ONLINE, "a"); mk_ds(&ds[1], 1, DS_ONLINE, "b");
    add_row(0, "a", 1000, 800, 1, 1000000); add_row(1, "b", 1000, 800, 2, 1000000);
    add_assess(0, true, false, 0, 15000, "d-a");               /* live deny */
    add_assess(1, true, false, 0, 500, "d-b");                 /* received 999000 + ttl 500: expired at 999500 */
    struct placement_ctx c = smart_ctx();
    struct placement_candidate out[2]; struct placement_reject_counts why;
    memset(&why, 0, sizeof(why));
    ASSERT_EQ(placement_candidates(&c, ds, 2, out, &why), 1u);
    ASSERT_EQ(out[0].ds_id, 1u);
    ASSERT_EQ(why.by_reason[PR_CONNECTOR_DENIED], 1u);
    ASSERT_EQ(why.neutral, 1u);
}

static void test_smart_without_any_view_places_like_fill(void)
{
    struct mds_ds_info ds[1];
    reset_smart();
    mk_ds(&ds[0], 0, DS_ONLINE, "a");
    add_row(0, "a", 1000, 500, 1, 1000000);
    struct placement_ctx c = smart_ctx();
    c.assess = NULL;                                           /* no batch since start */
    struct placement_candidate out[1]; struct placement_reject_counts why;
    memset(&why, 0, sizeof(why));
    ASSERT_EQ(placement_candidates(&c, ds, 1, out, &why), 1u);
    ASSERT_EQ(why.by_reason[PR_MODE_NOT_READY], 0u);
    ASSERT_TRUE(out[0].weight == placement_weight(50, 1000000, 1, NULL));
}

static void test_smart_retained_verdict_is_counted(void)
{
    struct mds_ds_info ds[1];
    reset_smart();
    mk_ds(&ds[0], 0, DS_ONLINE, "a");
    add_row(0, "a", 1000, 500, 1, 1000000);
    add_assess(0, true, true, 1000000, 600000, "d-a");
    A.rows[0].retained = true;
    struct placement_ctx c = smart_ctx();
    struct placement_candidate out[1]; struct placement_reject_counts why;
    memset(&why, 0, sizeof(why));
    ASSERT_EQ(placement_candidates(&c, ds, 1, out, &why), 1u);
    ASSERT_EQ(why.retained, 1u);
}
```

Rewrite the tests that assert refusal for lack of data: `test_smart_without_assessments_is_not_ready`, `test_smart_without_view_is_not_ready`, the `NO_BINDING`/`ASSESSMENT_UNKNOWN`/`ASSESSMENT_STALE` parts of `test_smart_reasons`, the `MODE_NOT_READY` part of `test_singleton_smart_init_and_readiness` (now `placement_select_gated` → `MDS_OK` before any view), and in `tests/unit/test_placement_create_boundary.c` the first assertion of `test_admit_create_refuses_a_connector_denied_ds` (before any view: `MDS_OK`). The alias/domain tests keep their expectations; `test_smart_connector_domain_and_map_mismatch` gains a case: an **expired** row's domain is not used (the operator map / `ds:<id>` applies, no `DOMAIN_MAP_MISMATCH`).

- [ ] **Step 2: Run** `pm-run.sh "test_placement_gate|test_placement_create_boundary"` → FAIL.

- [ ] **Step 3: Implement** in `candidates_weighted`: delete the `if (ctx->mode == PM_SMART && ctx->assess == NULL) { … PR_MODE_NOT_READY … }` block; in the domain loop use the connector domain only for a live row:

```c
            if (ar != NULL && ar->present && ar->valid && ctx->now_mono_ms < ar->expires_mono_ms &&
                ar->domain[0] != '\0') {
```

replace the smart branch after the capacity gate by:

```c
        if (ctx->mode == PM_SMART) {
            const struct placement_assessment_row *ar = assess_row(ctx->assess, ds_list[i].ds_id);
            bool live = ar != NULL && ar->present && ar->valid && ctx->now_mono_ms < ar->expires_mono_ms;
            uint32_t manual = 0;

            if (!live) {
                if (why != NULL) {
                    why->neutral++;            /* spec rule 1/3: neutral, ppm stays 1000000 */
                }
            } else {
                if (!ar->allowed) {
                    count_reason(why, PR_CONNECTOR_DENIED);
                    continue;
                }
                if (ar->multiplier_ppm == 0) {
                    count_reason(why, PR_ZERO_MULTIPLIER);
                    continue;
                }
                ppm = ar->multiplier_ppm;
                if (ar->retained && why != NULL) {
                    why->retained++;
                }
                if (manual_domain_weight(ctx, rs[r_i].domain, &manual)) {
                    domain_weight = manual;
                }
            }
        }
```

In `placement_admit` the `n_c == 0` reason becomes `PR_NO_ELIGIBLE_DS` unconditionally. `placement_gate_note_rejections` stores `why->neutral`/`why->retained` into the gauges added in R7 (until R7, ignore them). Keep the enum values `PR_NO_BINDING`, `PR_ASSESSMENT_UNKNOWN`, `PR_ASSESSMENT_STALE`, `PR_MODE_NOT_READY`.

- [ ] **Step 4: Docs** fork `docs/placement-modes.md` candidate rule for `smart`: "no verdict in force → neutral (multiplier 1, the `fill` weight, the operator's domain); a live deny or zero multiplier excludes; a live allow weights by its multiplier; `MODE_NOT_READY` is no longer produced".

- [ ] **Step 5: Run** `pm-run.sh all` → PASS. **Commit** `feat(placement): smart places neutrally without a verdict in force`.

### Task R7: Observability — readiness, `config show`, metrics (fork)

**Files:**
- Modify: `include/placement_gate.h` (`struct placement_readiness` gains `uint32_t retained_ds, neutral_ds;`; `struct placement_ds_status` gains `char verdict[12]; uint64_t hold_left_ms;`), `src/fsal_obj/placement_gate.c` (`placement_gate_readiness`, `placement_gate_ds_status`, `placement_gate_note_rejections`), `src/cluster/cluster_transport.c`, `include/mds_metrics.h`, `src/common/mds_metrics.c`, `src/mds/ds_connector.c` (expired counter into metrics), `docs/placement-modes.md`
- Test: `tests/unit/test_cluster_transport.c`, `tests/unit/test_placement_gate.c`

**Interfaces:**
- Produces: `placement_readiness = … eligible_ds=… retained_ds=… neutral_ds=…`; `placement_ds.<id> = … verdict=fresh|retained|none hold_left_ms=<n>|none …`; for a neutral data store `quality=NONE allowed=- ppm=1000000`; metrics `pnfs_mds_placement_neutral_ds`, `pnfs_mds_placement_retained_ds` (gauges), `pnfs_mds_connector_verdicts_expired_total` (counter).

- [ ] **Step 1: Failing tests.** `test_cluster_transport.c::test_config_show_placement_rows` (the smart part): with one live retained row for ds 0 and nothing for ds 1, assert the substrings `"retained_ds=1 neutral_ds=1"`, `"placement_ds.0 = "` … `"verdict=retained hold_left_ms="`, `"placement_ds.1 = "` … `"verdict=none hold_left_ms=none"` and `"allowed=- ppm=1000000"`. `test_placement_gate.c::test_singleton_smart_init_and_readiness`: after publishing a view with one live row of two registered DS, `r.retained_ds == 0`, `r.neutral_ds == 1`, and `placement_gate_ds_status(1, &st)` gives `strcmp(st.verdict, "none") == 0`, `st.hold_left_ms == 0`, `st.weight > 0`, `st.reason == PR_NONE`.

- [ ] **Step 2: Run** `pm-run.sh "test_cluster_transport|test_placement_gate"` → FAIL.

- [ ] **Step 3: Implement.** Readiness: keep `covered_ds` (live rows), add `retained_ds` (live and `retained`), `neutral_ds = registered_ds − covered_ds`. `placement_gate_ds_status`: `verdict` = `"fresh"`/`"retained"` for a live row, `"none"` otherwise; `hold_left_ms = expires − now` for a live row, 0 otherwise; for a neutral DS set `assessment_allowed` display to "-" by a new `bool neutral` in the status and let the renderer print `allowed=-` and `ppm=1000000`. `cluster_transport.c`: append ` retained_ds=%u neutral_ds=%u` to the readiness row and ` verdict=%s hold_left_ms=%s` to each `placement_ds.<id>` row (`hold_left_ms` as `none` when not live). Metrics: in `struct mds_branch_metrics` add `_Atomic uint64_t placement_neutral_ds, placement_retained_ds, connector_verdicts_expired_total;`, render them next to `pnfs_mds_placement_eligible_ds` / the connector block; `placement_gate_note_rejections` stores `neutral`/`retained`; `ds_connector_poll_once` adds the per-batch expired count to `connector_verdicts_expired_total`.

- [ ] **Step 4: Docs** fork `docs/placement-modes.md` observability section: the new readiness keys, the per-DS `verdict`/`hold_left_ms`, the gauges and counter, and "alert on `pnfs_mds_connector_reachable == 0` and on `pnfs_mds_placement_neutral_ds` growing while it was 0".

- [ ] **Step 5: Run** `pm-run.sh all` → PASS; push the fork branch; watch `placement-modes.yml`; open PR `xinnor/smart-verdict-retention` → `xinnor/placement-modes`. In pNFS: `scripts/export-patches.sh` (with the fork checkout on the PR branch head) and commit the patches + `mds/manifest.json`. **Commit** (fork) `feat(placement): report verdict and hold per data store; neutral and retained gauges`.

### Task R8: Helper — steering outages are warnings (pNFS `tools/lattice-placement`)

**Files:**
- Modify: `lattice_placement/live.py` (`Readiness.retained_ds/neutral_ds`, `DsRow.verdict/hold_left_ms`, `allowed=-` → `None`), `lattice_placement/verify.py`, `lattice_placement/validate.py` (`fold_preflight`), tests `test_live.py`, `test_verify.py`, `test_validate.py`, `test_cli_validate.py`, `test_cli_show_verify.py`, new fixture `tests/fixtures/config-show-smart-retention.json`
- Docs: `tools/lattice-placement/README.md`

**Interfaces:**
- Consumes: R7's `config show` rows.
- Produces: `verdict()` rules below; `fold_preflight` adds connector problems to `warnings`.

- [ ] **Step 1: Failing tests** (`tests/test_verify.py`):

```python
def test_no_coverage_and_unreachable_are_warnings_now():
    a = load("config-show-smart-steering-off.json", "m1")         # reachable=0, coverage=none, a valid profiles row
    v = verdict([a])
    assert v.exit_code == EXIT_OK
    assert any(w.startswith("STEERING_OFF:m1") for w in v.warnings)
    assert any(w.startswith("CONNECTOR_UNREACHABLE:m1") for w in v.warnings)
    assert verdict([a], require_full_coverage=True).exit_code == EXIT_DIFFER


def test_retained_verdicts_fail_only_full_coverage():
    s = load("config-show-smart-retention.json", "m1")            # ds 0 retained, ds 1 neutral
    assert s.readiness.retained_ds == 1 and s.readiness.neutral_ds == 1
    assert s.ds[0].verdict == "retained" and s.ds[1].verdict == "none" and s.ds[1].allowed is None
    v = verdict([s])
    assert v.exit_code == EXIT_OK and any(w.startswith("COVERAGE_PARTIAL:m1") for w in v.warnings)
    v = verdict([s], require_full_coverage=True)
    assert v.exit_code == EXIT_DIFFER and any(e.startswith("COVERAGE_RETAINED:m1") for e in v.errors)
```

and in `tests/test_validate.py`: `fold_preflight(rep_smart, None)` → `ready` stays True, `warnings` contains `CONNECTOR:UNAVAILABLE …`; a not-ready preflight → warnings, not errors. The fixture `config-show-smart-retention.json` is `config-show-smart-mds2.json` with `placement_readiness = … coverage=partial registered_ds=2 covered_ds=1 eligible_ds=1 retained_ds=1 neutral_ds=1`, `placement_ds.0 = … quality=VALID allowed=1 ppm=1000000 ttl_ms=412000 weight=6422528000000 reason=NONE verdict=retained hold_left_ms=412000`, `placement_ds.1 = domain=ds:1 state=ONLINE … quality=NONE allowed=- ppm=1000000 ttl_ms=0 weight=6422528000000 reason=NONE verdict=none hold_left_ms=none`. A second fixture `config-show-smart-steering-off.json` is the same file with `placement_readiness = mode_active=1 connector_config_valid=1 connector_reachable=0 last_batch_valid=0 coverage=none registered_ds=2 covered_ds=0 eligible_ds=2 retained_ds=0 neutral_ds=2`, both rows `verdict=none hold_left_ms=none allowed=- ppm=1000000`, and `placement_connector_last_detail = socket /run/lattice-ds-connector/connector.sock: absent or refused`; it keeps the valid `placement_connector_profiles` row of `config-show-smart-mds2.json` so no profile-map error interferes.

- [ ] **Step 2: Run** `python -m pytest -q` in `tools/lattice-placement` → FAIL.

- [ ] **Step 3: Implement** `verify.verdict` smart branch:

```python
            if not r.connector_reachable:
                v.warnings.append("CONNECTOR_UNREACHABLE:%s: %s" % (s.host, s.last_detail or ""))
            if r.coverage == "none":
                msg = "STEERING_OFF:%s: no data store has a verdict in force; placing by fill weights" % s.host
                (v.errors if require_full_coverage else v.warnings).append(msg)
            elif r.coverage == "partial":
                bad = ["ds %d %s" % (row.ds_id, row.verdict or row.reason) for row in s.ds if row.verdict != "fresh" and row.verdict != "retained"]
                msg = "COVERAGE_PARTIAL:%s: %d of %d DS have a verdict in force (neutral: %s)" % (s.host, r.covered_ds, r.registered_ds, ", ".join(bad))
                (v.errors if require_full_coverage else v.warnings).append(msg)
            if require_full_coverage and r.retained_ds > 0:
                v.errors.append("COVERAGE_RETAINED:%s: %d DS held by retained verdicts (the connector is not observing them)" % (s.host, r.retained_ds))
```

(`CONNECTOR_CONFIG_INVALID`, the profile-map errors, `DESIRED_NE_EFFECTIVE`, the differences and `MDS_UNREADABLE` stay errors.) `validate.fold_preflight`: append `CONNECTOR:…` to `report.warnings` and leave `report.ready` as computed from errors. `render_row`: `verdict=%s hold_left=%s`; the readiness line adds `retained=%d neutral=%d`.

- [ ] **Step 4: Docs** `tools/lattice-placement/README.md` exit-code table and the `verify` paragraph.

- [ ] **Step 5: Run** the helper suite → PASS. **Commit** `feat(lattice-placement): a smart cluster without steering is a warning; retained verdicts only fail full coverage`.

### Task R9: Cross-cutting docs and the contract manifest (pNFS)

**Files:**
- Modify: `docs/placement-modes/contract-manifest.json` and `tools/lattice-placement/lattice_placement/data/contract-manifest.json` (identical copy), `docs/superpowers/specs/2026-09-23-placement-modes-design.md`, `docs/placement-modes/operations.md`, `docs/TODO.md`, `README.md`

- [ ] **Step 1: Manifest** — `connector_client`: `"contract_version": "1.1"`, `"ttl_max_ms": 3600000`, a `"retention_rule"` string ("a VALID verdict stays in force for remaining_ttl_ms without a new one; UNKNOWN/rejected/dropped never replace it; a FAILED snapshot carries only VERDICT_RETAINED records; a rebind clears; no verdict in force = neutral (1 000 000 ppm, operator domain)"), `"hold_defaults_ms": {"critical": 1200000, "other": 600000}`, `"new_reason_codes": ["VERDICT_RETAINED", "RESTORED_FROM_STATE"]`; `config_show_keys`: note the readiness keys `retained_ds`, `neutral_ds` and the row fields `verdict`, `hold_left_ms`; `metrics` += `pnfs_mds_placement_neutral_ds`, `pnfs_mds_placement_retained_ds`, `pnfs_mds_connector_verdicts_expired_total`; `cli.verify_exit` "1" text: remove "connector invalid/unreachable, coverage none" from the failure list except `connector_config_valid=0`, add "partial, none or retained coverage with --require-full-coverage". Copy to the bundled path.
- [ ] **Step 2: Design spec 2026-09-23** — in §5 (candidate rule), §7 ("Readiness", candidate rule, "no fallback" wording) and §13 (the smart acceptance rows) add a line "Superseded for smart by `2026-09-29-smart-verdict-retention-design.md` (2026-09-29): no verdict in force means neutral, not refusal." and replace the `MODE_NOT_READY` sentence.
- [ ] **Step 3: `operations.md`** — "What neutral means" (a data store without a verdict in force is placed like in fill; the capacity gate and the DS state still apply), the alerts (`pnfs_mds_connector_reachable == 0`; `pnfs_mds_placement_neutral_ds` rising), the profile-pin update after the profile digest change (`lattice-placement mode set smart --set ds_connector_expected_profiles=…` on every MDS in the same window), the reasons table rows for `VERDICT_RETAINED`, `RESTORED_FROM_STATE`, `verdict=none`.
- [ ] **Step 4: `docs/TODO.md`** — close "A connector just (re)started reports its DS … RECOVERY_HOLD_DOWN" (R3 fixed it); add nothing else unless a task deferred something.
- [ ] **Step 5: Run** `python3 scripts/check-manifests.py --fork ~/Documents/GitHub/pnfs-lattice` (fork checkout on the R7 head) → ok; both pytest suites → PASS; push `feat/smart-verdict-retention`, open the pNFS PR, watch CI. **Commit** `docs(placement-modes): verdict retention in the manifest, the live spec, the runbook`.

### Task R10: Stand acceptance on the lab (announced window; restarts both MDS and both connectors)

Pre-condition: Sergey's go-ahead in chat. From xinas-box with the Stage C scripts (`pm-deploy.sh`, `pm-trial.sh`, `pm-watch.sh`, `pm-connector-install.sh`, `pm-bench.sh`).

**Files:**
- Create: `docs/placement-modes/stand-<run date>-retention.md`
- Modify: `mds/scripts/pm-watch.sh` only if a new row pattern is needed

- [ ] **Step 1: MDS first.** `pm-run.sh build` on the R7 head, `pm-deploy.sh smart "ds_connector_poll_ms=1000;ds_connector_request_deadline_ms=500"` (the current connectors still speak 1.0). Expect `verify` exit 0; DS 1 now `verdict=none` (neutral) instead of `NO_BINDING`.
- [ ] **Step 2: Connectors second** with shortened holds for the stand: edit `/etc/lattice-ds-connector/config.json` on node225 (`profiles[0].critical_hold_ms = 180000`, `verdict_hold_ms = 90000`, `runtime.state_path` default), then `pm-connector-install.sh 192.168.65.225 192.168.65.225` and `pm-connector-install.sh 192.168.65.223 192.168.65.225` (same tree, same config). If `mds.conf` pins `ds_connector_expected_profiles`, update the digest on both MDS with `mode set smart --set ds_connector_expected_profiles=…` + restart; `verify` exit 0.
- [ ] **Step 3: Trials** (record each with `pm-watch.sh` rows and file counts):
  1. 40 × 4 MiB via MDS 2 → ≈ 20 : 20 (DS 0 fresh normal, DS 1 neutral).
  2. DS 0 critical: add `"10.99.0.0/24"` to DS 0's `expected_client_networks` on both nodes, `systemctl reload lattice-ds-connector` → `EXPORT_ACCESS_MISSING`, `verdict=fresh allowed=0`; 20 files → 0 : 20.
  3. `systemctl stop xinas-agent` on the box → DS 0 `verdict=retained`, `reason_codes` start `VERDICT_RETAINED`; 20 files → 0 : 20 during the 180 s; after it `verdict=none` and 20 files ≈ 10 : 10 (DS 0 neutral); `systemctl start xinas-agent`.
  4. Re-create the critical (agent up), stop the agent again, then `systemctl restart lattice-ds-connector` on node225 inside the hold → `RESTORED_FROM_STATE`, still 0 files on DS 0.
  5. Inside a fresh hold: `systemctl restart pnfs-mds` on node225 → the connector re-publishes the retained deny; still 0 on DS 0.
  6. Inside a fresh hold: stop the connector on node225 → the MDS keeps the deny until its TTL, then neutral; no ENOSPC anywhere.
  7. Restore: remove `10.99.0.0/24`, set holds back to the defaults (delete the two keys), reinstall on both nodes, `pm-deploy.sh --no-binary legacy`, `verify` exit 0; one `pm-bench.sh` run per mode (gate mean within a few µs of Stage C).
- [ ] **Step 4: Report** `docs/placement-modes/stand-<date>-retention.md` with the rows, timings and the restored lab state; commit to `feat/smart-verdict-retention`; update the memory note.

## Self-review

- Spec coverage: §1/§3 rules → R2 (store, retained, errors), R3 (hold-down), R5 (MDS keep/expire/rebind/epoch), R6 (neutral, no `MODE_NOT_READY`, capacity gate untouched, `DOMAIN_MAP_MISMATCH` live-only); §4.1 → R1; §4.2/4.3 → R2/R3; §4.4 → R4; §4.5 → R1 (contract), R2 (preflight); §5.1 → R5; §5.2 → R6; §5.3 → R7; §6 → R8; §7 → R1–R4 (connector docs), R5–R7 (fork doc), R9 (manifest, design spec, runbook, TODO); §8 → R10 steps 1–2 and R9 runbook; §9 → the tests per task and R10; §10 respected (no MDS persistence, two hold classes only).
- Placeholders: helper names used in tests exist or are introduced in the same step (`runtime_helpers.py`, `rec_r`, `rebind`, `rec_has_reason`); the one instruction to reuse an existing builder in `tests/test_config.py` names the exact role of the helper.
- Type consistency: `VerdictKey`/`StoredVerdict`/`VerdictStore` (R2) used by R3/R4; `PublishedAssessment.hold_ms/hold_origin_mono/retained/restored/retained_cause` (R2) used by R4; `struct ds_connector_verdict` and `row.retained` (R5) used by R6/R7; `placement_reject_counts.neutral/retained` (R6) used by R7; `Readiness.retained_ds/neutral_ds`, `DsRow.verdict/hold_left_ms` (R8) match R7's rows.
