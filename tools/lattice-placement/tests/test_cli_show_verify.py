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
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.2", "--mds-admin", admin, "--no-metrics", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and any(e.startswith("COVERAGE_NONE:10.0.0.2") for e in data["errors"])
    assert main(["mode", "verify", "--mds", "10.0.0.1,10.0.0.9", "--mds-admin", admin, "--no-metrics"]) == 2
    assert "MDS_UNREADABLE:10.0.0.9" in capsys.readouterr().out


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
