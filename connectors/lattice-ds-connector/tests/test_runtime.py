# SPDX-License-Identifier: MIT
"""Runtime semantics: freshness/TTL, retained verdicts, epochs, sequences,
hold-down, worker isolation, batch limits (T-18..T-22, T-29, T-33)."""

import io
import json
import threading
import time

import pytest

import source_builder as sb
from conftest import make_binding, make_profile, validate
from lattice_ds_connector import contract
from lattice_ds_connector.config import Instance, RuntimeConfig
from lattice_ds_connector.log import Logger
from lattice_ds_connector.modules.base import CollectionError, Module, SourceBatch
from lattice_ds_connector.runtime import BatchTooLarge, Runtime
from runtime_helpers import FakeClock, ScriptedModule, make_config, make_instance, make_runtime, records, settle

HOLD_OTHER = contract.DEFAULT_VERDICT_HOLD_MS
HOLD_CRIT = contract.DEFAULT_CRITICAL_HOLD_MS


# ---------------------------------------------------------------------------
# Start-up and the batch shape
# ---------------------------------------------------------------------------


def test_start_is_all_unknown_and_the_batch_validates(schemas):
    clock = FakeClock()
    rt = make_runtime(clock, {"xi-01": ScriptedModule(clock)})
    inst, recs = records(rt)
    assert inst["sequence"] == 0 and inst["snapshot_status"] == "FAILED"
    for rec in recs.values():
        assert rec["quality"] == "UNKNOWN" and rec["placement"] == {"allowed": False, "multiplier_ppm": 0, "reason_codes": ["NO_ASSESSMENT"]}
        assert rec["remaining_ttl_ms"] == 0 and rec["evidence_age_ms"] is None and rec["observed_at"] is None
        assert rec["endpoint"]["export_path"].startswith("/mnt/")
    validate(schemas["batch"], rt.batch())
    assert rt.health()["ready"] is False


def test_first_collect_publishes_at_once(schemas):
    clock = FakeClock()
    rt = make_runtime(clock, {"xi-01": ScriptedModule(clock)})
    ir = rt.instances["xi-01"]
    ir.run_cycle()
    inst, recs = records(rt)
    assert inst["sequence"] == 1 and inst["snapshot_status"] == "COMPLETE"
    a = recs[0]
    # No deny was in force: the first verdict after a start is not held down (design rule 6).
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is True
    assert a["placement"]["multiplier_ppm"] == contract.PPM_FULL
    assert a["placement"]["reason_codes"] == ["NORMAL"]
    assert "hold_down" not in a["diagnostics"]
    assert a["target_incarnation"] == "training-a:7" and a["resources"]["capacity_domain_id"]
    validate(schemas["batch"], rt.batch())
    assert rt.health()["ready"] is True
    clock.advance(5.0)
    ir.run_cycle()
    inst, recs = records(rt)
    assert recs[0]["placement"]["allowed"] is True and inst["sequence"] == 2
    validate(schemas["batch"], rt.batch())


# ---------------------------------------------------------------------------
# T-18: aging, TTL, same snapshot repeated, GET does not refresh
# ---------------------------------------------------------------------------


def test_t18_ages_grow_and_expire_without_a_new_collect():
    clock = FakeClock()
    rt = make_runtime(clock, {"xi-01": ScriptedModule(clock)})
    settle(rt, clock, cycles=3)
    _, recs = records(rt)
    age0, ttl0 = recs[0]["evidence_age_ms"], recs[0]["remaining_ttl_ms"]
    assert recs[0]["placement"]["allowed"]
    clock.advance(2.0)
    _, recs = records(rt)
    assert recs[0]["evidence_age_ms"] == age0 + 2000 and recs[0]["remaining_ttl_ms"] == ttl0 - 2000
    assert recs[0]["placement"]["allowed"]
    # The TTL is the verdict's hold minus its age, not source_max_age_ms:
    # 30 s later (the old 20 s expiry is long past) the allow still holds.
    clock.advance(30.0)
    _, recs = records(rt)
    assert recs[0]["placement"]["allowed"] and recs[0]["remaining_ttl_ms"] == ttl0 - 32000
    assert ttl0 + age0 == HOLD_OTHER
    clock.advance(HOLD_OTHER / 1000.0)
    inst, recs = records(rt)
    assert recs[0]["remaining_ttl_ms"] == 0
    assert recs[0]["quality"] == "UNKNOWN" and recs[0]["placement"] == {"allowed": False, "multiplier_ppm": 0, "reason_codes": ["EVIDENCE_EXPIRED", "NORMAL"]}
    # The sequence did not move: a GET is not positive evidence.
    assert inst["sequence"] == 3


def test_t18_same_source_snapshot_repeated_is_not_a_new_sample():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    ir = rt.instances["xi-01"]
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["offline"]))
    ir.run_cycle()  # a deny is in force, so the recovery is held down
    clock.advance(1.0)
    ir.run_cycle()  # healthy: the first sample of the hold-down
    first_seq = records(rt)[0]["sequence"]
    for _ in range(4):
        clock.advance(4.0)
        mod.push(sb.base_result(), same_generation=True)
        ir.run_cycle()
    inst, recs = records(rt)
    assert recs[0]["placement"]["allowed"] is False
    assert recs[0]["placement"]["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    assert recs[0]["diagnostics"]["hold_down"]["distinct_cycles"] == 1
    # Audit C-05: a replayed generation is ignored outright — the sequence
    # does not advance and the counter records it.
    assert inst["sequence"] == first_seq
    assert sum(rt.log.counters.snapshot().get("connector_source_replay_total", {}).values()) == 4
    # A frozen source also ages on its own evidence (the source's age grows):
    # the hold counts from the observation, so an old sample has less left.
    frozen = sb.base_result()
    for s in frozen["shares"]:
        s["evidence_age_ms"] = 19000
    mod.push(frozen)
    ir.run_cycle()
    _, recs = records(rt)
    assert recs[0]["quality"] == "VALID" and recs[0]["placement"]["allowed"] is True
    assert recs[0]["evidence_age_ms"] >= 19000
    assert recs[0]["remaining_ttl_ms"] == HOLD_OTHER - recs[0]["evidence_age_ms"]


# ---------------------------------------------------------------------------
# T-19: no failure revokes; the last verdict holds for its hold time
# ---------------------------------------------------------------------------


def test_t19_transport_timeout_retains_until_original_expiry():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    settle(rt, clock, cycles=3)
    inst_before, recs = records(rt)
    assert recs[0]["placement"]["allowed"]
    ttl_before = recs[0]["remaining_ttl_ms"]
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "boom", retryable=True))
    clock.advance(1.0)
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["sequence"] == inst_before["sequence"] + 1 and inst["snapshot_status"] == "FAILED"
    assert recs[0]["placement"]["allowed"] and recs[0]["remaining_ttl_ms"] == ttl_before - 1000
    assert recs[0]["placement"]["reason_codes"] == ["VERDICT_RETAINED", "SOURCE_TIMEOUT", "NORMAL"]
    assert rt.health()["instances"]["xi-01"]["last_error"] == "SOURCE_TIMEOUT"
    clock.advance(25.0)
    _, recs = records(rt)
    assert recs[0]["quality"] == "VALID" and recs[0]["placement"]["allowed"]   # the old 20 s expiry is gone
    clock.advance(ttl_before / 1000.0)
    _, recs = records(rt)
    assert recs[0]["quality"] == "UNKNOWN" and recs[0]["placement"]["reason_codes"][0] == "EVIDENCE_EXPIRED"
    assert "VERDICT_RETAINED" not in recs[0]["placement"]["reason_codes"]


@pytest.mark.parametrize("code", ["SOURCE_AUTH_FAILED", "SOURCE_SCHEMA_INVALID", "SOURCE_NOT_READY", "SOURCE_STALE", "SNAPSHOT_TOO_LARGE"])
def test_t19_non_retryable_failure_retains_and_alerts(code):
    clock = FakeClock()
    mod = ScriptedModule(clock)
    log = io.StringIO()
    rt = make_runtime(clock, {"xi-01": mod}, logger=Logger(stream=log))
    settle(rt, clock, cycles=3)
    mod.push(error=CollectionError(code, "x", retryable=False))
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "FAILED" and inst["sequence"] == 4
    assert all(r["quality"] == "VALID" and r["placement"]["allowed"] for r in recs.values())
    assert all(r["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", code] for r in recs.values())
    lines = [json.loads(line) for line in log.getvalue().splitlines()]
    failed = [x for x in lines if x["event"].startswith("collect_failed")]
    assert [x["event"] for x in failed] == ["collect_failed_retained"]
    assert failed[0]["level"] == "error" and failed[0]["alert"] is True and failed[0]["code"] == code
    assert failed[0]["retained"] == 3


def test_t19_retryable_failure_warns_without_an_alert():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    log = io.StringIO()
    rt = make_runtime(clock, {"xi-01": mod}, logger=Logger(stream=log))
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    rt.instances["xi-01"].run_cycle()                     # nothing observed yet: nothing to retain
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "FAILED" and inst["sequence"] == 1
    assert all(r["quality"] == "UNKNOWN" and r["placement"]["reason_codes"] == ["SOURCE_TIMEOUT"] for r in recs.values())
    line = next(json.loads(x) for x in log.getvalue().splitlines() if "collect_failed" in x)
    assert line["event"] == "collect_failed_retained" and line["level"] == "warn"
    assert line["alert"] is False and line["retained"] == 0


def test_t19_explicit_unknown_and_partial_snapshots():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    settle(rt, clock, cycles=3)
    partial = sb.with_resource_error(sb.base_result(), "array:log2", "XIRAID_DAEMON_UNAVAILABLE")
    mod.push(partial)
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "PARTIAL"
    assert recs[0]["placement"]["reason_codes"] == ["NORMAL"] and recs[1]["placement"]["reason_codes"] == ["NORMAL"]
    # C's UNKNOWN record does not replace its verdict: the allow is retained
    # and the UNKNOWN record's first reason says why.
    assert recs[2]["quality"] == "VALID" and recs[2]["placement"]["allowed"]
    assert recs[2]["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "DEPENDENCY_ERROR"]
    failed = sb.base_result()
    failed["snapshot_status"] = "FAILED"
    mod.push(failed)
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "FAILED"
    # A VALID record in a FAILED snapshot is always a retained verdict.
    assert all(r["quality"] == "VALID" and r["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "SOURCE_FAILED"] for r in recs.values())


# ---------------------------------------------------------------------------
# T-21: hold-down
# ---------------------------------------------------------------------------


def test_t21_flap_resets_the_hold_down():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    ir = rt.instances["xi-01"]
    settle(rt, clock, cycles=3)
    assert records(rt)[1][0]["placement"]["allowed"]
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["online", "initialized", "read_only"]))
    ir.run_cycle()
    _, recs = records(rt)
    assert not recs[0]["placement"]["allowed"] and "READ_ONLY" in recs[0]["placement"]["reason_codes"]
    assert recs[2]["placement"]["allowed"]  # C untouched: no deny in force, so no hold-down for it
    clock.advance(5.0)
    ir.run_cycle()  # healthy again: cycle 1 of the new streak
    _, recs = records(rt)
    assert recs[0]["placement"]["reason_codes"] == ["RECOVERY_HOLD_DOWN"]
    assert recs[2]["placement"]["reason_codes"] == ["NORMAL"]
    clock.advance(5.0)
    ir.run_cycle()  # cycle 2 but only 5 s
    assert records(rt)[1][0]["placement"]["allowed"] is False
    clock.advance(4.0)
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["online", "initialized", "read_only"]))
    ir.run_cycle()  # the flap: a deny inside the hold-down resets the streak
    assert "READ_ONLY" in records(rt)[1][0]["placement"]["reason_codes"]
    clock.advance(4.0)
    ir.run_cycle()  # cycle 1 of a new streak
    assert records(rt)[1][0]["placement"]["reason_codes"] == ["RECOVERY_HOLD_DOWN"]
    clock.advance(6.0)
    ir.run_cycle()  # 19 s after the first healthy cycle, but only 6 s into the new streak
    assert records(rt)[1][0]["placement"]["allowed"] is False
    clock.advance(5.0)
    ir.run_cycle()
    assert records(rt)[1][0]["placement"]["allowed"] is True


def test_t21_expired_lease_requires_a_fresh_hold_down():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    ir = rt.instances["xi-01"]
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["offline"]))
    ir.run_cycle()  # a deny is in force
    clock.advance(5.0)
    ir.run_cycle()  # healthy: the hold-down starts
    assert records(rt)[1][0]["placement"]["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    clock.advance(25.0)  # the source is silent past the lease (source_max_age_ms 20 s); the deny stays held
    ir.run_cycle()
    # Two cycles and 30 s since the first healthy one would satisfy a
    # hold-down that ignored the lease; the silent gap restarts it instead.
    a = records(rt)[1][0]
    assert a["placement"]["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    assert a["diagnostics"]["hold_down"]["distinct_cycles"] == 1
    clock.advance(11.0)
    ir.run_cycle()
    assert records(rt)[1][0]["placement"]["allowed"] is True


def test_hold_down_parameters_from_profile():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    p = make_profile(recovery_hold_down_ms=0, recovery_distinct_cycles=1)
    rt = Runtime(make_config([make_instance()], RuntimeConfig(collect_deadline_ms=200, collect_interval_ms=1000), profile=p), Logger(stream=open("/dev/null", "w")), clock=clock, module_factory=lambda inst: mod)
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["offline"]))
    rt.instances["xi-01"].run_cycle()  # a deny is in force
    assert records(rt)[1][0]["placement"]["allowed"] is False
    clock.advance(1.0)
    rt.instances["xi-01"].run_cycle()  # no hold-down time, one cycle: the allow is published at once
    assert records(rt)[1][0]["placement"]["allowed"] is True


# ---------------------------------------------------------------------------
# T-20 / T-29: epochs, sequences, reload, rebind
# ---------------------------------------------------------------------------


def test_sequence_increments_per_accepted_collection_and_epoch_is_stable():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    ir = rt.instances["xi-01"]
    epochs = set()
    for n in range(1, 4):
        ir.run_cycle()
        inst, _ = records(rt)
        assert inst["sequence"] == n
        epochs.add(inst["epoch"])
    assert len(epochs) == 1 and rt.runtime_epoch in next(iter(epochs))


def test_t29_reload_with_a_rebind_resets_permission_and_changes_the_epoch():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    modules = {"xi-01": mod}
    rt = make_runtime(clock, modules)
    settle(rt, clock, cycles=3)
    inst_before, recs = records(rt)
    assert recs[0]["placement"]["allowed"]
    new_bindings = list(make_instance().bindings)
    new_bindings[0] = make_binding(0, "training-a", "/mnt/data/training-a", "training-a:8", gen=2)
    rt.reload(make_config([make_instance(bindings=new_bindings)], RuntimeConfig(collect_deadline_ms=200, collect_interval_ms=1000)))
    inst, recs = records(rt)
    assert inst["epoch"] != inst_before["epoch"] and inst["sequence"] == 0
    assert recs[0]["binding_generation"] == 2 and recs[0]["quality"] == "UNKNOWN"
    rt.instances["xi-01"].run_cycle()
    _, recs = records(rt)
    assert recs[0]["placement"]["reason_codes"] == ["INCARNATION_MISMATCH"]  # the share still carries :7


def test_reload_without_changes_keeps_state():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    settle(rt, clock, cycles=3)
    inst_before, _ = records(rt)
    rt.reload(make_config([make_instance()], RuntimeConfig(collect_deadline_ms=200, collect_interval_ms=1000)))
    inst, recs = records(rt)
    assert inst["epoch"] == inst_before["epoch"] and inst["sequence"] == inst_before["sequence"]
    assert recs[0]["placement"]["allowed"]


def test_reload_removes_and_adds_instances():
    clock = FakeClock()
    mods = {"xi-01": ScriptedModule(clock), "xi-02": ScriptedModule(clock)}
    rt = make_runtime(clock, mods)
    other = make_instance("xi-02", bindings=[make_binding(7, "training-a", "/mnt/data/training-a", "training-a:7")])
    rt.reload(make_config([other], RuntimeConfig(collect_deadline_ms=200, collect_interval_ms=1000)))
    ids = [i["connector_instance_id"] for i in rt.batch()["instances"]]
    assert ids == ["xi-02"]


# ---------------------------------------------------------------------------
# T-22: a hanging worker does not block another instance; bounded restarts
# ---------------------------------------------------------------------------


def test_c05_regressed_generation_is_ignored_and_a_new_epoch_resets():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    ir = rt.instances["xi-01"]
    ir.run_cycle()  # generation 101
    ir.run_cycle()  # 102
    seq = records(rt)[0]["sequence"]
    mod.generation = 90  # a delayed / replayed older snapshot
    mod.push(sb.base_result(), same_generation=True)
    clock.advance(5.0)
    ir.run_cycle()
    assert records(rt)[0]["sequence"] == seq
    assert ir._last_source == ("publisher-epoch-1", 102)
    # A new source epoch (agent restart) starts a fresh generation line.
    mod.epoch = "publisher-epoch-2"
    mod.generation = 5
    mod.push(sb.base_result(), same_generation=True)
    clock.advance(5.0)
    ir.run_cycle()
    assert records(rt)[0]["sequence"] == seq + 1
    assert ir._last_source == ("publisher-epoch-2", 5)


class SlowEvaluateModule(Module):
    """collect returns at once; evaluate hangs (audit C-07)."""

    name = "xinas"

    def __init__(self, clock):
        self.clock = clock
        self.release = threading.Event()
        self.evaluate_calls = 0

    def describe(self):
        return {}

    def validate(self, instance, profile):
        return []

    def collect(self, deadline_s):
        r = sb.base_result()
        return SourceBatch(r, self.clock(), 10, r["server_epoch"], r["source_generation"], r["snapshot_status"])

    def evaluate(self, batch, profile, bindings):
        self.evaluate_calls += 1
        self.release.wait(30)
        return []


def test_c07_slow_evaluate_is_bounded_by_the_same_deadline():
    real_clock = time.monotonic
    slow = SlowEvaluateModule(real_clock)
    cfg = make_config([make_instance()], RuntimeConfig(collect_deadline_ms=100, collect_interval_ms=1000, worker_restart_limit=1))
    rt = Runtime(cfg, Logger(stream=open("/dev/null", "w")), clock=real_clock, module_factory=lambda inst: slow)
    ir = rt.instances["xi-01"]
    t0 = real_clock()
    ir.run_cycle()
    assert real_clock() - t0 < 2.0
    assert rt.health()["instances"]["xi-01"]["stuck"] is True
    assert rt.health()["instances"]["xi-01"]["last_error"] == "SOURCE_TIMEOUT"
    # While the helper is still inside evaluate, the next tick does not start a second one.
    ir.run_cycle()
    assert slow.evaluate_calls == 1
    assert rt.health()["instances"]["xi-01"]["last_error"] == "COLLECT_IN_FLIGHT"
    slow.release.set()


class HangingModule(Module):
    name = "fixture"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def describe(self):
        return {}

    def validate(self, instance, profile):
        return []

    def collect(self, deadline_s):
        self.calls += 1
        self.started.set()
        self.release.wait(30)
        raise CollectionError("SOURCE_TIMEOUT", "late")

    def evaluate(self, batch, profile, bindings):
        return []


def test_t22_hanging_collect_is_abandoned_at_the_deadline_and_others_proceed():
    real_clock = time.monotonic
    hang = HangingModule()
    clock = FakeClock()
    healthy = ScriptedModule(clock)
    fixture_inst = Instance(id="fx", module="fixture", bindings=(make_binding(9, "dataset-a", "/pool/a"),), fixture_file="/dev/null")
    cfg = make_config([make_instance(), fixture_inst], RuntimeConfig(collect_deadline_ms=100, collect_interval_ms=1000, worker_restart_limit=1))
    rt = Runtime(cfg, Logger(stream=open("/dev/null", "w")), clock=real_clock, module_factory=lambda inst: hang if inst.id == "fx" else healthy)
    t0 = real_clock()
    rt.instances["fx"].run_cycle()
    assert real_clock() - t0 < 2.0  # bounded by the deadline, not by the hang
    assert rt.health()["instances"]["fx"]["stuck"] is True
    assert rt.health()["instances"]["fx"]["last_error"] == "SOURCE_TIMEOUT"
    rt.instances["xi-01"].run_cycle()
    assert records(rt)[0]["sequence"] == 1
    # Audit C-07: at most one collect in flight — a second cycle while the
    # first hangs does NOT start another helper; it is skipped as COLLECT_IN_FLIGHT.
    rt.instances["fx"].run_cycle()
    assert hang.calls == 1
    assert rt.health()["instances"]["fx"]["last_error"] == "COLLECT_IN_FLIGHT"
    assert rt.health()["instances"]["fx"]["stuck"] is True
    # Once the stuck helper returns, the next cycle starts a fresh one.
    hang.release.set()
    hang.started.clear()
    for _ in range(50):
        if not rt.instances["fx"]._helper.is_alive():
            break
        time.sleep(0.02)
    rt.instances["fx"].run_cycle()
    assert hang.calls == 2


def test_worker_restart_budget_publishes_worker_stuck():
    hang = HangingModule()
    fixture_inst = Instance(id="fx", module="fixture", bindings=(make_binding(9, "dataset-a", "/pool/a"),), fixture_file="/dev/null")
    cfg = make_config([fixture_inst], RuntimeConfig(collect_deadline_ms=50, collect_interval_ms=1000, worker_restart_limit=1))
    rt = Runtime(cfg, Logger(stream=open("/dev/null", "w")), module_factory=lambda inst: hang)
    ir = rt.instances["fx"]
    for _ in range(3):
        ir.run_cycle()
        ir._note_restart()
    assert ir._stuck_exhausted() is True
    _, recs = records(rt, "fx")
    assert recs[9]["placement"]["reason_codes"] == ["WORKER_STUCK"]
    hang.release.set()


# ---------------------------------------------------------------------------
# T-33 / CON-21: limits and bounded diagnostics
# ---------------------------------------------------------------------------


def test_batch_too_large_is_an_observable_error():
    clock = FakeClock()
    rt = make_runtime(clock, {"xi-01": ScriptedModule(clock)}, runtime_cfg=RuntimeConfig(collect_deadline_ms=200, collect_interval_ms=1000, max_batch_bytes=4096))
    settle(rt, clock, cycles=3)
    with pytest.raises(BatchTooLarge):
        rt.batch_bytes()


def test_diagnostics_are_bounded_not_silently_cut():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    big = sb.base_result()
    sb.find(big, "array:data1")["details"]["raw_states"] = ["online", "initialized"] + ["degraded"] * 2000
    mod.push(big)
    rt.instances["xi-01"].run_cycle()
    _, recs = records(rt)
    diag = recs[0]["diagnostics"]
    assert diag["truncated"] is True and diag["bytes"] > contract.MAX_DIAGNOSTICS_BYTES
    assert len(json.dumps(diag)) < contract.MAX_DIAGNOSTICS_BYTES


def test_256_ds_batch_validates_and_renders_quickly(schemas):
    clock = FakeClock()
    result = sb.base_result()
    bindings = []
    for i in range(256):
        result["shares"].append(sb.share(f"s{i}", f"/mnt/data/s{i}", "fs:mnt-data.mount"))
        result["resources"].append(sb.export(f"/mnt/data/s{i}"))
        bindings.append(make_binding(i, f"s{i}", f"/mnt/data/s{i}", f"s{i}:7"))
    mod = ScriptedModule(clock)
    mod.push(result)
    mod.push(result)
    mod.push(result)
    rt = make_runtime(clock, {"xi-01": mod}, instances=[make_instance(bindings=bindings)])
    settle(rt, clock, cycles=3)
    t0 = time.perf_counter()
    data = rt.batch_bytes()
    assert time.perf_counter() - t0 < 0.5
    assert len(data) < contract.MAX_BATCH_BYTES
    batch = json.loads(data)
    validate(schemas["batch"], batch)
    assert sum(1 for a in batch["instances"][0]["assessments"] if a["placement"]["allowed"]) == 256


def test_metrics_text_has_bounded_labels():
    clock = FakeClock()
    rt = make_runtime(clock, {"xi-01": ScriptedModule(clock)})
    rt.instances["xi-01"].run_cycle()
    text = rt.metrics_text()
    assert 'connector_collect_total{instance="xi-01",result="ok"} 1' in text
    assert 'connector_assessment_allowed{instance="xi-01",ds_id="0"} 1' in text


def test_batch_schema_rejects_a_profile_id_that_cannot_be_pinned(schemas):
    jsonschema = pytest.importorskip("jsonschema")
    prof = (schemas["batch"]["properties"]["instances"]["items"]["properties"]["assessments"]
            ["items"]["properties"]["profile"])
    assert prof["properties"]["id"]["pattern"] == "^[A-Za-z0-9._-]{1,63}$"
    assert prof["properties"]["id"]["maxLength"] == 63
    assert not jsonschema.Draft7Validator(prof).is_valid({"id": "bad id", "version": "1", "digest": "d"})


def test_batch_schema_allows_an_absolute_ds_path(schemas):
    jsonschema = pytest.importorskip("jsonschema")
    ep = (schemas["batch"]["properties"]["instances"]["items"]["properties"]["assessments"]
          ["items"]["properties"]["endpoint"])
    assert ep["properties"]["ds_path"] == ep["properties"]["export_path"]   # both canonical (2026-09-27)
    assert "ds_path" not in ep["required"]
    base = {"server": "s", "export_path": "/mnt/data", "protocol": "NFS", "transport": "TCP", "port": 2049}
    assert jsonschema.Draft7Validator(ep).is_valid(dict(base, ds_path="/mnt/data/pnfs-ds"))
    assert not jsonschema.Draft7Validator(ep).is_valid(dict(base, ds_path="relative"))
