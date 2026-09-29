# SPDX-License-Identifier: MIT
import os

from lattice_placement.live import (parse_build, parse_config_show, parse_ds_row, parse_metrics,
                                    parse_readiness, read_desired_mode, state_from_show)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as fh:
        return fh.read()


def test_parse_config_show_json_and_text():
    show = parse_config_show(fixture("config-show-legacy-mds2.json"))
    assert show["placement_mode"] == "legacy" and show["placement_mode_effective"] == "legacy"
    assert show["placement_kernel_id"] == "58494e01"
    assert "placement_readiness" not in show
    text = "placement_mode = fill\nplacement_ds.0 = domain=x state=ONLINE weight=5 reason=NONE\n"
    show = parse_config_show(text)
    assert show["placement_mode"] == "fill" and show["placement_ds.0"].startswith("domain=x")


def test_parse_ds_row_smart_and_none():
    show = parse_config_show(fixture("config-show-smart-mds2.json"))
    r0 = parse_ds_row(0, show["placement_ds.0"])
    assert r0.domain.endswith(":/dev/xi_data") and r0.state == "ONLINE"
    assert r0.capacity_age_ms == 1450 and r0.avail == 41423342665728 and r0.total == 42234482262016
    assert r0.assessment_age_ms == 448 and r0.quality == "VALID" and r0.allowed is True
    assert r0.ppm == 1000000 and r0.ttl_ms == 14866 and r0.weight == 6422528000000 and r0.reason == "NONE"
    assert r0.eligible
    r1 = parse_ds_row(1, show["placement_ds.1"])
    assert r1.domain == "ds:1" and r1.assessment_age_ms is None and r1.quality is None
    assert r1.allowed is False and r1.weight == 0 and r1.reason == "NO_BINDING" and not r1.eligible
    legacy = parse_config_show(fixture("config-show-legacy-mds2.json"))
    if "placement_ds.0" in legacy:
        lr = parse_ds_row(0, legacy["placement_ds.0"])
        assert lr.state == "ONLINE"


def test_parse_ds_row_neutral_and_retained():
    show = parse_config_show(fixture("config-show-smart-retention.json"))
    r0 = parse_ds_row(0, show["placement_ds.0"])
    assert r0.quality == "VALID" and r0.allowed is True and r0.ppm == 1000000
    assert r0.ttl_ms == 412000 and r0.assessment_age_ms == 188000
    assert r0.verdict == "retained" and r0.hold_left_ms == 412000 and r0.eligible
    r1 = parse_ds_row(1, show["placement_ds.1"])       # no verdict in force: placed neutrally
    assert r1.quality is None and r1.allowed is None and r1.ppm == 1000000 and r1.ttl_ms == 0
    assert r1.verdict == "none" and r1.hold_left_ms is None and r1.reason == "NONE" and r1.eligible
    assert r1.as_dict()["allowed"] is None and r1.as_dict()["verdict"] == "none"
    fresh = parse_ds_row(2, "domain=x state=ONLINE quality=VALID allowed=0 ppm=0 ttl_ms=5000 weight=0 "
                            "reason=CONNECTOR_DENIED verdict=fresh hold_left_ms=5000")
    assert fresh.verdict == "fresh" and fresh.allowed is False and fresh.hold_left_ms == 5000 and not fresh.eligible
    old = parse_ds_row(0, "domain=x state=ONLINE quality=VALID allowed=1 ppm=1000000 ttl_ms=14866 weight=5 reason=NONE")
    assert old.verdict is None and old.hold_left_ms is None      # an older MDS renders neither field


def test_parse_readiness_build_metrics():
    r = parse_readiness("mode_active=1 connector_config_valid=1 connector_reachable=0 last_batch_valid=0 "
                        "coverage=none registered_ds=2 covered_ds=0 eligible_ds=0")
    assert r.mode_active and r.connector_config_valid and not r.connector_reachable
    assert r.coverage == "none" and r.registered_ds == 2 and r.covered_ds == 0
    assert r.retained_ds == 0 and r.neutral_ds == 2            # an older MDS: neutral = registered - covered
    assert r.retention_aware is False                          # ... and it refuses a DS without a verdict
    r = parse_readiness("mode_active=1 connector_config_valid=1 connector_reachable=1 last_batch_valid=1 "
                        "coverage=partial registered_ds=2 covered_ds=1 eligible_ds=2 retained_ds=1 neutral_ds=1")
    assert r.eligible_ds == 2 and r.retained_ds == 1 and r.neutral_ds == 1
    assert r.retention_aware is True and r.as_dict()["retention_aware"] is True
    assert r.as_dict()["retained_ds"] == 1 and r.as_dict()["neutral_ds"] == 1
    assert parse_build("wrr=1 connector=1 prealloc=0") == {"wrr": 1, "connector": 1, "prealloc": 0}
    m = parse_metrics(fixture("metrics-smart-mds2.txt"))
    assert m["pnfs_mds_placement_eligible_ds"] == 1.0
    assert m['pnfs_mds_placement_rejections_total{reason="NO_BINDING"}'] == 20.0
    assert m["pnfs_mds_connector_reachable"] == 1.0
    assert m["pnfs_mds_placement_admit_seconds_count"] == 37.0
    assert not [k for k in m if not k.startswith("pnfs_mds_")]
    lm = parse_metrics(fixture("metrics-legacy-mds2.txt"))
    assert lm['pnfs_mds_placement_mode{mode="legacy"}'] == 1.0


def test_state_from_show_smart_partial_and_none():
    s2 = state_from_show("192.168.65.225", parse_config_show(fixture("config-show-smart-mds2.json")),
                         parse_metrics(fixture("metrics-smart-mds2.txt")), desired="smart")
    assert s2.ok and s2.mode_effective == "smart" and s2.desired_mode == "smart"
    assert s2.generation.startswith("faf2d6bbd3bf") and s2.kernel_id == "58494e01"
    assert s2.build == {"wrr": 1, "connector": 1, "prealloc": 0}
    assert s2.readiness.coverage == "partial" and s2.readiness.covered_ds == 1 and s2.readiness.eligible_ds == 1
    assert s2.config_digest.startswith("sha256:30259fc1") and s2.profiles["xinas-mvp"].startswith("sha256:c9bee5b2")
    assert s2.last_detail == "accepted 1"
    assert [r.ds_id for r in s2.ds] == [0, 1] and s2.ds[1].reason == "NO_BINDING"
    assert s2.metrics["pnfs_mds_connector_covered_ds"] == 1.0
    s1 = state_from_show("192.168.65.223", parse_config_show(fixture("config-show-smart-mds1-none.json")))
    assert s1.readiness.coverage == "none" and not s1.readiness.connector_reachable
    assert s1.config_digest is None and s1.profiles is None       # "-" means none
    assert s1.last_detail.startswith("socket /run/lattice-ds-connector/connector.sock")
    assert all(r.reason == "MODE_NOT_READY" for r in s1.ds)
    legacy = state_from_show("x", parse_config_show(fixture("config-show-legacy-mds2.json")))
    assert legacy.mode_effective == "legacy" and legacy.readiness is None and legacy.build == {}


def test_state_from_show_retention_and_steering_off():
    s = state_from_show("m1", parse_config_show(fixture("config-show-smart-retention.json")), desired="smart")
    assert s.readiness.coverage == "partial" and s.readiness.retained_ds == 1 and s.readiness.neutral_ds == 1
    assert [r.verdict for r in s.ds] == ["retained", "none"] and s.profiles_row and s.profiles_error is None
    off = state_from_show("m1", parse_config_show(fixture("config-show-smart-steering-off.json")), desired="smart")
    assert off.readiness.coverage == "none" and not off.readiness.connector_reachable
    assert off.readiness.eligible_ds == 2 and off.readiness.neutral_ds == 2 and off.readiness.retained_ds == 0
    assert all(r.verdict == "none" and r.allowed is None and r.eligible for r in off.ds)
    assert off.profiles["xinas-mvp"].startswith("sha256:c9bee5b2") and off.config_digest.startswith("sha256:30259fc1")
    assert off.last_detail.startswith("socket /run/lattice-ds-connector/connector.sock")


def test_new_gauges_reach_the_metrics_map():
    m = parse_metrics("pnfs_mds_placement_neutral_ds 1\npnfs_mds_placement_retained_ds 2\n"
                      "pnfs_mds_connector_verdicts_expired_total 3\nunrelated_total 9\n")
    assert m == {"pnfs_mds_placement_neutral_ds": 1.0, "pnfs_mds_placement_retained_ds": 2.0,
                 "pnfs_mds_connector_verdicts_expired_total": 3.0}


def test_is_local_address():
    from lattice_placement.live import is_local_address
    assert is_local_address("127.0.0.1")
    assert is_local_address("localhost")
    assert not is_local_address("192.0.2.1")          # TEST-NET-1: never ours
    assert not is_local_address("no-such-host.invalid")


def test_read_desired_mode_local_and_ssh(tmp_path, monkeypatch):
    cfg = tmp_path / "mds.conf"
    cfg.write_text("placement_policy = wrr\n")
    assert read_desired_mode(str(cfg)) == "legacy"
    cfg.write_text("placement_mode = fill\n")
    assert read_desired_mode(str(cfg)) == "fill"
    assert read_desired_mode(str(tmp_path / "missing")) is None
    # a fake ssh on PATH prints a file named by the last argument's basename
    fake = tmp_path / "ssh"
    fake.write_text("#!/bin/sh\nfor a in \"$@\"; do f=\"$a\"; done\ncat \"%s/$(basename \"$f\")\"\n" % tmp_path)
    os.chmod(fake, 0o755)
    (tmp_path / "remote.conf").write_text("placement_mode = smart\n")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert read_desired_mode("/etc/pnfs-mds/remote.conf", ssh_target="root@10.0.0.1") == "smart"
    assert read_desired_mode("/etc/pnfs-mds/nope.conf", ssh_target="root@10.0.0.1") is None
    # a local address reads the file directly, never through ssh
    assert read_desired_mode(str(cfg), ssh_target="root@127.0.0.1") == "fill"
