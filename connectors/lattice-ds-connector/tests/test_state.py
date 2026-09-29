# SPDX-License-Identifier: MIT
"""The verdict state file (smart verdict retention design §4.4): the store is
written after the cycles that change it, at most once per collect interval,
atomically and 0600; a restart restores the verdicts in force before the
first publication; a bad file never stops the connector."""

import io
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import source_builder as sb
from conftest import make_profile, validate
from lattice_ds_connector import contract, state
from lattice_ds_connector.config import RuntimeConfig
from lattice_ds_connector.log import Logger
from lattice_ds_connector.modules.base import CollectionError
from lattice_ds_connector.verdicts import VerdictKey
from runtime_helpers import FakeClock, ScriptedModule, make_runtime, records

HOLD_CRIT = contract.DEFAULT_CRITICAL_HOLD_MS
HOLD_OTHER = contract.DEFAULT_VERDICT_HOLD_MS

ENTRY_KEYS = {
    "instance", "ds_id", "binding_generation", "target_id", "expected_target_incarnation",
    "profile_id", "allowed", "multiplier_ppm", "reason_codes", "capacity_domain_id",
    "shared_resource_ids", "datastore_id", "target_incarnation", "observed_at", "critical",
    "critical_observed_at",
}


class FakeWall:
    def __init__(self, clock):
        self.clock, self.base = clock, datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.base + timedelta(seconds=self.clock.t)


def rt_with_state(tmp_path, clock, mod, profile=None, logger=None):
    cfg = RuntimeConfig(collect_deadline_ms=200, collect_interval_ms=1000, state_path=str(tmp_path / "verdicts.json"))
    return make_runtime(clock, {"xi-01": mod}, runtime_cfg=cfg, wall_clock=FakeWall(clock), profile=profile, logger=logger)


def z(dt):
    return dt.isoformat().replace("+00:00", "Z")


def parse(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def offline():
    return sb.with_array_states(sb.base_result(), "data1", ["offline"])   # ARRAY_UNAVAILABLE: a veto on ds 0 and 1


def saved(tmp_path):
    with open(tmp_path / "verdicts.json", encoding="utf-8") as fh:
        return json.load(fh)


def entry_of(data, ds_id):
    return next(e for e in data["verdicts"] if e["ds_id"] == ds_id)


def test_round_trip_restores_a_critical_verdict_after_restart(tmp_path, schemas):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    mod.push(offline())
    rt.instances["xi-01"].run_cycle(); rt.save_state(force=True)
    data = saved(tmp_path)
    assert data["version"] == 1 and any(e["ds_id"] == 0 and e["critical"] for e in data["verdicts"])
    assert data["runtime_epoch"] == rt.runtime_epoch and parse(data["written_at"]) == FakeWall(clock)()
    assert all(set(e) == ENTRY_KEYS for e in data["verdicts"])        # no credentials, no endpoints
    assert oct(os.stat(tmp_path / "verdicts.json").st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["verdicts.json"]   # no temp file left behind
    clock.advance(60.0)
    rt2 = rt_with_state(tmp_path, clock, ScriptedModule(clock))   # a restart: nothing collected yet
    inst, recs = records(rt2)
    a = recs[0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False
    assert a["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "RESTORED_FROM_STATE"]
    assert a["placement"]["reason_codes"][2:4] == ["NO_ASSESSMENT", "ARRAY_UNAVAILABLE"]
    assert contract.DEFAULT_CRITICAL_HOLD_MS - 70_000 <= a["remaining_ttl_ms"] <= contract.DEFAULT_CRITICAL_HOLD_MS - 55_000
    assert a["target_incarnation"] == "training-a:7" and a["datastore_id"] == sb.CONTROLLER
    # The allow on data2 comes back too, on its own (shorter) hold.
    c = recs[2]
    assert c["quality"] == "VALID" and c["placement"]["allowed"] is True and c["placement"]["multiplier_ppm"] == 1_000_000
    assert c["remaining_ttl_ms"] == HOLD_OTHER - c["evidence_age_ms"]
    assert inst["sequence"] == 0 and inst["snapshot_status"] == "FAILED"
    validate(schemas["batch"], rt2.batch())


def test_the_file_carries_store_times_not_wire_times(tmp_path):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    ir = rt.instances["xi-01"]
    mod.push(offline())
    ir.run_cycle()
    v = rt.verdicts.get(ir.key_for(ir.instance.bindings[0]))
    clock.advance(3.0)
    rt.save_state(force=True)
    e = entry_of(saved(tmp_path), 0)
    wall = FakeWall(clock)()
    fetched_obs = v.published.fetched_mono - v.published.assessment.evidence_age_ms / 1000.0
    assert e["observed_at"] != sb.AT                                       # never the source's own stamp
    assert abs((parse(e["observed_at"]) - (wall - timedelta(seconds=clock.t - fetched_obs))).total_seconds()) < 0.002
    assert abs((parse(e["critical_observed_at"]) - (wall - timedelta(seconds=clock.t - v.hold_origin_mono))).total_seconds()) < 0.002
    allow = entry_of(saved(tmp_path), 2)
    assert allow["critical"] is False and allow["critical_observed_at"] is None


def test_foreign_expired_and_future_entries(tmp_path):
    clock = FakeClock(); wall = FakeWall(clock)
    now = wall()
    entry = lambda **o: {**{"instance": "xi-01", "ds_id": 0, "binding_generation": 1, "target_id": "training-a",  # noqa: E731
                           "expected_target_incarnation": "training-a:7", "profile_id": "xinas-mvp", "allowed": False,
                           "multiplier_ppm": 0, "reason_codes": ["ARRAY_UNAVAILABLE"], "capacity_domain_id": "d",
                           "shared_resource_ids": [], "datastore_id": sb.CONTROLLER, "target_incarnation": "training-a:7",
                           "observed_at": z(now), "critical": True,
                           "critical_observed_at": z(now)}, **o}
    (tmp_path / "verdicts.json").write_text(json.dumps({"version": 1, "written_at": "x", "runtime_epoch": "r", "verdicts": [
        entry(ds_id=1, binding_generation=9),                                            # foreign binding: dropped
        entry(ds_id=2, target_id="training-c", expected_target_incarnation="training-c:7",
              observed_at=z(now - timedelta(hours=1))),                                  # expired: dropped
        entry(observed_at=z(now + timedelta(minutes=5))),                                # future: age 0, full hold
    ]}))
    rt = rt_with_state(tmp_path, clock, ScriptedModule(clock))
    recs = records(rt)[1]
    assert recs[0]["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "RESTORED_FROM_STATE"]
    assert recs[0]["remaining_ttl_ms"] == contract.DEFAULT_CRITICAL_HOLD_MS
    assert recs[0]["evidence_age_ms"] == 0 and parse(recs[0]["observed_at"]) <= now   # never a stamp in the future
    assert recs[1]["quality"] == "UNKNOWN" and recs[2]["quality"] == "UNKNOWN"


def test_entries_that_do_not_fit_the_binding_are_not_restored(tmp_path):
    clock = FakeClock(); wall = FakeWall(clock)
    now = z(wall())
    base = {"instance": "xi-01", "ds_id": 0, "binding_generation": 1, "target_id": "training-a",
            "expected_target_incarnation": "training-a:7", "profile_id": "xinas-mvp", "allowed": True,
            "multiplier_ppm": 1_000_000, "reason_codes": ["NORMAL"], "capacity_domain_id": "d",
            "shared_resource_ids": [], "datastore_id": sb.CONTROLLER, "target_incarnation": "training-a:7",
            "observed_at": now, "critical": False, "critical_observed_at": None}
    ds1 = {**base, "ds_id": 1, "target_id": "training-b", "expected_target_incarnation": "training-b:7", "target_incarnation": "training-b:7"}
    ds2 = {**base, "ds_id": 2, "target_id": "training-c", "expected_target_incarnation": "training-c:7", "target_incarnation": "training-c:7"}
    verdicts = [
        {**base, "profile_id": "other-profile"},              # another profile
        {**ds1, "datastore_id": "another-node"},             # the instance now names another data store
        {**ds2, "allowed": "yes"},                           # a bad type
        {**ds2, "instance": ["xi-01"]},                      # unhashable: must not stop the start
        {**ds2, "critical": True},                           # critical contradicts allowed
        {**ds2, "reason_codes": ["NOT_A_REASON"]},           # outside the catalogue
        {**ds2, "multiplier_ppm": 0},                        # an allow with 0 ppm
        {**ds2, "observed_at": "yesterday"},                 # unparsable time
        "not an object",
    ]
    (tmp_path / "verdicts.json").write_text(json.dumps({"version": 1, "written_at": now, "runtime_epoch": "r", "verdicts": verdicts}))
    rt = rt_with_state(tmp_path, clock, ScriptedModule(clock))
    recs = records(rt)[1]
    assert all(r["quality"] == "UNKNOWN" and r["placement"]["reason_codes"] == ["NO_ASSESSMENT"] for r in recs.values())
    assert rt.verdicts.keys() == []
    assert (tmp_path / "verdicts.json").exists()             # a readable file with bad entries is not renamed


def test_restored_verdict_takes_the_current_profile_hold(tmp_path):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    mod.push(offline())
    rt.instances["xi-01"].run_cycle(); rt.save_state(force=True)
    clock.advance(60.0)
    short = make_profile(critical_hold_ms=600_000)
    rt2 = rt_with_state(tmp_path, clock, ScriptedModule(clock), profile=short)
    a = records(rt2)[1][0]
    assert a["placement"]["reason_codes"][:2] == ["VERDICT_RETAINED", "RESTORED_FROM_STATE"]
    assert a["remaining_ttl_ms"] == 600_000 - a["evidence_age_ms"]
    key = rt2.instances["xi-01"].key_for(rt2.instances["xi-01"].instance.bindings[0])
    assert abs(rt2.verdicts.get(key).remaining_ms(clock()) - a["remaining_ttl_ms"]) <= 1   # one hold, one answer
    clock.advance(600.0)                                     # past the current profile's hold, inside the old one
    rt3 = rt_with_state(tmp_path, clock, ScriptedModule(clock), profile=short)
    assert records(rt3)[1][0]["quality"] == "UNKNOWN"


def test_restored_hold_down_deny_counts_from_the_critical_origin(tmp_path):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    ir = rt.instances["xi-01"]
    mod.push(offline())
    ir.run_cycle()                                           # critical observed at t0
    clock.advance(5.0)
    ir.run_cycle()                                           # healthy: the hold-down withholds the allow
    before = records(rt)[1][0]
    assert before["placement"]["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    rt.save_state(force=True)
    e = entry_of(saved(tmp_path), 0)
    assert e["critical"] is True and e["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    # observed_at is the healthy sample's observation, critical_observed_at the critical one.
    assert abs((parse(e["observed_at"]) - parse(e["critical_observed_at"])).total_seconds() - 5.0) < 0.002
    clock.advance(60.0)
    rt2 = rt_with_state(tmp_path, clock, ScriptedModule(clock))
    a = records(rt2)[1][0]
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False
    assert a["placement"]["reason_codes"][:4] == ["VERDICT_RETAINED", "RESTORED_FROM_STATE", "NO_ASSESSMENT", "RECOVERY_HOLD_DOWN"]
    # The TTL keeps counting from the critical observation, the age from the healthy sample.
    assert abs(a["remaining_ttl_ms"] - (before["remaining_ttl_ms"] - 60_000)) <= 2
    assert abs(a["evidence_age_ms"] - (before["evidence_age_ms"] + 60_000)) <= 2


def test_restored_deny_holds_down_the_first_fresh_allow(tmp_path):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    mod.push(offline())
    rt.instances["xi-01"].run_cycle(); rt.save_state(force=True)
    clock.advance(30.0)
    rt2 = rt_with_state(tmp_path, clock, ScriptedModule(clock))   # the source is healthy again
    ir2 = rt2.instances["xi-01"]
    ir2.run_cycle()
    recs = records(rt2)[1]
    a = recs[0]
    # A restored deny is in force (rule 6): leaving it is held down, counting from the critical origin.
    assert a["quality"] == "VALID" and a["placement"]["allowed"] is False
    assert a["placement"]["reason_codes"][0] == "RECOVERY_HOLD_DOWN"
    assert a["remaining_ttl_ms"] <= HOLD_CRIT - 30_000
    assert recs[2]["placement"]["allowed"] is True and recs[2]["placement"]["reason_codes"] == ["NORMAL"]   # a restored allow: none
    clock.advance(11.0)
    ir2.run_cycle()
    assert records(rt2)[1][0]["placement"]["allowed"] is True


def test_corrupt_file_is_renamed_and_the_connector_starts(tmp_path):
    clock = FakeClock()
    (tmp_path / "verdicts.json").write_text("{not json")
    out = io.StringIO()
    rt = rt_with_state(tmp_path, clock, ScriptedModule(clock), logger=Logger(stream=out))
    assert all(r["quality"] == "UNKNOWN" for r in records(rt)[1].values())
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("verdicts.json.corrupt-")]
    assert not (tmp_path / "verdicts.json").exists()
    assert "state_file_ignored" in out.getvalue() and "STATE_FILE_UNREADABLE" in out.getvalue()


@pytest.mark.parametrize("body", [
    json.dumps({"version": 2, "verdicts": []}),              # a newer format
    json.dumps({"version": True, "verdicts": []}),           # True == 1 in Python: not a version
    json.dumps({"version": 1, "verdicts": {}}),              # the wrong shape
    json.dumps([1, 2, 3]),
    "\xff\xfe",
])
def test_unusable_file_is_renamed(tmp_path, body):
    clock = FakeClock()
    (tmp_path / "verdicts.json").write_text(body, encoding="latin-1")
    rt = rt_with_state(tmp_path, clock, ScriptedModule(clock))
    assert all(r["quality"] == "UNKNOWN" for r in records(rt)[1].values())
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("verdicts.json.corrupt-")]


def test_missing_file_is_a_clean_start(tmp_path):
    clock = FakeClock()
    out = io.StringIO()
    rt = rt_with_state(tmp_path, clock, ScriptedModule(clock), logger=Logger(stream=out))
    assert all(r["quality"] == "UNKNOWN" for r in records(rt)[1].values())
    assert list(tmp_path.iterdir()) == [] and "state_file_ignored" not in out.getvalue()


def test_write_failure_is_tolerated_and_rate_bounded(tmp_path, monkeypatch):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    calls = []
    monkeypatch.setattr(state, "save", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError(28, "no space")))
    for _ in range(3):
        rt.instances["xi-01"].run_cycle(); clock.advance(0.2)       # 3 cycles within one collect interval
    assert len(calls) == 1                                          # at most once per collect_interval_ms
    assert rt.log.counters.snapshot()["connector_state_write_errors_total"]
    assert records(rt)[1][0]["quality"] == "VALID"                  # the in-memory store carries on
    clock.advance(1.0)
    rt.instances["xi-01"].run_cycle()
    assert len(calls) == 2                                          # the next interval tries again


def test_saves_follow_store_changes_only(tmp_path, monkeypatch):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    real, calls = state.save, []
    monkeypatch.setattr(state, "save", lambda *a, **k: (calls.append(1), real(*a, **k)))
    ir = rt.instances["xi-01"]
    ir.run_cycle()
    assert len(calls) == 1
    mod.push(sb.base_result(), same_generation=True)                # a replayed snapshot: no change
    clock.advance(2.0)
    ir.run_cycle()
    assert len(calls) == 1
    mod.push(error=CollectionError("SOURCE_TIMEOUT", "t", retryable=True))   # retained: no change either
    clock.advance(2.0)
    ir.run_cycle()
    assert len(calls) == 1
    clock.advance(2.0)
    ir.run_cycle()                                                  # a fresh verdict
    assert len(calls) == 2


def test_writer_skips_expired_and_unconfigured_entries(tmp_path):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    ir = rt.instances["xi-01"]
    ir.run_cycle()
    v = rt.verdicts.get(ir.key_for(ir.instance.bindings[0]))
    rt.verdicts.put(VerdictKey("xi-01", 0, 7, "training-a", "training-a:7"), v)   # a key no binding has
    rt.save_state(force=True)
    data = saved(tmp_path)
    assert sorted((e["ds_id"], e["binding_generation"]) for e in data["verdicts"]) == [(0, 1), (1, 1), (2, 1)]
    clock.advance(HOLD_OTHER / 1000.0 + 1.0)                        # every hold ran out; nothing dropped them
    assert rt.verdicts.keys()
    rt.save_state(force=True)
    assert saved(tmp_path)["verdicts"] == []


def test_stop_writes_the_last_change(tmp_path, monkeypatch):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = rt_with_state(tmp_path, clock, mod)
    ir = rt.instances["xi-01"]
    ir.run_cycle()                                                  # saved
    mod.push(offline())
    clock.advance(0.3)
    ir.run_cycle()                                                  # inside the interval: not yet saved
    assert entry_of(saved(tmp_path), 0)["critical"] is False
    rt.stop(1.0)
    assert entry_of(saved(tmp_path), 0)["critical"] is True


def test_save_is_atomic(tmp_path, monkeypatch):
    path = str(tmp_path / "verdicts.json")
    wall = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    state.save(path, [{"a": 1}], "epoch-1", wall)
    before = open(path, encoding="utf-8").read()

    def boom(*_a, **_k):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(state.os, "replace", boom)
    with pytest.raises(OSError):
        state.save(path, [{"a": 2}], "epoch-1", wall)
    assert open(path, encoding="utf-8").read() == before            # the old file is intact
    assert sorted(p.name for p in tmp_path.iterdir()) == ["verdicts.json"]   # the temp file is gone


def test_no_state_path_means_no_file(tmp_path, monkeypatch):
    clock = FakeClock(); mod = ScriptedModule(clock)
    rt = make_runtime(clock, {"xi-01": mod})                        # RuntimeConfig().state_path is None
    monkeypatch.setattr(state, "save", lambda *a, **k: pytest.fail("no state file configured"))
    monkeypatch.setattr(state, "load", lambda *a, **k: pytest.fail("no state file configured"))
    rt.instances["xi-01"].run_cycle()
    rt.save_state(force=True)
    rt.stop(1.0)
