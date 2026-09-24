# SPDX-License-Identifier: MIT
import pytest

from lattice_placement import manifest as manifest_mod
from lattice_placement.ini import IniDocument
from lattice_placement.validate import fold_preflight, validate_document

M = manifest_mod.load()


def rep(text, mode, assume_set=False):
    return validate_document(IniDocument.parse(text), mode, M, assume_set=assume_set)


def codes(r):
    return [e.split(":", 1)[0] for e in r.errors]


# --- the fork's test_placement_config.c cases, same INI texts ------------

@pytest.mark.parametrize("text,code", [
    ("placement_mode = smart\nds_connector_enabled = false\n", "PLACEMENT_MODE_CONFLICT"),
    ("placement_mode = rr\nds_connector_enabled = true\n", "PLACEMENT_MODE_CONFLICT"),
    ("placement_mode = fill\nds_connector_enabled = true\n", "PLACEMENT_MODE_CONFLICT"),
    ("placement_mode = smart\ndefault_mirror_count = 2\n", "MIRROR_COUNT_UNSUPPORTED"),
    ("placement_mode = smart\nds_capacity_poll_ms = 0\n", "RANGE"),
    ("placement_mode = smart\nds_connector_poll_ms = 100\n", "RANGE"),
    ("placement_mode = smart\nds_connector_poll_ms = 20000\n", "RANGE"),
    ("placement_mode = smart\nds_connector_request_deadline_ms = 2000\n", "RANGE"),
    ("placement_mode = smart\nds_connector_request_deadline_ms = 10\n", "RANGE"),
    ("placement_mode = smart\nds_connector_max_ds = 0\n", "RANGE"),
    ("placement_mode = smart\nds_connector_max_ds = 257\n", "RANGE"),
    ("placement_mode = smart\nds_connector_expected_contract_major = 0\n", "RANGE"),
    ("placement_mode = smart\nds_connector_socket = relative.sock\n", "RANGE"),
    ("placement_mode = smart\nds_connector_socket = /run/lattice-ds-connector/" + "0" * 100 + ".sock\n", "RANGE"),
    ("placement_mode = smart\nds_connector_poll_ms = 1s\n", "RANGE"),
    ("placement_mode = smart\nds_connector_access_scope = \n", "RANGE"),
    ("placement_mode = fill\nds_capacity_poll_ms = 60000\nplacement_capacity_max_age_ms = 60000\n", "RANGE"),
    ("placement_mode = fill\nplacement_capacity_max_age_ms = 86400001\n", "RANGE"),
    ("placement_mode = fill\nplacement_capacity_max_age_ms = 0\n", "RANGE"),
    ("placement_mode = fill\nplacement_stripe_shrink = maybe\n", "RANGE"),
    ("placement_mode = fill\nds_capacity_domain.3 = \n", "RANGE"),
    ("placement_mode = fill\nds_capacity_domain.999 = d\n", "RANGE"),
    ("placement_mode = fill\nds_capacity_domain.x = d\n", "RANGE"),
    ("placement_mode = fill\nds_weight.0 = 5\n", "PLACEMENT_MODE_CONFLICT"),
    ("placement_mode = rr\nplacement_policy = wrr\n", "PLACEMENT_MODE_CONFLICT"),
    ("placement_mode = rr\nplacement_policy_enabled = true\n", "PLACEMENT_MODE_CONFLICT"),
    ("placement_mode = rr\nworkload_profile = hpc\n", "PLACEMENT_MODE_CONFLICT"),
    ("placement_mode = rr\nplacement_domain_weight.d = 5\n", "DOMAIN_WEIGHT_FORBIDDEN"),
    ("placement_mode = smart\nplacement_domain_weight.d = 5\n", "DOMAIN_WEIGHT_FORBIDDEN"),
    ("placement_mode = smart\nplacement_allow_manual_base_weights = true\nplacement_domain_weight.d = 10001\n", "RANGE"),
    ("placement_mode = smart\nplacement_allow_manual_base_weights = true\nplacement_domain_weight.d = 0\n", "RANGE"),
    ("placement_mode = fill\nplacement_min_free_bytes = 1G\n", "RANGE"),
    ("placement_mode = sideways\n", "RANGE"),
])
def test_fork_bad_cases_are_not_ready(text, code):
    mode = IniDocument.parse(text).get("placement_mode") or "rr"
    if mode == "sideways":
        r = rep(text, "rr")
    else:
        r = rep(text, mode)
    assert not r.ready, text
    assert code in codes(r), (text, r.errors)


def test_good_cases_and_effective_defaults():
    r = rep("placement_mode = smart\n", "smart")
    assert r.ready and r.errors == []
    assert r.effective["ds_connector_socket"] == "/run/lattice-ds-connector/connector.sock"
    assert r.effective["ds_connector_poll_ms"] == "1000"
    assert r.effective["ds_connector_request_deadline_ms"] == "500"
    assert r.effective["ds_connector_expected_contract_major"] == "1"
    assert r.effective["ds_connector_max_ds"] == "256"
    assert r.effective["ds_connector_access_scope"] == "cluster-default"
    assert r.effective["placement_capacity_max_age_ms"] == "120000"
    assert r.effective["ds_capacity_poll_ms"] == "60000"
    assert any("not pinned" in w for w in r.warnings)
    r = rep("placement_mode = fill\nds_capacity_poll_ms = 30000\nplacement_capacity_max_age_ms = 90000\n"
            "placement_min_free_bytes = 1073741824\nplacement_stripe_shrink = strict\n"
            "ds_capacity_domain.0 = xi/fs-1\nds_capacity_domain.1 = xi/fs-1\n", "fill")
    assert r.ready
    assert r.effective["ds_capacity_domain.0"] == "xi/fs-1"
    assert r.effective["placement_stripe_shrink"] == "strict"
    r = rep("placement_mode = smart\nds_connector_socket = /run/lattice-ds-connector/" + "0" * 76 + ".sock\n", "smart")
    assert r.ready
    r = rep("placement_mode = smart\nplacement_allow_manual_base_weights = true\nplacement_domain_weight.d = 300\n", "smart")
    assert r.ready and r.effective["placement_domain_weight.d"] == "300"
    r = rep("placement_mode = rr\nworkload_profile = default\n", "rr")
    assert r.ready


def test_mode_argument_versus_file():
    # strict: the file must already say the mode
    assert not rep("placement_policy = wrr\n", "fill").ready
    assert "PLACEMENT_MODE_CONFLICT" in codes(rep("placement_policy = wrr\n", "fill"))
    assert not rep("placement_mode = rr\n", "fill").ready
    # as `set` would leave it: legacy keys are a warning, the mode is assumed
    r = rep("placement_policy_enabled = true\nplacement_policy = wrr\nds_weight.0 = 55\nds_weight.1 = 45\n",
            "fill", assume_set=True)
    assert r.ready, r.errors
    assert any("legacy keys present" in w for w in r.warnings)
    assert r.effective["placement_mode"] == "fill"
    r = rep("placement_mode = smart\n", "legacy", assume_set=True)
    assert r.ready and r.effective["placement_mode"] == "legacy"
    assert not rep("placement_mode = smart\n", "legacy").ready


def test_legacy_rules():
    r = rep("placement_policy_enabled = true\nplacement_policy = wrr\nds_weight.0 = 55\nds_weight.1 = 45\n", "legacy")
    assert r.ready and r.effective["placement_policy"] == "wrr"
    assert not rep("placement_policy = sideways\n", "legacy").ready
    assert not rep("ds_weight.0 = 0\n", "legacy").ready
    assert not rep("ds_weight.x = 1\n", "legacy").ready
    assert "PLACEMENT_MODE_CONFLICT" in codes(rep("ds_connector_enabled = true\n", "legacy"))
    assert rep("ds_connector_enabled = false\n", "legacy").ready
    r = rep("placement_capacity_max_age_ms = 5\n", "legacy")
    assert r.ready and any("ignored" in w for w in r.warnings)


def test_unknown_mode_and_bad_booleans():
    r = rep("", "sideways")
    assert not r.ready and "RANGE" in codes(r)
    assert not rep("placement_mode = smart\nds_connector_enabled = maybe\n", "smart").ready
    assert not rep("placement_mode = smart\nplacement_allow_manual_base_weights = 2\n", "smart").ready


def test_duplicate_keys_warn():
    r = rep("placement_mode = fill\nds_capacity_poll_ms = 10000\nds_capacity_poll_ms = 10000\n", "fill")
    assert r.ready and any("duplicate key ds_capacity_poll_ms" in w for w in r.warnings)


def test_fold_preflight():
    base = rep("placement_mode = smart\n", "smart")
    r = fold_preflight(rep("placement_mode = smart\n", "smart"), None)
    assert not r.ready and "CONNECTOR:UNAVAILABLE" in r.errors[0]
    r = fold_preflight(rep("placement_mode = smart\n", "smart"), {"ready": False, "reasons": ["CONNECTOR_NOT_READY", "UNBOUND_DS:1"]})
    assert not r.ready and r.errors == ["CONNECTOR:CONNECTOR_NOT_READY", "CONNECTOR:UNBOUND_DS:1"]
    r = fold_preflight(rep("placement_mode = smart\n", "smart"), {"ready": True, "reasons": []})
    assert r.ready and r.connector == {"ready": True, "reasons": []}
    # not smart: untouched
    r = fold_preflight(rep("placement_mode = fill\n", "fill"), None)
    assert r.ready and r.connector is None
    assert base.ready
