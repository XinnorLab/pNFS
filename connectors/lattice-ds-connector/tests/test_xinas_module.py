# SPDX-License-Identifier: MIT
"""The xinas source client against a local HTTP server (T-27 transport and
auth safety, CON-20) and envelope validation."""

import http.server
import json
import threading

import pytest

import source_builder as sb
from conftest import make_binding, make_profile
from lattice_ds_connector.config import Endpoint, Instance, SourceConfig
from lattice_ds_connector.modules.base import CollectionError
from lattice_ds_connector.modules.xinas import XinasModule, fetch_observations, validate_envelope


class Scripted(http.server.BaseHTTPRequestHandler):
    responses = []
    seen = []

    def do_GET(self):  # noqa: N802
        Scripted.seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
        status, headers, body = Scripted.responses.pop(0)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        return


@pytest.fixture
def server():
    Scripted.responses = []
    Scripted.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), Scripted)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_port}/api/v1/placement/observations"
    finally:
        srv.shutdown()


def fetch(url, deadline=2.0):
    return fetch_observations(url, "tok-viewer", deadline, None, allow_insecure_http=True)


def test_ok_sends_bearer_and_returns_envelope(server):
    srv, url = server
    Scripted.responses.append((200, {"Content-Type": "application/json"}, sb.envelope(sb.base_result())))
    doc = fetch(url)
    assert doc["result"]["controller_id"] == sb.CONTROLLER
    assert Scripted.seen[0]["auth"] == "Bearer tok-viewer"
    assert Scripted.seen[0]["path"] == "/api/v1/placement/observations"


@pytest.mark.parametrize("status, code, retryable", [(401, "SOURCE_AUTH_FAILED", False), (403, "SOURCE_AUTH_FAILED", False), (429, "SOURCE_UNAVAILABLE", True), (500, "SOURCE_UNAVAILABLE", True)])
def test_http_errors_are_typed(server, status, code, retryable):
    srv, url = server
    Scripted.responses.append((status, {"Content-Type": "application/json"}, {"errors": [{"code": "X"}]}))
    with pytest.raises(CollectionError) as ei:
        fetch(url)
    assert ei.value.code == code and ei.value.retryable is retryable


@pytest.mark.parametrize("source_code", ["SOURCE_NOT_READY", "SOURCE_STALE", "SOURCE_FAILED", "SNAPSHOT_TOO_LARGE"])
def test_503_carries_the_source_verdict_and_revokes(server, source_code):
    srv, url = server
    Scripted.responses.append((503, {"Content-Type": "application/json"}, sb.envelope(None, errors=[{"code": source_code, "message": "m"}])))
    with pytest.raises(CollectionError) as ei:
        fetch(url)
    assert ei.value.code == source_code and ei.value.retryable is False


def test_redirect_is_refused_and_never_followed(server):
    srv, url = server
    Scripted.responses.append((302, {"Location": "http://evil.invalid/steal"}, b""))
    with pytest.raises(CollectionError) as ei:
        fetch(url)
    assert ei.value.retryable is False and "redirect" in str(ei.value)
    assert len(Scripted.seen) == 1


def test_non_json_and_oversize(server):
    srv, url = server
    Scripted.responses.append((200, {"Content-Type": "text/html"}, b"<html>proxy error</html>"))
    with pytest.raises(CollectionError) as ei:
        fetch(url)
    assert ei.value.code == "SOURCE_SCHEMA_INVALID"
    Scripted.responses.append((200, {"Content-Type": "application/json"}, b"[" + b"1," * (9 * 1024 * 1024) + b"1]"))
    with pytest.raises(CollectionError) as ei:
        fetch(url)
    assert ei.value.code == "SOURCE_SCHEMA_INVALID" and "16 MiB" in str(ei.value)


def test_timeout_is_retryable(server):
    srv, url = server

    class Slow(Scripted):
        def do_GET(self):  # noqa: N802
            import time

            time.sleep(1.0)
            super().do_GET()

    srv.RequestHandlerClass = Slow
    Scripted.responses.append((200, {}, sb.envelope(sb.base_result())))
    with pytest.raises(CollectionError) as ei:
        fetch(url, deadline=0.2)
    assert ei.value.code == "SOURCE_TIMEOUT" and ei.value.retryable


def test_https_is_the_default_and_plain_http_needs_the_flag():
    with pytest.raises(CollectionError):
        fetch_observations("http://127.0.0.1:1/x", "t", 0.2, None, allow_insecure_http=False)


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda e: e.pop("request_id"), "SOURCE_SCHEMA_INVALID"),
        (lambda e: e["result"].__setitem__("schema_version", "2.0"), "SOURCE_SCHEMA_INVALID"),
        (lambda e: e["result"].__setitem__("shares", {}), "SOURCE_SCHEMA_INVALID"),
        (lambda e: e["result"].__setitem__("snapshot_status", "MAYBE"), "SOURCE_SCHEMA_INVALID"),
        (lambda e: e.__setitem__("result", None), "SOURCE_UNAVAILABLE"),
        (lambda e: e["result"].__setitem__("shares", [{}] * 257), "SNAPSHOT_TOO_LARGE"),
    ],
)
def test_validate_envelope(mutate, code):
    env = sb.envelope(sb.base_result())
    mutate(env)
    with pytest.raises(CollectionError) as ei:
        validate_envelope(env)
    assert ei.value.code == code


@pytest.mark.parametrize(
    "mutate, what",
    [
        (lambda r: r["shares"].append(dict(r["shares"][0])), "duplicate share_id"),
        (lambda r: r["resources"].append(dict(r["resources"][0])), "duplicate resource id"),
        (lambda r: r["shares"][0].__setitem__("evidence_age_ms", None), "SUCCESS without evidence"),
        (lambda r: r["resources"][0].__setitem__("observed_at", None), "SUCCESS without evidence"),
        (lambda r: r["resources"][0].__setitem__("evidence_age_ms", -5), "evidence_age_ms invalid"),
        (lambda r: r["resources"][0]["details"].__setitem__("kind", "VOLUME"), "kind invalid"),
        (lambda r: r["resources"][0].__setitem__("collection_status", "ERROR"), "ERROR without a reason"),
        (lambda r: r["shares"][0].pop("share_id"), "share_id missing"),
    ],
)
def test_c02_structural_checks_run_without_jsonschema(monkeypatch, mutate, what):
    """The runtime never depends on the optional jsonschema package for the
    checks the policy relies on (audit C-02)."""
    from lattice_ds_connector.modules import xinas as xmod

    monkeypatch.setattr(xmod, "_schema_state", "unavailable")
    monkeypatch.setattr(xmod, "_schema_validator", None)
    env = sb.envelope(sb.base_result())
    mutate(env["result"])
    with pytest.raises(CollectionError) as ei:
        validate_envelope(env)
    assert ei.value.code == "SOURCE_SCHEMA_INVALID" and ei.value.retryable is False
    assert what in str(ei.value)


def test_c02_full_schema_validation_when_jsonschema_is_installed(monkeypatch):
    pytest.importorskip("jsonschema")
    from lattice_ds_connector.modules import xinas as xmod

    monkeypatch.setattr(xmod, "_schema_state", "unloaded")
    monkeypatch.setattr(xmod, "_schema_validator", None)
    assert xmod.schema_validation_available()
    env = sb.envelope(sb.base_result())
    env["result"]["resources"][0]["details"]["members"][0]["index"] = "zero"  # a type the schema pins
    with pytest.raises(CollectionError) as ei:
        validate_envelope(env)
    assert ei.value.code == "SOURCE_SCHEMA_INVALID" and "schema:" in str(ei.value)
    validate_envelope(sb.envelope(sb.base_result()))  # the healthy envelope still passes


def test_discover_prints_share_incarnations_for_an_unpinned_config(server, token_file, tmp_path, capsys):
    """Audit C-05: `discover` is how an operator obtains the incarnation to pin."""
    from lattice_ds_connector.cli import main

    srv, url = server
    Scripted.responses.append((200, {"Content-Type": "application/json"}, sb.envelope(sb.base_result())))
    cfg = {
        "config_version": "1.0",
        "test_mode": True,
        "runtime": {"socket_path": str(tmp_path / "c.sock"), "collect_interval_ms": 5000, "collect_deadline_ms": 2000},
        "profiles": [{"id": "xinas-mvp", "version": "1"}],
        "instances": [
            {
                "id": "xi-01",
                "module": "xinas",
                "expected_controller_id": sb.CONTROLLER,
                "source": {"url": url, "allow_insecure_http": True, "bearer_token_file": token_file},
                "profile": "xinas-mvp",
                "bindings": [
                    {
                        "ds_id": 0,
                        "binding_generation": 1,
                        "target_id": "training-a",
                        "endpoint": {"server": "10.10.10.21", "export_path": "/mnt/data/training-a"},
                        "expected_client_networks": ["10.10.10.0/24"],
                        "expected_security": ["sys"],
                    }
                ],
            }
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    assert main(["validate-config", "--config", str(path)]) == 1
    out = capsys.readouterr().out
    assert "INCARNATION_REQUIRED" in out
    assert main(["discover", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "instance xi-01: controller xinas-node-01" in out
    assert "share training-a" in out and "incarnation training-a:7" in out and "bound ds 0, pinned -" in out
    assert "share training-c" in out and "incarnation training-c:7" in out


def test_module_collect_and_evaluate(token_file):
    calls = []

    def fake_fetch(url, token, deadline, ca, insecure):
        calls.append((url, token, ca, insecure))
        return sb.envelope(sb.base_result())

    inst = Instance(
        id="xi-01",
        module="xinas",
        bindings=(make_binding(0, "training-a", "/mnt/data/training-a", "training-a:7"),),
        profile_id="xinas-mvp",
        expected_controller_id=sb.CONTROLLER,
        source=SourceConfig(url="https://xinas/api/v1/placement/observations", tls_ca_file="/ca.pem", bearer_token_file=token_file),
    )
    mod = XinasModule(inst, fetch=fake_fetch)
    batch = mod.collect(2.0)
    assert calls == [("https://xinas/api/v1/placement/observations", "tok-viewer", "/ca.pem", False)]
    assert batch.source_generation == 120 and batch.snapshot_status == "COMPLETE"
    out = mod.evaluate(batch, make_profile(), inst.bindings)
    assert out[0].allowed and out[0].datastore_id == sb.CONTROLLER
    desc = mod.describe()
    assert desc["supported_source_versions"] == ["1.0"] and "raid.array_states" in desc["capabilities"]


def test_missing_token_file_is_an_auth_failure(tmp_path):
    inst = Instance(id="x", module="xinas", bindings=(), profile_id="p", expected_controller_id="c", source=SourceConfig(url="https://h/x", bearer_token_file=str(tmp_path / "none")))
    with pytest.raises(CollectionError) as ei:
        XinasModule(inst).collect(1.0)
    assert ei.value.code == "SOURCE_AUTH_FAILED" and ei.value.retryable is False
