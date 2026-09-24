# SPDX-License-Identifier: MIT
import json

from lattice_placement.cli import main

LEGACY = "mds_id = 1\nplacement_policy_enabled = true\nplacement_policy = wrr\nds_weight.0 = 55\nds_weight.1 = 45\n"


def test_set_is_a_dry_run_by_default(tmp_path, capsys):
    cfg = tmp_path / "mds.conf"
    cfg.write_text(LEGACY)
    assert main(["mode", "set", "smart", "--config", str(cfg), "--set", "ds_connector_poll_ms=1000"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN") and "+placement_mode = smart" in out and "-ds_weight.0 = 55" in out
    assert "NOTE: smart admits no DS" in out
    assert cfg.read_text() == LEGACY                      # untouched


def test_set_apply_writes_backup_and_audit(tmp_path, capsys):
    cfg = tmp_path / "mds.conf"
    cfg.write_text(LEGACY)
    audit = tmp_path / "audit.log"
    assert main(["mode", "set", "fill", "--config", str(cfg), "--apply", "--audit-log", str(audit), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] is True and data["plan"]["old_mode"] == "legacy" and data["plan"]["mode"] == "fill"
    assert "placement_mode = fill" in cfg.read_text() and "ds_weight" not in cfg.read_text()
    assert data["backup"].startswith(str(cfg))
    assert json.loads(audit.read_text())["new_mode"] == "fill"
    # again: nothing to do
    assert main(["mode", "set", "fill", "--config", str(cfg), "--apply", "--audit-log", str(audit)]) == 0
    assert capsys.readouterr().out.startswith("NO CHANGE")
    assert len(audit.read_text().splitlines()) == 1
    # back to legacy with weights, the health-veto warning appears only when leaving smart
    assert main(["mode", "set", "smart", "--config", str(cfg), "--apply", "--audit-log", str(audit)]) == 0
    capsys.readouterr()
    assert main(["mode", "set", "legacy", "--config", str(cfg), "--apply", "--audit-log", str(audit),
                 "--ds-weight", "0=55", "--ds-weight", "1=45"]) == 0
    out = capsys.readouterr().out
    assert "applied:" in out and "WARNING: leaving smart" in out and "still uses mode smart" in out
    text = cfg.read_text()
    assert "placement_mode" not in text and "ds_weight.0 = 55" in text and "placement_policy = wrr" in text


def test_set_refusals_and_errors(tmp_path, capsys):
    cfg = tmp_path / "mds.conf"
    cfg.write_text(LEGACY)
    assert main(["mode", "set", "fill", "--config", str(cfg), "--set", "bogus=1"]) == 1
    assert capsys.readouterr().out.startswith("REFUSED")
    assert main(["mode", "set", "fill", "--config", str(cfg), "--set", "novalue"]) == 2
    assert main(["mode", "set", "fill", "--config", str(tmp_path / "missing")]) == 2
    assert main(["mode", "set", "smart", "--config", str(cfg), "--set", "ds_capacity_poll_ms=0", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["applied"] is False and any("RANGE" in e for e in data["refused"])
    assert cfg.read_text() == LEGACY
