# SPDX-License-Identifier: MIT
"""The Unix-socket read-only server (CON-01, CON-22) and the fixture module
end to end (T-02, T-35)."""

import json
import os
import stat
import time

import pytest

from conftest import make_binding, validate
from lattice_ds_connector.config import Config, Instance, RuntimeConfig
from lattice_ds_connector.log import Logger
from lattice_ds_connector.runtime import Runtime
from lattice_ds_connector.server import Server, get_json

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(os.path.dirname(HERE), "fixtures", "fixture-module", "zfs-shaped-healthy.json")


@pytest.fixture
def served(tmp_path):
    # macOS caps AF_UNIX paths at 104 bytes; pytest's tmp_path is longer.
    import pathlib
    import tempfile

    short = pathlib.Path(tempfile.mkdtemp(prefix="ldc-", dir="/tmp"))
    tmp_path = short
    fx = tmp_path / "zfs.json"
    fx.write_text(open(FIXTURE, encoding="utf-8").read())
    inst = Instance(
        id="fixture-01",
        module="fixture",
        bindings=(
            make_binding(2, "dataset-a", "/pool/dataset-a", "dataset-a-inc-1", networks=()),
        ),
        fixture_file=str(fx),
    )
    # A binding with a generic datastore id (the fixture module needs one).
    inst = Instance(**{**inst.__dict__, "bindings": (inst.bindings[0].__class__(**{**inst.bindings[0].__dict__, "datastore_id": "fixture-zfs-01"}),)})
    cfg = Config("1.0", True, RuntimeConfig(socket_path=str(tmp_path / "c.sock"), collect_interval_ms=1000, collect_deadline_ms=200), {}, (inst,), "sha256:" + "1" * 64)
    rt = Runtime(cfg, Logger(stream=open("/dev/null", "w")))
    server = Server(rt, cfg.runtime.socket_path)
    server.start()
    try:
        yield rt, server, cfg.runtime.socket_path, fx
    finally:
        server.stop()
        rt.stop(1.0)
        import shutil

        shutil.rmtree(short, ignore_errors=True)


def test_socket_permissions_and_routes(served, schemas):
    rt, server, sock, fx = served
    mode = stat.S_IMODE(os.stat(sock).st_mode)
    assert mode == 0o660
    status, health = get_json(sock, "/healthz")
    assert status == 503 and health["ready"] is False
    status, batch = get_json(sock, "/v1/assessments")
    assert status == 200 and batch["contract_version"] == "1.0"
    validate(schemas["batch"], batch)
    rec = batch["instances"][0]["assessments"][0]
    assert rec["quality"] == "UNKNOWN" and rec["placement"]["reason_codes"] == ["NO_ASSESSMENT"]
    status, _ = get_json(sock, "/nope")
    assert status == 404


def test_fixture_module_gives_generic_results(served, schemas):
    rt, server, sock, fx = served
    ir = rt.instances["fixture-01"]
    ir.run_cycle()
    status, batch = get_json(sock, "/v1/assessments")
    rec = batch["instances"][0]["assessments"][0]
    assert rec["datastore_id"] == "fixture-zfs-01" and rec["target_incarnation"] == "dataset-a-inc-1"
    assert rec["quality"] == "VALID" and rec["placement"]["allowed"] is False  # hold-down
    assert rec["placement"]["reason_codes"] == ["RECOVERY_HOLD_DOWN", "FIXTURE_HEALTHY"]
    assert rec["resources"]["capacity_domain_id"] == "fixture-zfs-01/pool-01"
    assert rec["profile"]["id"] == "fixture-zfs-shaped" and rec["profile"]["digest"].startswith("sha256:")
    assert rec["diagnostics"]["test_only"] is True
    validate(schemas["batch"], batch)
    status, health = get_json(sock, "/healthz")
    assert status == 200 and health["ready"] is True
    # Rewrite the fixture: an explicit deny is applied on the next cycle.
    doc = json.load(open(fx, encoding="utf-8"))
    doc["targets"]["dataset-a"].update({"allowed": False, "multiplier_ppm": 0, "reason_codes": ["FIXTURE_DEGRADED_POOL"]})
    fx.write_text(json.dumps(doc))
    ir.run_cycle()
    status, batch = get_json(sock, "/v1/assessments")
    rec = batch["instances"][0]["assessments"][0]
    assert rec["placement"] == {"allowed": False, "multiplier_ppm": 0, "reason_codes": ["FIXTURE_DEGRADED_POOL"]}
    assert batch["instances"][0]["sequence"] == 2


def test_fixture_failure_injection(served):
    rt, server, sock, fx = served
    ir = rt.instances["fixture-01"]
    ir.run_cycle()
    doc = json.load(open(fx, encoding="utf-8"))
    doc["fail"] = {"code": "SOURCE_AUTH_FAILED", "retryable": False}
    fx.write_text(json.dumps(doc))
    ir.run_cycle()
    status, batch = get_json(sock, "/v1/assessments")
    rec = batch["instances"][0]["assessments"][0]
    assert rec["quality"] == "UNKNOWN" and rec["placement"]["reason_codes"] == ["SOURCE_AUTH_FAILED"]
    assert batch["instances"][0]["snapshot_status"] == "FAILED"


def test_metrics_endpoint(served):
    rt, server, sock, fx = served
    import http.client, socket as pysocket

    class C(http.client.HTTPConnection):
        def connect(self):
            self.sock = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
            self.sock.connect(sock)

    conn = C("localhost")
    conn.request("GET", "/metrics")
    resp = conn.getresponse()
    body = resp.read().decode()
    assert resp.status == 200 and "connector_assessment_allowed" in body


def test_get_p99_is_fast(served):
    rt, server, sock, fx = served
    durations = []
    for _ in range(50):
        t0 = time.perf_counter()
        get_json(sock, "/v1/assessments")
        durations.append(time.perf_counter() - t0)
    durations.sort()
    assert durations[int(len(durations) * 0.99) - 1] < 0.05
