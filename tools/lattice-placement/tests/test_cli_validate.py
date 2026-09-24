# SPDX-License-Identifier: MIT
import json
import os
import stat

from lattice_placement.cli import main


def write(tmp_path, name, text, mode=0o644):
    p = tmp_path / name
    p.write_text(text)
    os.chmod(p, mode)
    return str(p)


def fake_connector(tmp_path, ready, reasons=()):
    """A stand-in for `lattice-ds-connector preflight --json`."""
    body = json.dumps({"ready": ready, "reasons": list(reasons), "ds": []})
    script = "#!/bin/sh\ncase \"$*\" in *preflight*) echo '%s';; *) exit 3;; esac\n" % body
    return write(tmp_path, "fake-connector", script, 0o755)


def test_validate_exit_codes(tmp_path, capsys):
    cfg = write(tmp_path, "mds.conf", "placement_mode = fill\nds_capacity_poll_ms = 10000\n")
    assert main(["mode", "validate", "fill", "--config", cfg]) == 0
    out = capsys.readouterr().out
    assert out.startswith("READY\n") and "placement_mode = fill" in out
    bad = write(tmp_path, "bad.conf", "placement_mode = fill\nds_capacity_poll_ms = 0\n")
    assert main(["mode", "validate", "fill", "--config", bad]) == 1
    out = capsys.readouterr().out
    assert out.startswith("NOT_READY\n") and "error:   RANGE" in out
    assert main(["mode", "validate", "fill", "--config", str(tmp_path / "missing.conf")]) == 2


def test_validate_json(tmp_path, capsys):
    cfg = write(tmp_path, "mds.conf", "placement_policy = wrr\n")
    assert main(["mode", "validate", "rr", "--config", cfg, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ready"] is True and data["mode"] == "rr"
    assert any("legacy keys present" in w for w in data["warnings"])
    assert main(["mode", "validate", "rr", "--config", cfg, "--strict", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ready"] is False


def test_validate_smart_uses_the_connector(tmp_path, capsys):
    cfg = write(tmp_path, "mds.conf", "placement_mode = smart\n")
    good = fake_connector(tmp_path, True)
    assert main(["mode", "validate", "smart", "--config", cfg, "--connector-cli", good,
                 "--connector-socket", "/tmp/x.sock", "--expect-ds", "0,1"]) == 0
    assert "connector: ready=True" in capsys.readouterr().out
    bad = fake_connector(tmp_path, False, ["CONNECTOR_NOT_READY"])
    assert main(["mode", "validate", "smart", "--config", cfg, "--connector-cli", bad, "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert "CONNECTOR:CONNECTOR_NOT_READY" in data["errors"]
    assert data["connector"]["ready"] is False
    # no connector CLI at all: NOT_READY, never a crash
    assert main(["mode", "validate", "smart", "--config", cfg, "--connector-cli", str(tmp_path / "nope")]) == 1
    assert "CONNECTOR:UNAVAILABLE" in capsys.readouterr().out
