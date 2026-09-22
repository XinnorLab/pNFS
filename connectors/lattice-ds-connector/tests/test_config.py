# SPDX-License-Identifier: MIT
"""CON-19, LAT-22, XMOD-15, T-29, T-30: configuration validation."""

import copy
import json
import os

import pytest

from lattice_ds_connector.config import ConfigError, load_config, validate_config_dict

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), "examples", "connector-config.json")


def example(token_file: str, tmp_path) -> dict:
    with open(EXAMPLE, encoding="utf-8") as fh:
        doc = json.load(fh)
    src = doc["instances"][0]["source"]
    src["bearer_token_file"] = token_file
    src.pop("tls_ca_file")  # system trust store → warning only
    fx = tmp_path / "zfs.json"
    fx.write_text("{}")
    doc["instances"][1]["fixture_file"] = str(fx)
    return doc


def errors_of(doc) -> set:
    config, issues = validate_config_dict(doc)
    return {(i.code, i.path) for i in issues if i.severity == "error"}


def test_example_validates_with_digests(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    config, issues = validate_config_dict(doc)
    assert config is not None, issues
    assert config.digest.startswith("sha256:")
    assert config.profiles["xinas-mvp"].digest.startswith("sha256:")
    assert {i.code for i in config.warnings} == {"TLS_SYSTEM_CA"}
    assert [b.ds_id for i in config.instances for b in i.bindings] == [0, 1, 2]
    # The digest is over the canonical document without example_only and is stable.
    again, _ = validate_config_dict(copy.deepcopy(doc))
    assert again.digest == config.digest


def test_profile_digest_changes_only_with_placement_fields(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    a, _ = validate_config_dict(doc)
    doc2 = copy.deepcopy(doc)
    doc2["profiles"][0]["degraded_multiplier_ppm"] = 100000
    b, _ = validate_config_dict(doc2)
    assert a.profiles["xinas-mvp"].digest != b.profiles["xinas-mvp"].digest
    assert a.digest != b.digest


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda d: d["instances"][1]["bindings"][0].__setitem__("ds_id", 0), ("DUPLICATE_DS", "instances[1].bindings")),
        (lambda d: d.__setitem__("test_mode", False), ("FIXTURE_IN_PRODUCTION", "instances[1].module")),
        (lambda d: d["instances"][0]["source"].__setitem__("url", "http://xinas/api/v1/placement/observations"), ("INSECURE_HTTP", "instances[0].source.url")),
        (lambda d: d["runtime"].__setitem__("collect_deadline_ms", 5000), ("TIMEOUT_RELATION", "runtime.collect_deadline_ms")),
        (lambda d: d["profiles"][0]["required_checks"].append("network.path"), ("CHECK_NOT_IMPLEMENTED", "profiles[0].required_checks")),
        (lambda d: d["profiles"][0]["required_checks"].append("magic.check"), ("CHECK_UNKNOWN", "profiles[0].required_checks")),
        (lambda d: d["profiles"][0].__setitem__("version", "2"), ("UNSUPPORTED_PROFILE", "profiles[0].version")),
        (lambda d: d["profiles"][0].__setitem__("source_max_age_ms", 25000), ("RANGE", "profiles[0].source_max_age_ms")),
        (lambda d: d["profiles"][0].__setitem__("source_max_age_ms", 8000), ("TIMEOUT_RELATION", "profiles[0].source_max_age_ms")),
        (lambda d: d["instances"][0]["bindings"][0].__setitem__("expected_client_networks", []), ("CLIENT_NETWORKS_REQUIRED", "instances[0].bindings[0].expected_client_networks")),
        (lambda d: d["instances"][0]["bindings"][0]["expected_client_networks"].append("not-a-net"), ("FORMAT", "instances[0].bindings[0].expected_client_networks[2]")),
        (lambda d: d["instances"][0]["bindings"][0]["endpoint"].__setitem__("export_path", "relative"), ("FORMAT", "instances[0].bindings[0].endpoint.export_path")),
        (lambda d: d["instances"][0]["bindings"][0]["endpoint"].__setitem__("transport", "UDP"), ("ENUM", "instances[0].bindings[0].endpoint.transport")),
        (lambda d: d["instances"][0].__setitem__("profile", "nope"), ("UNKNOWN_PROFILE", "instances[0].profile")),
        (lambda d: d["instances"][0].pop("expected_controller_id"), ("MISSING_FIELD", "instances[0].expected_controller_id")),
        (lambda d: d["instances"][0]["source"].__setitem__("url", "https://user:pw@xinas/api"), ("CREDENTIALS_IN_URL", "instances[0].source.url")),
        (lambda d: d["instances"][1]["bindings"][0].pop("datastore_id"), ("MISSING_FIELD", "instances[1].bindings[0].datastore_id")),
        (lambda d: d.__setitem__("config_version", "2.0"), ("UNSUPPORTED_CONFIG_VERSION", "config_version")),
        (lambda d: d["instances"].append({**d["instances"][1], "id": "fixture-01"}), ("DUPLICATE_INSTANCE", "instances[2].id")),
    ],
)
def test_typed_errors(token_file, tmp_path, mutate, expected):
    doc = example(token_file, tmp_path)
    mutate(doc)
    assert expected in errors_of(doc)


def test_duplicate_target_needs_explicit_alias(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    b = copy.deepcopy(doc["instances"][0]["bindings"][0])
    b["ds_id"] = 9
    doc["instances"][0]["bindings"].append(b)
    assert ("DUPLICATE_TARGET", "instances[0].bindings") in errors_of(doc)
    for binding in doc["instances"][0]["bindings"]:
        if binding["target_id"] == "training-a":
            binding["alias"] = True
    assert not errors_of(doc)


def test_insecure_http_allowed_explicitly_with_warning(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    doc["instances"][0]["source"]["url"] = "http://xinas.lab/api/v1/placement/observations"
    doc["instances"][0]["source"]["allow_insecure_http"] = True
    config, issues = validate_config_dict(doc)
    assert config is not None
    assert ("INSECURE_HTTP", "warning") in {(i.code, i.severity) for i in issues}


def test_secret_file_permissions(tmp_path):
    token = tmp_path / "t.token"
    token.write_text("x")
    token.chmod(0o644)
    doc = example(str(token), tmp_path)
    assert ("SECRET_PERMISSIONS", "instances[0].source.bearer_token_file") in errors_of(doc)
    doc["instances"][0]["source"]["bearer_token_file"] = str(tmp_path / "missing")
    assert ("SECRET_FILE", "instances[0].source.bearer_token_file") in errors_of(doc)


def test_limits(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    doc["runtime"]["max_ds"] = 2
    assert ("LIMIT", "instances") in errors_of(doc)
    doc = example(token_file, tmp_path)
    doc["runtime"]["max_instances"] = 1
    assert ("LIMIT", "instances") in errors_of(doc)


def test_load_config_reports_every_error_at_once(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"config_version": "1.0", "runtime": {"collect_deadline_ms": 9000}, "profiles": [], "instances": [{"id": "x", "module": "zfs", "bindings": []}]}))
    with pytest.raises(ConfigError) as ei:
        load_config(str(p))
    codes = {i.code for i in ei.value.issues}
    assert {"TIMEOUT_RELATION", "UNKNOWN_MODULE", "TYPE"} <= codes


def test_load_config_bad_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{")
    with pytest.raises(ConfigError) as ei:
        load_config(str(p))
    assert ei.value.issues[0].code == "JSON"
