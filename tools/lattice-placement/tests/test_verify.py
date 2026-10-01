# SPDX-License-Identifier: MIT
import copy
import os

from lattice_placement.live import MdsState, parse_config_show, parse_metrics, state_from_show
from lattice_placement.verify import EXIT_DIFFER, EXIT_OK, EXIT_UNREADABLE, render_show, verdict

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name, host, desired=None, metrics=None):
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as fh:
        show = parse_config_show(fh.read())
    m = None
    if metrics:
        with open(os.path.join(FIX, metrics), "r", encoding="utf-8") as fh:
            m = parse_metrics(fh.read())
    return state_from_show(host, show, m, desired)


def smart2(host="192.168.65.225", desired="smart"):
    return load("config-show-smart-mds2.json", host, desired, "metrics-smart-mds2.txt")


def test_two_identical_smart_mds_with_partial_coverage():
    a, b = smart2("m1"), smart2("m2")
    v = verdict([a, b])
    assert v.exit_code == EXIT_OK and v.errors == []
    assert len(v.warnings) == 2
    assert v.warnings[0].startswith("COVERAGE_PARTIAL:m1: 1 of 2 DS have a verdict in force (neutral: ds 1 NO_BINDING)")
    v = verdict([a, b], require_full_coverage=True)
    assert v.exit_code == EXIT_DIFFER and any(e.startswith("COVERAGE_PARTIAL:m1") for e in v.errors)


def test_a_pre_retention_mds_without_steering_refuses_every_file_and_fails():
    """An MDS that predates verdict retention (its readiness row has no
    retained_ds/neutral_ds) refuses every new file without a verdict
    (MODE_NOT_READY / ASSESSMENT_* -> ENOSPC): no coverage and an unreachable
    connector stay errors there, as before retention (final review F3)."""
    b = load("config-show-smart-mds1-none.json", "m1", "smart")
    assert b.readiness.retention_aware is False
    v = verdict([b])
    assert v.exit_code == EXIT_DIFFER
    assert any(e.startswith("CONNECTOR_UNREACHABLE:m1: socket /run/lattice-ds-connector") for e in v.errors)
    assert any(e.startswith("COVERAGE_NONE:m1") for e in v.errors)
    assert any(e.startswith("NO_ELIGIBLE_DS:m1") for e in v.errors)     # registered_ds=2 eligible_ds=0
    assert not any(w.startswith(("CONNECTOR_UNREACHABLE", "STEERING_OFF")) for w in v.warnings)
    a = smart2("m2")
    assert a.readiness.retention_aware is False
    v = verdict([a, b])
    assert any(e.startswith("CONNECTOR_CONFIG_DIGEST_MISMATCH") for e in v.errors)   # m1 has no digest
    assert not any(e.endswith(":m2") or ":m2:" in e for e in v.errors)             # m2 is reachable, partial


def test_an_mds_that_admits_no_ds_fails_on_any_build():
    s = load("config-show-smart-retention.json", "m1")
    assert s.readiness.retention_aware is True
    # both DS denied by fresh verdicts: full coverage, nothing eligible
    s.readiness.coverage, s.readiness.covered_ds, s.readiness.neutral_ds = "full", 2, 0
    s.readiness.retained_ds, s.readiness.eligible_ds = 0, 0
    v = verdict([s])
    assert v.exit_code == EXIT_DIFFER
    assert [e.split(":")[0] for e in v.errors] == ["NO_ELIGIBLE_DS"]
    assert v.errors[0].startswith("NO_ELIGIBLE_DS:m1: the MDS admits no DS (2 registered, 0 eligible)")
    s.readiness.eligible_ds = 1
    assert verdict([s]).exit_code == EXIT_OK
    s.readiness.registered_ds, s.readiness.covered_ds, s.readiness.eligible_ds = 0, 0, 0   # nothing registered yet
    assert not any(e.startswith("NO_ELIGIBLE_DS") for e in verdict([s]).errors)


def test_no_coverage_and_unreachable_are_warnings_now():
    a = load("config-show-smart-steering-off.json", "m1")         # reachable=0, coverage=none, a valid profiles row
    v = verdict([a])
    assert v.exit_code == EXIT_OK and v.errors == []
    assert any(w.startswith("STEERING_OFF:m1") for w in v.warnings)
    assert any(w.startswith("CONNECTOR_UNREACHABLE:m1: socket /run/lattice-ds-connector/connector.sock: absent or refused")
               for w in v.warnings)
    v = verdict([a], require_full_coverage=True)
    assert v.exit_code == EXIT_DIFFER
    assert any(e.startswith("STEERING_OFF:m1") for e in v.errors)
    assert any(w.startswith("CONNECTOR_UNREACHABLE:m1") for w in v.warnings)     # still a warning: only coverage is gated


def test_retained_verdicts_fail_only_full_coverage():
    s = load("config-show-smart-retention.json", "m1")            # ds 0 retained, ds 1 neutral
    assert s.readiness.retained_ds == 1 and s.readiness.neutral_ds == 1
    assert s.ds[0].verdict == "retained" and s.ds[1].verdict == "none" and s.ds[1].allowed is None
    v = verdict([s])
    assert v.exit_code == EXIT_OK and any(w.startswith("COVERAGE_PARTIAL:m1") for w in v.warnings)
    assert not any("RETAINED" in w for w in v.warnings)
    v = verdict([s], require_full_coverage=True)
    assert v.exit_code == EXIT_DIFFER and any(e.startswith("COVERAGE_RETAINED:m1") for e in v.errors)
    assert any(e.startswith("COVERAGE_PARTIAL:m1") for e in v.errors)


def test_full_coverage_held_only_by_retained_verdicts_fails_the_gate():
    s = load("config-show-smart-retention.json", "m1")
    s.readiness.coverage, s.readiness.covered_ds, s.readiness.neutral_ds = "full", 2, 0
    s.readiness.retained_ds = 2
    assert verdict([s]).exit_code == EXIT_OK and verdict([s]).warnings == []
    v = verdict([s], require_full_coverage=True)
    assert v.exit_code == EXIT_DIFFER and [e.split(":")[0] for e in v.errors] == ["COVERAGE_RETAINED"]
    s.readiness.retained_ds = 0
    assert verdict([s], require_full_coverage=True).exit_code == EXIT_OK


def test_the_partial_message_names_the_neutral_data_stores_and_their_gate_reason():
    s = load("config-show-smart-retention.json", "m1")
    v = verdict([s])
    assert v.warnings == ["COVERAGE_PARTIAL:m1: 1 of 2 DS have a verdict in force (neutral: ds 1 no verdict)"]
    s.ds[1].reason = "CAPACITY_FULL"                               # a neutral DS the capacity gate excludes
    assert verdict([s]).warnings[0].endswith("(neutral: ds 1 CAPACITY_FULL)")


def test_invalid_connector_config_and_a_mismatch_stay_errors_with_steering_off():
    s = load("config-show-smart-steering-off.json", "m1", "smart")
    s.readiness.connector_config_valid = False
    v = verdict([s])
    assert v.exit_code == EXIT_DIFFER and "CONNECTOR_CONFIG_INVALID:m1" in v.errors
    assert any(w.startswith("STEERING_OFF:m1") for w in v.warnings)
    s = load("config-show-smart-steering-off.json", "m1", "rr")            # the file says rr, the daemon runs smart
    assert any(e.startswith("DESIRED_NE_EFFECTIVE:m1") for e in verdict([s]).errors)
    a, b = load("config-show-smart-steering-off.json", "m1"), smart2("m2")
    assert verdict([a, b]).exit_code == EXIT_OK                            # same digest, same profiles, same build
    b.profiles = {"xinas-mvp": "sha256:other"}
    assert any(e.startswith("CONNECTOR_PROFILES_MISMATCH") for e in verdict([a, b]).errors)


def test_differences_across_mds():
    a, b = smart2("m1"), smart2("m2")
    b.generation = "0" * 64
    v = verdict([a, b])
    assert v.exit_code == EXIT_DIFFER and any(e.startswith("GENERATION_MISMATCH") for e in v.errors)
    b = smart2("m2")
    b.build = {"wrr": 1, "connector": 0, "prealloc": 0}
    assert any(e.startswith("BUILD_MISMATCH") for e in verdict([a, b]).errors)
    b = smart2("m2")
    b.mode_effective = "fill"
    b.readiness = None
    assert any(e.startswith("MODE_MISMATCH") for e in verdict([a, b]).errors)
    b = smart2("m2")
    b.profiles = {"xinas-mvp": "sha256:other"}
    assert any(e.startswith("CONNECTOR_PROFILES_MISMATCH") for e in verdict([a, b]).errors)
    b = smart2("m2")
    b.profiles = dict(a.profiles, **{"zfs-mvp": "sha256:z"})
    assert any(e.startswith("CONNECTOR_PROFILES_MISMATCH") for e in verdict([a, b]).errors)


def test_desired_versus_effective_and_unreadable():
    a = smart2("m1", desired="rr")
    v = verdict([a])
    assert v.exit_code == EXIT_DIFFER and v.errors[0].startswith("DESIRED_NE_EFFECTIVE:m1: the file says rr, the daemon runs smart")
    a = smart2("m1", desired=None)          # unknown desired mode is not an error
    assert verdict([a]).exit_code == EXIT_OK
    bad = MdsState(host="m3", ok=False, error="mds-admin config show failed (rc 255): timeout")
    v = verdict([a, bad])
    assert v.exit_code == EXIT_UNREADABLE and v.errors == ["MDS_UNREADABLE:m3: mds-admin config show failed (rc 255): timeout"]
    assert len(v.per_mds) == 2


def test_legacy_pair_is_fine_and_a_lone_smart_has_no_digest_rule():
    a = load("config-show-legacy-mds2.json", "m1", "legacy")
    b = load("config-show-legacy-mds2.json", "m2", "legacy")
    v = verdict([a, b])
    assert v.exit_code == EXIT_OK and v.warnings == []
    lone = load("config-show-smart-mds1-none.json", "m1")
    v = verdict([lone])
    assert not any("DIGEST" in e for e in v.errors)                  # one MDS: nothing to compare
    assert any(e.startswith("COVERAGE_NONE:m1") for e in v.errors)   # a pre-retention MDS without steering


def test_render_show_lists_everything_and_warns_on_differences():
    a, b = smart2("m1"), smart2("m2")
    b.generation = "1" * 64
    out = render_show([a, b])
    assert "m1: desired=smart effective=smart generation=faf2d6bbd3bf kernel=58494e01 build=connector=1 prealloc=0 wrr=1" in out
    assert "readiness: mode_active=1 connector_config_valid=1 connector_reachable=1 last_batch_valid=1 coverage=partial registered=2 covered=1 eligible=1" in out
    assert "ds 0   ONLINE" in out and "assess=VALID allowed=1 ppm=1000000" in out and "reason=NONE" in out
    assert "ds 1   ONLINE" in out and "reason=NO_BINDING" in out
    assert "registered=2 covered=1 eligible=1 retained=0 neutral=1" in out      # an older MDS: neutral = registered - covered
    assert "verdict=" not in out and "hold_left=" not in out                    # an older MDS renders no verdict fields
    assert "metrics: eligible_ds=1 rejections=NO_BINDING=20" in out
    assert "WARN: MDS differ -- GENERATION_MISMATCH: m1=faf2d6bbd3bf, m2=111111111111" in out
    legacy = load("config-show-legacy-mds2.json", "m3")
    out = render_show([legacy])
    assert "m3: desired=? effective=legacy" in out and "readiness" not in out
    bad = MdsState(host="m4", ok=False, error="boom")
    assert render_show([bad]) == "m4: UNREADABLE: boom"
    a.metrics_error = "http://m1:9090/metrics: refused"
    assert "metrics: unavailable (http://m1:9090/metrics: refused)" in render_show([a])


def test_render_show_prints_verdict_hold_and_the_retained_neutral_counts():
    out = render_show([load("config-show-smart-retention.json", "m1", "smart")])
    assert ("readiness: mode_active=1 connector_config_valid=1 connector_reachable=1 last_batch_valid=1 "
            "coverage=partial registered=2 covered=1 eligible=2 retained=1 neutral=1") in out
    ds0 = next(line for line in out.splitlines() if line.startswith("  ds 0"))
    assert "assess=VALID allowed=1 ppm=1000000 ttl=412.0s age=188.0s" in ds0
    assert ds0.endswith("reason=NONE verdict=retained hold_left=412.0s")
    ds1 = next(line for line in out.splitlines() if line.startswith("  ds 1"))
    assert "assess=" not in ds1 and "allowed=" not in ds1                     # no verdict in force: nothing to assess
    assert ds1.endswith("weight=6422528000000 reason=NONE verdict=none hold_left=-")


def test_render_show_of_a_steering_off_mds():
    out = render_show([load("config-show-smart-steering-off.json", "m1", "smart")])
    assert "coverage=none registered=2 covered=0 eligible=2 retained=0 neutral=2" in out
    assert out.count("verdict=none hold_left=-") == 2
    assert "connector_reachable=0" in out and "last=socket /run/lattice-ds-connector/connector.sock: absent or refused" in out


def _with_profiles(host, raw):
    with open(os.path.join(FIX, "config-show-smart-mds2.json"), "r", encoding="utf-8") as fh:
        show = parse_config_show(fh.read())
    if raw is None:
        del show["placement_connector_profiles"]
    else:
        show["placement_connector_profiles"] = raw
    return state_from_show(host, show, None, "smart")


def test_an_unparsable_profile_map_is_an_error_on_a_single_mds():
    v = verdict([_with_profiles("m1", "not-a-profile-map")])
    assert v.exit_code == EXIT_DIFFER
    assert "CONNECTOR_PROFILES_INVALID:m1: not-a-profile-map" in v.errors


def test_the_same_unparsable_profile_map_on_two_mds_is_an_error():
    v = verdict([_with_profiles("m1", "not-a-profile-map"), _with_profiles("m2", "not-a-profile-map")])
    assert v.exit_code == EXIT_DIFFER
    assert {e for e in v.errors if e.startswith("CONNECTOR_PROFILES_INVALID")} == {
        "CONNECTOR_PROFILES_INVALID:m1: not-a-profile-map", "CONNECTOR_PROFILES_INVALID:m2: not-a-profile-map"}


def test_a_smart_mds_without_the_profile_row_is_an_error():
    v = verdict([_with_profiles("m1", None)])
    assert v.exit_code == EXIT_DIFFER and "CONNECTOR_PROFILES_MISSING:m1" in v.errors


def test_no_pins_yet_is_not_a_profile_error():
    v = verdict([_with_profiles("m1", "-")])
    assert not any(e.startswith("CONNECTOR_PROFILES_") for e in v.errors)


def test_show_marks_an_unparsable_profile_map():
    out = render_show([_with_profiles("m1", "not-a-profile-map")])
    assert "profiles=INVALID(not-a-profile-map)" in out
