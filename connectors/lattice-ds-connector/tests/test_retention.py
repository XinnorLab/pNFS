# SPDX-License-Identifier: MIT
"""Smart verdict retention (design 2026-09-29 §3, §4.2, §4.3): a VALID
record's TTL is its hold minus its age; no new data — an UNKNOWN record or a
collection error of any kind — never replaces a verdict; a verdict is
dropped on a rebind or when its hold runs out."""

import threading
from dataclasses import replace

import pytest

import source_builder as sb
from conftest import make_profile, validate
from lattice_ds_connector import contract
from lattice_ds_connector.modules.base import CollectionError
from lattice_ds_connector.verdicts import StoredVerdict, VerdictKey, VerdictStore
from runtime_helpers import FakeClock, ScriptedModule, make_runtime, records, settle

HOLD_OTHER = contract.DEFAULT_VERDICT_HOLD_MS
HOLD_CRIT = contract.DEFAULT_CRITICAL_HOLD_MS


def healthy_rt():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    settle(rt, clock, cycles=3)                # allowed at once: no deny was in force
    return clock, mod, rt


def test_valid_record_ttl_is_the_hold_minus_age():
    clock, mod, rt = healthy_rt()
    _, recs = records(rt)
    a = recs[0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"]
    assert a["remaining_ttl_ms"] == HOLD_OTHER - a["evidence_age_ms"]
    assert HOLD_OTHER - 10_000 <= a["remaining_ttl_ms"] <= HOLD_OTHER
    ttl0 = a["remaining_ttl_ms"]
    clock.advance(100.0)
    _, recs = records(rt)
    assert recs[0]["remaining_ttl_ms"] == ttl0 - 100_000


@pytest.mark.parametrize("err", [
    CollectionError("SOURCE_TIMEOUT", "t", retryable=True),
    CollectionError("SOURCE_AUTH_FAILED", "a", retryable=False),
    CollectionError("SOURCE_SCHEMA_INVALID", "s", retryable=False),
    CollectionError("SOURCE_NOT_READY", "n", retryable=False),
    CollectionError("SOURCE_FAILED", "f", retryable=False),
])
def test_collection_errors_retain_the_verdict(err, schemas):
    clock, mod, rt = healthy_rt()
    inst0, recs0 = records(rt)
    seq0, age0 = inst0["sequence"], recs0[0]["evidence_age_ms"]
    mod.push(error=err)
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "FAILED" and inst["sequence"] == seq0 + 1
    a = recs[0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is True
    assert a["placement"]["reason_codes"] == ["VERDICT_RETAINED", err.code, "NORMAL"]
    assert a["target_incarnation"] == "training-a:7"          # the stored identity, not null
    assert a["observed_at"] == recs0[0]["observed_at"]       # the original observation
    # Re-publishing never extends the hold: the age keeps counting from the observation.
    assert a["evidence_age_ms"] == age0 + 5_000
    assert a["remaining_ttl_ms"] == HOLD_OTHER - a["evidence_age_ms"]
    validate(schemas["batch"], rt.batch())


def test_unknown_record_does_not_replace_a_verdict():
    clock, mod, rt = healthy_rt()
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["bogus-word"]))   # a successful collect that evaluates UNKNOWN
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "COMPLETE"
    a = recs[0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is True
    # The UNKNOWN record's first reason is the cause, then the verdict's own reasons.
    assert a["placement"]["reason_codes"] == ["VERDICT_RETAINED", "SOURCE_UNKNOWN", "NORMAL"]
    assert recs[2]["placement"]["reason_codes"] == ["NORMAL"]   # data2 is healthy: a fresh record


def test_unknown_record_without_a_verdict_is_published_as_is():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["bogus-word"]))
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["quality"] == "UNKNOWN" and a["remaining_ttl_ms"] == 0
    assert a["placement"]["reason_codes"][0] == "SOURCE_UNKNOWN" and "VERDICT_RETAINED" not in a["placement"]["reason_codes"]
    assert a["diagnostics"]    # the evaluated record, not a synthesized placeholder


def test_retained_verdict_expires_to_unknown_after_its_hold():
    clock, mod, rt = healthy_rt()
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    rt.instances["xi-01"].run_cycle()
    clock.advance(HOLD_OTHER / 1000.0 + 1.0)
    a = records(rt)[1][0]
    assert a["quality"] == "UNKNOWN" and a["placement"]["reason_codes"][0] == "EVIDENCE_EXPIRED"
    assert a["placement"]["allowed"] is False and a["placement"]["multiplier_ppm"] == 0
    assert "VERDICT_RETAINED" not in a["placement"]["reason_codes"]
    assert a["remaining_ttl_ms"] == 0
    # The next error cycle finds the hold run out: the entry is dropped.
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["quality"] == "UNKNOWN" and a["placement"]["reason_codes"] == ["SOURCE_TIMEOUT"]
    assert rt.verdicts.get(rt.instances["xi-01"].key_for(rt.instances["xi-01"].instance.bindings[0])) is None


def test_critical_verdict_is_held_twenty_minutes():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["offline"]))      # ARRAY_UNAVAILABLE: a veto
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False
    assert a["remaining_ttl_ms"] == HOLD_CRIT - a["evidence_age_ms"]
    assert HOLD_CRIT - 5_000 <= a["remaining_ttl_ms"] <= HOLD_CRIT
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    clock.advance(HOLD_OTHER / 1000.0 + 60.0)                                   # past the 10-min hold
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False       # still held (20 min)
    assert a["placement"]["reason_codes"][:3] == ["VERDICT_RETAINED", "SOURCE_TIMEOUT", "ARRAY_UNAVAILABLE"]
    clock.advance((HOLD_CRIT - HOLD_OTHER) / 1000.0)
    assert records(rt)[1][0]["quality"] == "UNKNOWN"


def test_worker_stuck_retains_the_verdict():
    clock, mod, rt = healthy_rt()
    ir = rt.instances["xi-01"]
    for _ in range(rt.config.runtime.worker_restart_limit + 1):
        ir._note_restart()
    assert ir._stuck_exhausted() is True
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "FAILED"
    assert all(r["quality"] == "VALID" and r["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "WORKER_STUCK"] for r in recs.values())


def rebind(cfg, ds_id, generation):
    """A copy of cfg whose binding for ds_id carries a new generation."""
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
    inst, recs = records(rt)
    assert inst["sequence"] == 0                              # the instance was recreated
    assert recs[0]["quality"] == "UNKNOWN"                    # neutral until the new binding reports
    # The recreated instance keeps the verdicts of the bindings that did not change.
    assert recs[1]["quality"] == "VALID" and recs[1]["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "NO_ASSESSMENT"]


def test_changed_incarnation_drops_the_verdict():
    clock, mod, rt = healthy_rt()
    cfg = rt.config
    inst = cfg.instances[0]
    bindings = tuple(replace(b, expected_target_incarnation="training-b:8") if b.ds_id == 1 else b for b in inst.bindings)
    rt.reload(replace(cfg, instances=(replace(inst, bindings=bindings),)))
    assert not any(k.ds_id == 1 for k in rt.verdicts.keys())
    recs = records(rt)[1]
    assert recs[1]["quality"] == "UNKNOWN" and recs[0]["quality"] == "VALID"


def test_preflight_reports_fresh_and_retained():
    from lattice_ds_connector.preflight import evaluate, render
    clock, mod, rt = healthy_rt()
    rep = evaluate({"ready": True}, rt.batch(), expect_ds=[0, 1, 2])
    assert rep["ready"] is True and {r["verdict"] for r in rep["ds"]} == {"fresh"}
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    rt.instances["xi-01"].run_cycle()
    rep = evaluate({"ready": True}, rt.batch(), expect_ds=[0, 1, 2])
    assert rep["ready"] is True, rep["reasons"]
    row = next(r for r in rep["ds"] if r["ds_id"] == 0)
    assert row["verdict"] == "retained" and row["hold_left_ms"] > 0
    assert "verdict=retained hold_left_ms=%d" % row["hold_left_ms"] in render(rep)


# ---------------------------------------------------------------------------
# The store itself
# ---------------------------------------------------------------------------


def _key(ds_id=0):
    return VerdictKey("xi-01", ds_id, 1, "training-a", "training-a:7")


def test_stored_verdict_remaining_uses_the_class_hold():
    p = make_profile()
    crit = StoredVerdict(published=None, critical=True, hold_origin_mono=100.0, profile_id=p.id)
    other = replace(crit, critical=False)
    assert crit.remaining_ms(p, 100.0) == HOLD_CRIT and other.remaining_ms(p, 100.0) == HOLD_OTHER
    assert other.remaining_ms(p, 100.0 + HOLD_OTHER / 1000.0) == 0
    assert crit.remaining_ms(p, 100.0 + HOLD_OTHER / 1000.0) == HOLD_CRIT - HOLD_OTHER
    assert crit.remaining_ms(p, 90.0) == HOLD_CRIT       # an origin in the future counts as age 0


def test_store_version_bumps_on_put_and_real_drop_only():
    s = VerdictStore()
    v = StoredVerdict(published=None, critical=False, hold_origin_mono=1.0, profile_id="xinas-mvp")
    assert s.version == 0 and s.get(_key()) is None
    s.put(_key(), v)
    assert s.version == 1 and s.get(_key()) is v and s.keys() == [_key()] and s.items() == [(_key(), v)]
    s.drop(_key(1))                                        # absent: no change
    assert s.version == 1
    newer = replace(v, hold_origin_mono=2.0)
    s.put(_key(), newer)
    s.drop(_key(), expected=v)                             # replaced meanwhile: kept
    assert s.version == 2 and s.get(_key()) is newer
    s.drop(_key())
    assert s.version == 3 and s.keys() == []


def test_store_is_safe_under_concurrent_writers():
    s = VerdictStore()
    v = StoredVerdict(published=None, critical=False, hold_origin_mono=1.0, profile_id="xinas-mvp")

    def writer(ds_id):
        for _ in range(500):
            s.put(_key(ds_id), v)
            s.items()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s.version == 8 * 500 and len(s.keys()) == 8


# ---------------------------------------------------------------------------
# Fix round 1: evidence-less denies; a FAILED source snapshot is no new data
# ---------------------------------------------------------------------------


def without_share(share_id):
    r = sb.base_result()
    r["shares"] = [s for s in r["shares"] if s["share_id"] != share_id]
    return r


def test_evidence_less_deny_is_a_held_critical_verdict(schemas):
    clock, mod, rt = healthy_rt()
    assert records(rt)[1][0]["placement"]["allowed"] is True
    mod.push(without_share("training-a"))                   # COMPLETE snapshot: SHARE_ABSENT, no evidence age
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    a = recs[0]
    assert inst["snapshot_status"] == "COMPLETE"
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False
    assert a["placement"]["reason_codes"][0] == "SHARE_ABSENT"
    assert a["remaining_ttl_ms"] == HOLD_CRIT                # the hold counts from the fetch
    assert a["evidence_age_ms"] is None and a["observed_at"] is None   # as the policy produced them
    validate(schemas["batch"], rt.batch())
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    clock.advance(30.0)
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    a = recs[0]
    assert inst["snapshot_status"] == "FAILED"
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False
    assert a["placement"]["reason_codes"][:3] == ["VERDICT_RETAINED", "SOURCE_TIMEOUT", "SHARE_ABSENT"]
    assert a["remaining_ttl_ms"] == HOLD_CRIT - 30_000       # counting down from that fetch
    validate(schemas["batch"], rt.batch())
    clock.advance(HOLD_CRIT / 1000.0)
    assert records(rt)[1][0]["quality"] == "UNKNOWN" and records(rt)[1][0]["placement"]["reason_codes"][0] == "EVIDENCE_EXPIRED"


def wrong_controller_failed():
    r = sb.base_result(snapshot_status="FAILED")
    r["controller_id"] = "someone-else"                      # the policy vetoes identity before the FAILED check
    return r


def test_failed_source_snapshot_retains_instead_of_a_fresh_verdict(schemas):
    clock, mod, rt = healthy_rt()
    mod.push(wrong_controller_failed())
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "FAILED"
    for r in recs.values():
        assert r["quality"] == "VALID" and r["placement"]["allowed"] is True    # the stored allow, not the fresh deny
        assert r["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "IDENTITY_MISMATCH"]
    validate(schemas["batch"], rt.batch())


def test_failed_source_snapshot_without_a_verdict_is_unknown(schemas):
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    mod.push(wrong_controller_failed())
    rt.instances["xi-01"].run_cycle()
    inst, recs = records(rt)
    assert inst["snapshot_status"] == "FAILED"
    for r in recs.values():
        assert r["quality"] == "UNKNOWN" and r["placement"]["reason_codes"][0] == "IDENTITY_MISMATCH"
        assert r["remaining_ttl_ms"] == 0
    assert rt.verdicts.keys() == []                          # nothing stored from a FAILED snapshot
    validate(schemas["batch"], rt.batch())


# ---------------------------------------------------------------------------
# R3: the recovery hold-down only follows a deny (design rule 6)
# ---------------------------------------------------------------------------


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
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["offline"]))
    rt.instances["xi-01"].run_cycle()                        # critical observed at t0
    crit_ttl = records(rt)[1][0]["remaining_ttl_ms"]
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()                        # healthy again: hold-down deny
    a = records(rt)[1][0]
    assert a["placement"]["allowed"] is False and a["placement"]["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    assert crit_ttl - 6_000 <= a["remaining_ttl_ms"] <= crit_ttl - 4_000   # counted from t0, not refreshed
    clock.advance(11.0)                                      # the hold-down began at the first allow: >= 10 s later, 2 cycles
    rt.instances["xi-01"].run_cycle()
    assert records(rt)[1][0]["placement"]["allowed"] is True


def test_unknown_period_without_a_deny_needs_no_hold_down():
    clock, mod, rt = healthy_rt()
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()                        # healthy again
    assert records(rt)[1][0]["placement"]["reason_codes"] == ["NORMAL"]


def test_neutral_period_after_an_allow_needs_no_hold_down():
    clock, mod, rt = healthy_rt()
    clock.advance(HOLD_OTHER / 1000.0 + 60.0)                # the allow's hold ran out with no collect
    assert records(rt)[1][0]["quality"] == "UNKNOWN"
    rt.instances["xi-01"].run_cycle()                        # the source is back
    a = records(rt)[1][0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is True and a["placement"]["reason_codes"] == ["NORMAL"]


def test_expired_deny_needs_no_hold_down():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["offline"]))
    rt.instances["xi-01"].run_cycle()
    clock.advance(HOLD_CRIT / 1000.0 + 1.0)                  # the deny's 20 min ran out: neutral, nothing in force
    assert records(rt)[1][0]["quality"] == "UNKNOWN"
    rt.instances["xi-01"].run_cycle()
    a = records(rt)[1][0]
    assert a["placement"]["allowed"] is True and a["placement"]["reason_codes"] == ["NORMAL"]


def test_a_retained_deny_still_holds_down_the_recovery():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["offline"]))
    rt.instances["xi-01"].run_cycle()
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))
    clock.advance(30.0)
    rt.instances["xi-01"].run_cycle()                        # no new data: the deny is retained, not revoked
    assert records(rt)[1][0]["placement"]["reason_codes"][0] == "VERDICT_RETAINED"
    clock.advance(5.0)
    rt.instances["xi-01"].run_cycle()                        # healthy again: leaving that deny is held down
    a = records(rt)[1][0]
    assert a["placement"]["allowed"] is False and a["placement"]["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    assert a["remaining_ttl_ms"] < HOLD_CRIT - 35_000        # counting from the critical observation


def test_hold_down_deny_keeps_the_critical_origin_in_the_store():
    clock = FakeClock()
    mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})
    ir = rt.instances["xi-01"]
    key = ir.key_for(ir.instance.bindings[0])
    mod.push(sb.with_array_states(sb.base_result(), "data1", ["offline"]))
    ir.run_cycle()
    crit = rt.verdicts.get(key)
    assert crit.critical is True
    clock.advance(5.0)
    ir.run_cycle()                                           # hold-down deny
    held = rt.verdicts.get(key)
    assert held.critical is True and held.hold_origin_mono == crit.hold_origin_mono
    assert held.published.assessment.reason_codes[0] == "RECOVERY_HOLD_DOWN"
    clock.advance(11.0)
    ir.run_cycle()                                           # the hold-down completed: a fresh allow replaces the deny
    allow = rt.verdicts.get(key)
    assert allow.critical is False and allow.hold_origin_mono > crit.hold_origin_mono
    assert allow.published.assessment.allowed is True
