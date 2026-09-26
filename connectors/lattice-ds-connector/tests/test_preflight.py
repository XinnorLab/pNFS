# SPDX-License-Identifier: MIT
"""``preflight``: the read-only readiness report an operator runs on an MDS
before switching it to smart placement (design section 11)."""

import copy
import json

import pytest

from lattice_ds_connector.preflight import evaluate, render


def batch(*records, contract="1.0"):
    return {
        "contract_version": contract,
        "runtime_epoch": "host:2026-09-24:1",
        "config_digest": "sha256:cfg",
        "generated_at": "2026-09-24T10:00:00Z",
        "instances": [
            {
                "connector_instance_id": "xi-01",
                "module_type": "xinas",
                "epoch": "e1",
                "sequence": 7,
                "snapshot_status": "COMPLETE",
                "assessments": list(records),
            }
        ],
    }


def record(ds_id, quality="VALID", allowed=True, ppm=1000000, ttl=15000, domain="ctrl/fs-1", datastore="ctrl", profile="sha256:p", pid="xinas-mvp", endpoint=None):
    d = {
        "ds_id": ds_id,
        "binding_generation": 2,
        "datastore_id": datastore,
        "target_id": "share-%d" % ds_id,
        "target_incarnation": "share:%d:u" % ds_id,
        "quality": quality,
        "remaining_ttl_ms": ttl,
        "profile": {"id": pid, "version": "1", "digest": profile},
        "placement": {"allowed": allowed, "multiplier_ppm": ppm, "reason_codes": ["NORMAL"]},
        "resources": {"capacity_domain_id": domain, "shared_resource_ids": []},
    }
    if endpoint is not None:
        d["endpoint"] = endpoint
    return d


HEALTH_OK = {"ready": True, "running": True}


def test_ready_on_a_healthy_batch():
    r = evaluate(HEALTH_OK, batch(record(0), record(1, domain="ctrl/fs-2")), expect_ds=[0, 1])
    assert r["ready"] and r["reasons"] == []
    assert r["profiles"] == {"xinas-mvp": "sha256:p"} and r["config_digest"] == "sha256:cfg"
    assert [d["ds_id"] for d in r["ds"]] == [0, 1]
    text = render(r)
    assert text.startswith("READY") and "ds   0 VALID" in text
    assert "profiles=xinas-mvp=sha256:p" in text


@pytest.mark.parametrize(
    "mutate, expect, reason",
    [
        (lambda b: None, [0, 5], "UNBOUND:ds5"),
        (lambda b: b["instances"][0]["assessments"][0].__setitem__("quality", "UNKNOWN"), [0], "UNKNOWN:ds0"),
        (lambda b: b["instances"][0]["assessments"][0].__setitem__("remaining_ttl_ms", 0), [0], "EXPIRED:ds0"),
        (lambda b: b["instances"][0].__setitem__("snapshot_status", "FAILED"), [0], "UNKNOWN:ds0"),
        (lambda b: b.__setitem__("contract_version", "2.0"), [0], "CONTRACT_MAJOR:2.0"),
        (lambda b: b["instances"][0]["assessments"].append(record(1, datastore="other")), [0, 1], "DOMAIN_INCONSISTENT:ctrl/fs-1"),
        (lambda b: b["instances"][0]["assessments"].append(record(1, profile="sha256:q", domain="d2")), [0, 1], "PROFILE_INCONSISTENT:xinas-mvp"),
        (lambda b: b["instances"][0]["assessments"].append(record(0)), [0], "DUPLICATE_DS:0"),
    ],
)
def test_not_ready_reasons(mutate, expect, reason):
    b = batch(record(0))
    mutate(b)
    r = evaluate(HEALTH_OK, b, expect_ds=expect)
    assert not r["ready"]
    assert reason in r["reasons"], r["reasons"]
    assert render(r).startswith("NOT_READY")


def test_connector_health_and_unavailable_batch():
    r = evaluate({"ready": False}, batch(record(0)), expect_ds=[0])
    assert "CONNECTOR_NOT_READY" in r["reasons"]
    r = evaluate(None, None, expect_ds=[0])
    assert r["reasons"] == ["HEALTHZ_UNAVAILABLE", "ASSESSMENTS_UNAVAILABLE"] and r["ds"] == []


def test_two_aliases_on_one_domain_are_consistent():
    r = evaluate(HEALTH_OK, batch(record(0), record(1)), expect_ds=[0, 1])
    assert r["ready"]


def test_two_profiles_in_one_batch_are_ready():
    b = batch(record(0), record(1, pid="zfs-mvp", profile="sha256:z", domain="d2"))
    r = evaluate(HEALTH_OK, b, expect_ds=[0, 1])
    assert r["ready"], r["reasons"]
    assert r["profiles"] == {"xinas-mvp": "sha256:p", "zfs-mvp": "sha256:z"}


def test_expect_profiles():
    b = batch(record(0), record(1, pid="zfs-mvp", profile="sha256:z", domain="d2"))
    ok = evaluate(HEALTH_OK, b, expect_profiles={"xinas-mvp": "sha256:p", "zfs-mvp": "sha256:z"})
    assert ok["ready"], ok["reasons"]
    r = evaluate(HEALTH_OK, b, expect_profiles={"xinas-mvp": "sha256:OTHER"})
    assert "PROFILE_PIN_MISMATCH:xinas-mvp" in r["reasons"]
    assert "PROFILE_NOT_PINNED:zfs-mvp" in r["reasons"]


def test_parse_and_format_profile_pins():
    from lattice_ds_connector.preflight import format_profiles, parse_profile_pins
    assert parse_profile_pins(" zfs-mvp=sha256:z, xinas-mvp=sha256:p ") == {"xinas-mvp": "sha256:p", "zfs-mvp": "sha256:z"}
    assert format_profiles({"zfs-mvp": "z", "xinas-mvp": "p"}) == "xinas-mvp=p,zfs-mvp=z"
    assert format_profiles({}) == "-"
    for bad in ("", "x", "a b=d", "a=d,a=e", "a=", ",".join("p%d=d" % i for i in range(9)), "a\nb=d",
               "a=" + "d" * 128):
        with pytest.raises(ValueError):
            parse_profile_pins(bad)
    assert parse_profile_pins("a=" + "d" * 127) == {"a": "d" * 127}


def test_preflight_cli_over_the_socket(tmp_path, capsys):
    """End to end with the fixture module: READY, then NOT_READY for an unbound id."""
    import os, pathlib, shutil, tempfile
    from conftest import make_binding
    from lattice_ds_connector.cli import main
    from lattice_ds_connector.config import Config, Instance, RuntimeConfig
    from lattice_ds_connector.log import Logger
    from lattice_ds_connector.runtime import Runtime
    from lattice_ds_connector.server import Server

    short = pathlib.Path(tempfile.mkdtemp(prefix="ldc-", dir="/tmp"))
    try:
        fixture = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "fixture-module", "zfs-shaped-healthy.json")
        fx = short / "zfs.json"
        fx.write_text(open(fixture, encoding="utf-8").read())
        b = make_binding(2, "dataset-a", "/pool/dataset-a", "dataset-a-inc-1", networks=())
        b = b.__class__(**{**b.__dict__, "datastore_id": "fixture-zfs-01"})
        inst = Instance(id="fixture-01", module="fixture", bindings=(b,), fixture_file=str(fx))
        cfg = Config("1.0", True, RuntimeConfig(socket_path=str(short / "c.sock"), collect_interval_ms=1000, collect_deadline_ms=200), {}, (inst,), "sha256:" + "1" * 64)
        rt = Runtime(cfg, Logger(stream=open("/dev/null", "w")))
        server = Server(rt, cfg.runtime.socket_path)
        server.start()
        try:
            rt.instances["fixture-01"].run_cycle()
            for _ in range(3):   # hold-down: two distinct cycles
                rt.instances["fixture-01"].run_cycle()
            rc = main(["preflight", "--socket", cfg.runtime.socket_path, "--expect-ds", "2"])
            out = capsys.readouterr().out
            assert "ds   2" in out
            assert rc in (0, 1)   # the fixture may still be in its hold-down; the report itself is what we check
            rc = main(["preflight", "--socket", cfg.runtime.socket_path, "--expect-ds", "2,9", "--json"])
            doc = json.loads(capsys.readouterr().out)
            assert rc == 1 and "UNBOUND:ds9" in doc["reasons"]
            assert doc["ds"][0]["ds_id"] == 2
        finally:
            server.stop()
            rt.stop(1.0)
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_rows_show_ds_path():
    b = batch(record(0, endpoint={"server": "s", "export_path": "/mnt/data", "ds_path": "/mnt/data/pnfs-ds"}),
              record(1, domain="d2"))
    r = evaluate(HEALTH_OK, b)
    rows = {d["ds_id"]: d for d in r["ds"]}
    assert rows[0]["ds_path"] == "/mnt/data/pnfs-ds" and rows[1]["ds_path"] is None
    text = render(r)
    assert "ds_path=/mnt/data/pnfs-ds" in text and "ds_path=-" in text
