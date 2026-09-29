# SPDX-License-Identifier: MIT
import json
import os
import shutil

from lattice_placement.cli import main

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fake_mds_admin(tmp_path, mapping):
    """A stand-in for mds-admin: `config show --mds-host H … --json` prints
    the fixture mapped to H; an unmapped host fails like a dead daemon."""
    lines = ["#!/bin/sh", "host=''", "prev=''",
             "for a in \"$@\"; do if [ \"$prev\" = --mds-host ]; then host=\"$a\"; fi; prev=\"$a\"; done",
             "case \"$host\" in"]
    for host, fixture in mapping.items():
        lines.append("  %s) cat '%s';;" % (host, os.path.join(FIX, fixture)))
    lines += ["  *) echo 'Error: config show failed (-2)' >&2; exit 1;;", "esac"]
    p = tmp_path / "mds-admin"
    p.write_text("\n".join(lines) + "\n")
    os.chmod(p, 0o755)
    return str(p)


def test_show_renders_two_mds_and_desired_from_config(tmp_path, capsys):
    admin = fake_mds_admin(tmp_path, {"10.0.0.1": "config-show-smart-mds2.json", "10.0.0.2": "config-show-smart-mds2.json"})
    cfg = tmp_path / "mds.conf"
    cfg.write_text("placement_mode = smart\n")
    rc = main(["mode", "show", "--mds", "10.0.0.1,10.0.0.2", "--mds-admin", admin, "--no-metrics",
               "--config", str(cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "10.0.0.1: desired=smart effective=smart" in out and "10.0.0.2: desired=? effective=smart" in out
    assert "WARN" not in out
    rc = main(["mode", "show", "--mds", "10.0.0.1", "--mds-admin", admin, "--no-metrics", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mds"][0]["readiness"]["coverage"] == "partial"


def test_verify_exit_codes(tmp_path, capsys):
    admin = fake_mds_admin(tmp_path, {"10.0.0.1": "config-show-smart-mds2.json",
                                      "10.0.0.2": "config-show-smart-mds1-none.json",
                                      "10.0.0.3": "config-show-smart-mds2.json"})
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.3", "--mds-admin", admin, "--no-metrics"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK") and "COVERAGE_PARTIAL" in out
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.3", "--mds-admin", admin, "--no-metrics",
                 "--require-full-coverage"]) == 1
    assert "error:   COVERAGE_PARTIAL" in capsys.readouterr().out
    # 10.0.0.2 is an older MDS without a connector: no steering is only a warning, the digest spread is the error
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.2", "--mds-admin", admin, "--no-metrics", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and any(e.startswith("CONNECTOR_CONFIG_DIGEST_MISMATCH") for e in data["errors"])
    assert not any(e.startswith("COVERAGE_NONE") for e in data["errors"])
    assert any(w.startswith("STEERING_OFF:10.0.0.2") for w in data["warnings"])
    assert any(w.startswith("CONNECTOR_UNREACHABLE:10.0.0.2") for w in data["warnings"])
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.9", "--mds-admin", admin, "--no-metrics"]) == 2
    assert "MDS_UNREADABLE:10.0.0.9" in capsys.readouterr().out


def test_verify_a_smart_cluster_without_steering_is_ok_with_warnings(tmp_path, capsys):
    admin = fake_mds_admin(tmp_path, {"10.0.0.1": "config-show-smart-steering-off.json",
                                      "10.0.0.2": "config-show-smart-steering-off.json"})
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.2", "--mds-admin", admin, "--no-metrics"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK\n") and "error:" not in out
    assert "warning: STEERING_OFF:10.0.0.1" in out and "warning: CONNECTOR_UNREACHABLE:10.0.0.2" in out
    assert "verdict=none hold_left=-" in out and "retained=0 neutral=2" in out
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.2", "--mds-admin", admin, "--no-metrics",
                 "--require-full-coverage"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("NOT OK\n") and "error:   STEERING_OFF:10.0.0.1" in out
    assert main(["mode", "verify", "--mds", "10.0.0.1", "--mds-admin", admin, "--no-metrics", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["errors"] == []
    assert data["mds"][0]["readiness"]["neutral_ds"] == 2 and data["mds"][0]["ds"][0]["verdict"] == "none"
    assert data["mds"][0]["ds"][0]["allowed"] is None


def test_verify_retained_verdicts_fail_only_full_coverage(tmp_path, capsys):
    admin = fake_mds_admin(tmp_path, {"10.0.0.1": "config-show-smart-retention.json",
                                      "10.0.0.2": "config-show-smart-retention.json"})
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.2", "--mds-admin", admin, "--no-metrics"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK\n") and "COVERAGE_PARTIAL" in out and "COVERAGE_RETAINED" not in out
    assert "verdict=retained hold_left=412.0s" in out
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.2", "--mds-admin", admin, "--no-metrics",
                 "--require-full-coverage"]) == 1
    out = capsys.readouterr().out
    assert "error:   COVERAGE_RETAINED:10.0.0.1: 1 DS held by retained verdicts" in out


def test_verify_desired_over_ssh_and_env_passthrough(tmp_path, capsys, monkeypatch):
    admin = fake_mds_admin(tmp_path, {"10.0.0.1": "config-show-smart-mds2.json"})
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/bin/sh\necho 'placement_mode = fill'\n")
    os.chmod(fake_ssh, 0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert main(["mode", "verify", "--mds", "10.0.0.1", "--mds-admin", admin, "--no-metrics",
                 "--ssh", "root", "--env", "LD_LIBRARY_PATH=/opt/rondb/lib"]) == 1
    out = capsys.readouterr().out
    assert "DESIRED_NE_EFFECTIVE:10.0.0.1: the file says fill, the daemon runs smart" in out


def test_show_needs_mds(capsys):
    assert main(["mode", "show", "--mds", "", "--no-metrics"]) == 2
