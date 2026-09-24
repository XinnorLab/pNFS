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
    assert len(v.warnings) == 2 and v.warnings[0].startswith("COVERAGE_PARTIAL:m1: 1 of 2 DS eligible (ds 1 NO_BINDING)")
    v = verdict([a, b], require_full_coverage=True)
    assert v.exit_code == EXIT_DIFFER and any(e.startswith("COVERAGE_PARTIAL:m1") for e in v.errors)


def test_coverage_none_and_unreachable_are_errors():
    a = smart2("m2")
    b = load("config-show-smart-mds1-none.json", "m1", "smart")
    v = verdict([a, b])
    assert v.exit_code == EXIT_DIFFER
    assert any(e.startswith("CONNECTOR_UNREACHABLE:m1") for e in v.errors)
    assert any(e.startswith("COVERAGE_NONE:m1") for e in v.errors)
    assert any(e.startswith("CONNECTOR_CONFIG_DIGEST_MISMATCH") for e in v.errors)   # m1 has no digest


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
    b.profile_digest = "sha256:other"
    assert any(e.startswith("CONNECTOR_PROFILE_DIGEST_MISMATCH") for e in verdict([a, b]).errors)


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
    assert not any("DIGEST" in e for e in v.errors) and any(e.startswith("COVERAGE_NONE") for e in v.errors)


def test_render_show_lists_everything_and_warns_on_differences():
    a, b = smart2("m1"), smart2("m2")
    b.generation = "1" * 64
    out = render_show([a, b])
    assert "m1: desired=smart effective=smart generation=faf2d6bbd3bf kernel=58494e01 build=connector=1 prealloc=0 wrr=1" in out
    assert "readiness: mode_active=1 connector_config_valid=1 connector_reachable=1 last_batch_valid=1 coverage=partial registered=2 covered=1 eligible=1" in out
    assert "ds 0   ONLINE" in out and "assess=VALID allowed=1 ppm=1000000" in out and "reason=NONE" in out
    assert "ds 1   ONLINE" in out and "reason=NO_BINDING" in out
    assert "metrics: eligible_ds=1 rejections=NO_BINDING=20" in out
    assert "WARN: MDS differ -- GENERATION_MISMATCH: m1=faf2d6bbd3bf, m2=111111111111" in out
    legacy = load("config-show-legacy-mds2.json", "m3")
    out = render_show([legacy])
    assert "m3: desired=? effective=legacy" in out and "readiness" not in out
    bad = MdsState(host="m4", ok=False, error="boom")
    assert render_show([bad]) == "m4: UNREADABLE: boom"
    a.metrics_error = "http://m1:9090/metrics: refused"
    assert "metrics: unavailable (http://m1:9090/metrics: refused)" in render_show([a])
