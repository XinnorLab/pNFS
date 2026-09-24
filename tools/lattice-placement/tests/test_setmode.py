# SPDX-License-Identifier: MIT
import json
import os
from datetime import datetime, timezone

import pytest

from lattice_placement import manifest as manifest_mod
from lattice_placement.ini import IniDocument
from lattice_placement.setmode import SetRefused, apply_plan, plan_set, sha256_text

M = manifest_mod.load()
BEGIN, END = M.managed_block

# the lab file as pm-deploy.sh leaves it (legacy)
LAB_LEGACY = """mds_id = 2
hostname = 192.168.65.225
grpc_port = 50051
cluster_bind_addr = 192.168.65.225
# capacity probe
ds_capacity_poll_ms = 10000
ds_capacity_poll_ms = 10000
ds_capacity_poll_ms = 10000
placement_policy_enabled = true
placement_policy = wrr
ds_weight.0 = 55
ds_weight.1 = 45
"""


def test_plan_smart_from_legacy_removes_legacy_keys_and_appends_the_block():
    p = plan_set(LAB_LEGACY, "smart", {"ds_connector_poll_ms": "1000",
                                       "ds_connector_request_deadline_ms": "500"}, M)
    assert p.old_mode == "legacy" and p.mode == "smart" and p.changed
    assert p.removed_keys == ["placement_policy_enabled", "placement_policy", "ds_weight.0", "ds_weight.1"]
    doc = IniDocument.parse(p.after)
    assert doc.get("placement_mode") == "smart"
    assert doc.get("placement_policy") is None and doc.prefixed("ds_weight.") == {}
    assert doc.count("ds_capacity_poll_ms") == 3            # untouched lines stay, duplicates included
    assert "# capacity probe" in p.after
    assert doc.managed_pairs(BEGIN, END) == [("placement_mode", "smart"), ("ds_connector_poll_ms", "1000"),
                                             ("ds_connector_request_deadline_ms", "500")]
    assert "-placement_policy = wrr" in p.diff and "+placement_mode = smart" in p.diff
    assert any("no DS until the first fresh VALID" in n for n in p.notes)
    assert not any("health veto" in w for w in p.warnings)


def test_plan_legacy_from_smart_restores_legacy_keys():
    smart = plan_set(LAB_LEGACY, "smart", {}, M).after
    p = plan_set(smart, "legacy", {}, M, legacy_policy="wrr", ds_weights={0: 55, 1: 45})
    assert p.old_mode == "smart"
    doc = IniDocument.parse(p.after)
    assert doc.get("placement_mode") is None
    assert doc.get("placement_policy") == "wrr" and doc.get("placement_policy_enabled") == "true"
    assert doc.prefixed("ds_weight.") == {"0": "55", "1": "45"}
    assert any("health veto" in w for w in p.warnings)
    # a stray mode key outside the block is removed too
    p2 = plan_set(smart + "placement_min_free_bytes = 5\n", "legacy", {}, M)
    assert "placement_min_free_bytes" not in p2.after and "placement_min_free_bytes" in p2.removed_keys


def test_plan_replaces_an_existing_block_and_strays():
    fill = plan_set(LAB_LEGACY, "fill", {"placement_min_free_bytes": "1073741824"}, M).after
    p = plan_set(fill + "placement_mode = rr\n", "rr", {}, M)   # a stray key after the block would win in config.c
    doc = IniDocument.parse(p.after)
    assert p.after.count(BEGIN) == 1 and p.after.count(END) == 1
    assert doc.count("placement_mode") == 1 and doc.get("placement_mode") == "rr"
    assert "placement_min_free_bytes" not in p.after
    assert plan_set(p.after, "rr", {}, M).changed is False


def test_plan_refusals():
    with pytest.raises(SetRefused, match="unknown key"):
        plan_set(LAB_LEGACY, "fill", {"foo": "1"}, M)
    with pytest.raises(SetRefused, match="does not apply"):
        plan_set(LAB_LEGACY, "fill", {"ds_connector_poll_ms": "1000"}, M)
    with pytest.raises(SetRefused, match="derived"):
        plan_set(LAB_LEGACY, "smart", {"ds_connector_enabled": "true"}, M)
    with pytest.raises(SetRefused, match="RANGE"):
        plan_set(LAB_LEGACY, "fill", {"ds_capacity_poll_ms": "0"}, M)
    with pytest.raises(SetRefused, match="RANGE"):
        plan_set(LAB_LEGACY, "smart", {"ds_connector_socket": "relative.sock"}, M)
    with pytest.raises(SetRefused, match="legacy-policy"):
        plan_set(LAB_LEGACY, "legacy", {}, M, legacy_policy="sideways")
    with pytest.raises(SetRefused, match="unknown mode"):
        plan_set(LAB_LEGACY, "sideways", {}, M)
    # a profile that sets a placement policy cannot take a mode
    with pytest.raises(SetRefused, match="PLACEMENT_MODE_CONFLICT"):
        plan_set("workload_profile = hpc\n", "rr", {}, M)


def test_apply_backup_atomic_audit(tmp_path):
    cfg = tmp_path / "mds.conf"
    cfg.write_text(LAB_LEGACY)
    os.chmod(cfg, 0o640)
    audit = tmp_path / "var" / "audit.log"
    plan = plan_set(LAB_LEGACY, "smart", {"ds_connector_poll_ms": "1000"}, M, path=str(cfg))
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    res = apply_plan(str(cfg), plan, M, str(audit), now=now, user="root", sudo_user="sergey", host="node225")
    assert cfg.read_text() == plan.after
    assert os.path.exists(res.backup) and open(res.backup).read() == LAB_LEGACY
    assert res.backup.startswith(str(cfg) + ".20260925-100000")
    assert (os.stat(cfg).st_mode & 0o777) == 0o640
    assert not [p for p in os.listdir(tmp_path) if "lattice-placement" in p]    # no temp file left
    line = json.loads(audit.read_text().splitlines()[-1])
    assert line["user"] == "root" and line["sudo_user"] == "sergey" and line["host"] == "node225"
    assert line["old_mode"] == "legacy" and line["new_mode"] == "smart"
    assert line["old_sha256"] == sha256_text(LAB_LEGACY) and line["new_sha256"] == sha256_text(plan.after)
    assert line["backup"] == res.backup and line["ts"] == "2026-09-25T10:00:00Z"
    assert line["removed_keys"] == ["placement_policy_enabled", "placement_policy", "ds_weight.0", "ds_weight.1"]
    # a second apply of the same (now stale) plan is refused
    with pytest.raises(SetRefused, match="changed since"):
        apply_plan(str(cfg), plan, M, str(audit), now=now)
    # two applies in the same second get distinct backups
    plan2 = plan_set(cfg.read_text(), "rr", {}, M)
    res2 = apply_plan(str(cfg), plan2, M, str(audit), now=now)
    assert res2.backup != res.backup and res2.backup.endswith("-1.bak")
    assert len(audit.read_text().splitlines()) == 2


def test_apply_restores_the_backup_when_the_result_fails(tmp_path, monkeypatch):
    cfg = tmp_path / "mds.conf"
    cfg.write_text(LAB_LEGACY)
    plan = plan_set(LAB_LEGACY, "fill", {}, M)
    from lattice_placement import setmode

    def broken_validate(doc, mode, manifest, assume_set=False):
        from lattice_placement.validate import Report
        return Report(ready=False, mode=mode, errors=["RANGE: injected"])

    monkeypatch.setattr(setmode, "validate_document", broken_validate)
    with pytest.raises(SetRefused, match="backup restored"):
        apply_plan(str(cfg), plan, M, str(tmp_path / "audit.log"))
    assert cfg.read_text() == LAB_LEGACY
    assert not (tmp_path / "audit.log").exists()
