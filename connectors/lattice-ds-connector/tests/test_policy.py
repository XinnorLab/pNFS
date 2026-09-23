# SPDX-License-Identifier: MIT
"""The xinas-mvp v1 decision policy: T-04..T-10 as pure-function tests."""

import copy
import itertools

import pytest

import source_builder as sb
from conftest import make_binding, make_profile
from lattice_ds_connector import contract
from lattice_ds_connector.modules.xinas_policy import evaluate_result

CTRL = sb.CONTROLLER


def assess(result, bindings, profile=None, duration=0):
    out = evaluate_result(result, profile or make_profile(), bindings, CTRL, duration)
    return {a.target_id: a for a in out}


def one(result, binding=None, **kw):
    b = binding or make_binding(0, "training-a", "/mnt/data/training-a", "training-a:7")
    return assess(result, (b,), **kw)["training-a"]


# ---------------------------------------------------------------------------
# T-04: topology
# ---------------------------------------------------------------------------


def test_t04_healthy_topology_allows_all_three(base_result, bindings):
    out = assess(base_result, bindings)
    for a in out.values():
        assert a.quality == "VALID" and a.allowed and a.multiplier_ppm == contract.PPM_FULL, a
        assert a.reason_codes == ["NORMAL"]
        assert a.target_incarnation.endswith(":7")
        assert a.evidence_age_ms == 1100  # the oldest of share/fs/export/service/arrays
    assert out["training-a"].capacity_domain_id == f"{CTRL}/fs-uuid-01/fs-uuid-01:/dev/xi_data1"
    assert out["training-a"].capacity_domain_id == out["training-b"].capacity_domain_id
    assert out["training-c"].capacity_domain_id != out["training-a"].capacity_domain_id
    assert out["training-a"].shared_resource_ids == [f"{CTRL}/array:data1", f"{CTRL}/array:log1"]
    checks = {c["check"]: c for c in out["training-a"].coverage}
    assert all(checks[c]["status"] == "EVALUATED" for c in make_profile().required_checks)
    assert checks["topology.log"]["status"] == "EVALUATED"
    assert checks["topology.realtime"] == {"check": "topology.realtime", "required": False, "status": "NOT_APPLICABLE", "reason": "NO_REALTIME_DEVICE"}
    assert checks["network.path"] == {"check": "network.path", "required": False, "status": "NOT_IMPLEMENTED", "reason": "OUT_OF_MVP"}


def test_t04_log1_fault_blocks_a_and_b_only(base_result, bindings):
    r = sb.with_array_states(base_result, "log1", ["online", "initialized", "reconstructing"])
    out = assess(r, bindings)
    assert not out["training-a"].allowed and "RECONSTRUCTION_ACTIVE" in out["training-a"].reason_codes
    assert not out["training-b"].allowed and out["training-b"].quality == "VALID"
    assert out["training-c"].allowed


def test_t04_independent_array_does_not_affect_share(base_result, bindings):
    r = sb.with_array_states(base_result, "data2", ["offline"])
    out = assess(r, bindings)
    assert out["training-a"].allowed and out["training-b"].allowed
    assert not out["training-c"].allowed


def test_internal_log_is_not_applicable_with_reason(base_result):
    fs = sb.find(base_result, "fs:mnt-data.mount")["details"]
    fs["log_mode"] = "INTERNAL"
    fs["array_refs"] = [{"role": "DATA", "resource_id": "array:data1"}]
    fs["super_options"] = ["rw"]
    a = one(base_result)
    assert a.allowed
    checks = {c["check"]: c for c in a.coverage}
    assert checks["topology.log"] == {"check": "topology.log", "required": False, "status": "NOT_APPLICABLE", "reason": "INTERNAL_LOG"}
    assert a.shared_resource_ids == [f"{CTRL}/array:data1"]


def test_realtime_device_is_evaluated(base_result):
    r = copy.deepcopy(base_result)
    r["resources"].append(sb.array("rt1", "10", [sb.member(0, "/dev/nvme8n1"), sb.member(1, "/dev/nvme9n1")]))
    fs = sb.find(r, "fs:mnt-data.mount")["details"]
    fs["array_refs"].append({"role": "REALTIME", "resource_id": "array:rt1"})
    fs["super_options"].append("rtdev=/dev/xi_rt1")  # the ref must be backed by the rtdev option (C-01)
    a = one(r)
    assert a.allowed
    assert {c["check"]: c["status"] for c in a.coverage}["topology.realtime"] == "EVALUATED"
    r2 = sb.with_array_states(r, "rt1", ["offline"])
    assert not one(r2).allowed


# ---------------------------------------------------------------------------
# T-06: the decision table, every word, permutations, shapes
# ---------------------------------------------------------------------------

TABLE = [
    (["online", "initialized"], True, contract.PPM_FULL, ["NORMAL"]),
    (["online", "initialized", "need_resize"], True, contract.PPM_FULL, ["NORMAL"]),
    (["online", "initialized", "sdc_scanning"], True, contract.PPM_FULL, ["SCAN_ACTIVE"]),
    (["online", "initialized", "degraded"], True, 250000, ["REDUNDANCY_DEGRADED"]),
    (["online", "initialized", "need_restripe"], True, 250000, ["RESTRIPE_PENDING"]),
    (["online", "initialized", "degraded", "need_restripe"], True, 250000, ["REDUNDANCY_DEGRADED", "RESTRIPE_PENDING"]),
    (["online", "initialized", "reconstructing"], False, 0, ["RECONSTRUCTION_ACTIVE"]),
    (["online", "initing"], False, 0, ["INITIALIZATION_ACTIVE"]),
    (["online", "initialized", "restriping"], False, 0, ["RESTRIPE_ACTIVE"]),
    (["online", "initialized", "need_recon"], False, 0, ["RECONSTRUCTION_REQUIRED"]),
    (["online", "need_init"], False, 0, ["INITIALIZATION_REQUIRED"]),
    (["online", "initialized", "inconsistent"], False, 0, ["INTEGRITY_ERROR"]),
    (["online", "initialized", "unrecovered"], False, 0, ["UNRECOVERED"]),
    (["online", "initialized", "read_only"], False, 0, ["READ_ONLY"]),
    (["offline", "initialized"], False, 0, ["ARRAY_UNAVAILABLE"]),
    (["none"], False, 0, ["ARRAY_UNAVAILABLE"]),
    (["initialized"], False, 0, ["ARRAY_UNAVAILABLE"]),  # initialized without online is not enough
    (["online", "initialized", "degraded", "reconstructing"], False, 0, ["RECONSTRUCTION_ACTIVE", "REDUNDANCY_DEGRADED"]),
]


@pytest.mark.parametrize("states, allowed, ppm, reasons", TABLE)
def test_t06_decision_table(base_result, states, allowed, ppm, reasons):
    a = one(sb.with_array_states(base_result, "data1", states))
    assert a.quality == "VALID"
    assert a.allowed is allowed
    assert a.multiplier_ppm == ppm
    for code in reasons:
        assert code in a.reason_codes, a.reason_codes


@pytest.mark.parametrize("states", [list(p) for p in itertools.permutations(["degraded", "online", "initialized", "sdc_scanning"])])
def test_t06_word_order_is_irrelevant(base_result, states):
    a = one(sb.with_array_states(base_result, "data1", states))
    assert a.allowed and a.multiplier_ppm == 250000
    assert set(a.reason_codes) == {"REDUNDANCY_DEGRADED", "SCAN_ACTIVE"}


@pytest.mark.parametrize(
    "states, valid",
    [
        ([], True),
        (None, True),
        ("online", True),
        (["online", 42], True),
        (["online", "initialized", "wibble"], True),
        (["online", "initialized"], False),
        ({"state": "online"}, True),
    ],
)
def test_t06_unknown_shapes_are_unknown_not_dropped(base_result, states, valid):
    a = one(sb.with_array_states(base_result, "data1", states, valid=valid))
    assert a.quality == "UNKNOWN" and not a.allowed and a.multiplier_ppm == 0
    assert "SOURCE_UNKNOWN" in a.reason_codes


@pytest.mark.parametrize("level", ["0", "raid0", "RAID0"])
def test_t06_online_without_initialization_proof_is_unknown_except_raid0(base_result, level):
    a = one(sb.with_array_states(base_result, "data1", ["online"]))
    assert a.quality == "UNKNOWN" and "SOURCE_UNKNOWN" in a.reason_codes
    r = sb.with_array_states(base_result, "data1", ["online"])
    sb.find(r, "array:data1")["details"]["raid_level"] = level
    a0 = one(r)
    assert a0.allowed and a0.reason_codes == ["NORMAL"]


def test_level_spelling_of_the_xinas_source_is_accepted(base_result):
    # xiNAS publishes raid5/raid10 (seen on xinas-box 2026-09-22); the level
    # must not be mistaken for RAID 0 nor for an unknown level.
    r = copy.deepcopy(base_result)
    sb.find(r, "array:data1")["details"]["raid_level"] = "raid5"
    sb.find(r, "array:log1")["details"]["raid_level"] = "raid10"
    assert one(r).allowed
    r2 = sb.with_array_states(r, "data1", ["online"])
    assert one(r2).quality == "UNKNOWN"


def test_t06_unknown_evidence_and_proven_veto_keep_both_reasons(base_result):
    r = sb.with_array_states(base_result, "data1", ["online", "initialized", "wibble", "read_only"])
    a = one(r)
    assert a.quality == "UNKNOWN"
    assert {"SOURCE_UNKNOWN", "READ_ONLY"} <= set(a.reason_codes)


# ---------------------------------------------------------------------------
# XMOD-09 members
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "states, valid, allowed, quality, code, ppm",
    [
        (["online"], True, True, "VALID", "NORMAL", contract.PPM_FULL),
        (["offline"], True, True, "VALID", "MEMBER_OFFLINE", 250000),
        (["reconstructing"], True, False, "VALID", "MEMBER_RECONSTRUCTION_ACTIVE", 0),
        (["need_recon"], True, False, "VALID", "MEMBER_RECONSTRUCTION_REQUIRED", 0),
        ([], True, False, "UNKNOWN", "MEMBER_STATE_MISSING", 0),
        (["online"], False, False, "UNKNOWN", "MEMBER_STATE_INVALID", 0),
        (["online", 3], True, False, "UNKNOWN", "MEMBER_STATE_INVALID", 0),
        (["sparkly"], True, False, "UNKNOWN", "MEMBER_STATE_INVALID", 0),
    ],
)
def test_member_states(base_result, states, valid, allowed, quality, code, ppm):
    a = one(sb.with_member_states(base_result, "data1", 1, states, valid=valid))
    assert (a.allowed, a.quality, a.multiplier_ppm) == (allowed, quality, ppm)
    assert code in a.reason_codes


def test_member_penalty_is_not_summed_per_member(base_result):
    r = sb.with_member_states(base_result, "data1", 0, ["offline"])
    r = sb.with_member_states(r, "data1", 1, ["offline"])
    a = one(r)
    assert a.allowed and a.multiplier_ppm == 250000


def test_missing_members_list_is_unknown(base_result):
    r = copy.deepcopy(base_result)
    del sb.find(r, "array:data1")["details"]["members"]
    a = one(r)
    assert a.quality == "UNKNOWN" and "MEMBER_STATE_MISSING" in a.reason_codes


# ---------------------------------------------------------------------------
# T-07 / XMOD-10: the minimum, not the product; deny beats allow
# ---------------------------------------------------------------------------


def test_t07_data_and_log_degraded_is_one_penalty(base_result, bindings):
    r = sb.with_array_states(base_result, "data1", ["online", "initialized", "degraded"])
    r = sb.with_array_states(r, "log1", ["online", "initialized", "degraded"])
    out = assess(r, bindings)
    assert out["training-a"].multiplier_ppm == 250000
    assert out["training-b"].multiplier_ppm == 250000
    assert out["training-c"].multiplier_ppm == contract.PPM_FULL


def test_t07_hard_deny_beats_allow(base_result):
    r = sb.with_array_states(base_result, "data1", ["online", "initialized", "degraded"])
    r = sb.with_array_states(r, "log1", ["online", "initialized", "read_only"])
    a = one(r)
    assert not a.allowed and a.multiplier_ppm == 0
    assert {"READ_ONLY", "REDUNDANCY_DEGRADED"} <= set(a.reason_codes)


def test_profile_ppm_parameters_apply(base_result):
    r = sb.with_array_states(base_result, "data1", ["online", "initialized", "need_restripe"])
    a = one(r, profile=make_profile(need_restripe_multiplier_ppm=100000))
    assert a.multiplier_ppm == 100000


# ---------------------------------------------------------------------------
# T-05 / T-08 / XMOD-11: filesystem, export, service prerequisites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch, quality, code",
    [
        ({"mounted": False, "writable": None}, "VALID", "FILESYSTEM_NOT_MOUNTED"),
        ({"writable": False}, "VALID", "FILESYSTEM_READ_ONLY"),
        ({"mounted": None, "writable": None}, "UNKNOWN", "FILESYSTEM_MOUNT_UNKNOWN"),
        ({"writable": None}, "UNKNOWN", "FILESYSTEM_MOUNT_UNKNOWN"),
        ({"mount_source_mismatch": "/dev/sdb1"}, "UNKNOWN", "FILESYSTEM_SOURCE_MISMATCH"),
        ({"fs_type": "ext4"}, "UNKNOWN", "FILESYSTEM_TYPE_UNSUPPORTED"),
        ({"array_refs": []}, "UNKNOWN", "DATA_ARRAY_UNRESOLVED"),
        ({"external_dependencies_resolved": False}, "UNKNOWN", "EXTERNAL_DEVICE_UNRESOLVED"),
        ({"log_mode": "UNKNOWN"}, "UNKNOWN", "LOG_MODE_UNKNOWN"),
        ({"log_mode": "EXTERNAL", "array_refs": [{"role": "DATA", "resource_id": "array:data1"}]}, "UNKNOWN", "EXTERNAL_DEVICE_UNRESOLVED"),
    ],
)
def test_filesystem_prerequisites(base_result, patch, quality, code):
    r = copy.deepcopy(base_result)
    sb.find(r, "fs:mnt-data.mount")["details"].update(patch)
    a = one(r)
    assert a.quality == quality and not a.allowed
    assert code in a.reason_codes


def test_t05_filesystem_unknown_never_falls_back_to_a_parent(base_result):
    """A share whose filesystem is UNKNOWN stays UNKNOWN even though another
    healthy managed filesystem exists on the node."""
    r = copy.deepcopy(base_result)
    fs = sb.find(r, "fs:mnt-data.mount")
    fs["collection_status"] = "UNKNOWN"
    fs["reason_codes"] = ["MOUNT_SOURCE_MISMATCH"]
    a = one(r)
    assert a.quality == "UNKNOWN" and "DEPENDENCY_ERROR" in a.reason_codes
    assert a.diagnostics["filesystem_reason_codes"] == ["MOUNT_SOURCE_MISMATCH"]


@pytest.mark.parametrize(
    "patch, quality, code",
    [
        ({"running": False}, "VALID", "NFS_SERVICE_STOPPED"),
        ({"running": None}, "UNKNOWN", "NFS_SERVICE_UNKNOWN"),
        ({"protocols": ["NFSv4.1"]}, "VALID", "NFS_PROTOCOL_MISSING"),
    ],
)
def test_service_prerequisites(base_result, patch, quality, code):
    r = copy.deepcopy(base_result)
    sb.find(r, "nfs:nfs-server")["details"].update(patch)
    a = one(r)
    assert a.quality == quality and not a.allowed and code in a.reason_codes


def test_t08_missing_export_is_a_valid_deny(base_result):
    r = copy.deepcopy(base_result)
    ex = sb.find(r, "export:mnt-data-training-a")["details"]
    ex["present"] = False
    ex["rules"] = []
    a = one(r)
    assert a.quality == "VALID" and not a.allowed and "EXPORT_ABSENT" in a.reason_codes


def test_export_source_etab_is_the_default_and_an_exports_file_source_is_unknown(base_result):
    r = copy.deepcopy(base_result)
    sb.find(r, "export:mnt-data-training-a")["details"]["source"] = "/etc/exports"
    a = one(r)
    assert a.quality == "UNKNOWN" and "EXPORT_SOURCE_NOT_EFFECTIVE" in a.reason_codes
    assert a.diagnostics["export_source"] == "/etc/exports"
    # A profile that accepts the labelled weaker source still allows it.
    assert one(r, profile=make_profile(export_source_required="exports")).allowed
    # An unknown label is refused by both.
    sb.find(r, "export:mnt-data-training-a")["details"]["source"] = "fixture"
    assert one(r, profile=make_profile(export_source_required="exports")).quality == "UNKNOWN"


# ---------------------------------------------------------------------------
# T-09: export access coverage
# ---------------------------------------------------------------------------


def with_rules(result, rules):
    r = copy.deepcopy(result)
    sb.find(r, "export:mnt-data-training-a")["details"]["rules"] = rules
    return r


@pytest.mark.parametrize(
    "rules, quality, allowed, code",
    [
        ([sb.rule("*")], "VALID", True, "NORMAL"),
        ([sb.rule("10.10.0.0/16")], "VALID", True, "NORMAL"),
        ([sb.rule("10.10.10.0/24"), sb.rule("10.10.20.0/24")], "VALID", True, "NORMAL"),
        ([sb.rule("10.10.10.0/255.255.255.0"), sb.rule("10.10.20.0/24")], "VALID", True, "NORMAL"),
        ([sb.rule("10.10.10.0/24")], "VALID", False, "EXPORT_ACCESS_MISSING"),
        ([sb.rule("10.10.10.0/25"), sb.rule("10.10.20.0/24")], "VALID", False, "EXPORT_ACCESS_MISSING"),
        ([sb.rule("10.10.0.0/16", writable=False)], "VALID", False, "EXPORT_READ_ONLY"),
        ([sb.rule("10.10.0.0/16", security=["krb5p"])], "VALID", False, "EXPORT_SECURITY_MISMATCH"),
        ([sb.rule("10.10.0.0/16", security=["krb5p", "sys"])], "VALID", True, "NORMAL"),
        ([sb.rule("@trusted")], "UNKNOWN", False, "EXPORT_RULE_UNSUPPORTED"),
        ([sb.rule("*.example.com")], "UNKNOWN", False, "EXPORT_RULE_UNSUPPORTED"),
        ([sb.rule("10.10.10.0/24"), sb.rule("@trusted")], "UNKNOWN", False, "EXPORT_RULE_UNSUPPORTED"),
        ([sb.rule("10.10.0.0/16"), sb.rule("10.10.10.0/24", writable=False)], "UNKNOWN", False, "EXPORT_RULES_CONFLICT"),
        ([sb.rule("10.10.0.0/16", writable=None)], "UNKNOWN", False, "EXPORT_RULES_CONFLICT"),
        ([], "VALID", False, "EXPORT_ACCESS_MISSING"),
    ],
)
def test_t09_export_coverage(base_result, rules, quality, allowed, code):
    a = one(with_rules(base_result, rules))
    assert (a.quality, a.allowed) == (quality, allowed), a.reason_codes
    assert code in a.reason_codes


def test_t09_ipv6_networks(base_result):
    b = make_binding(0, "training-a", "/mnt/data/training-a", "training-a:7", networks=("fd00:10::/64",))
    a = one(with_rules(base_result, [sb.rule("fd00:10::/48")]), binding=b)
    assert a.allowed
    a2 = one(with_rules(base_result, [sb.rule("10.10.0.0/16")]), binding=b)
    assert not a2.allowed and "EXPORT_ACCESS_MISSING" in a2.reason_codes


# ---------------------------------------------------------------------------
# Identity, graph, share presence, freshness, capabilities
# ---------------------------------------------------------------------------


def test_identity_mismatch_is_a_valid_deny(base_result):
    out = evaluate_result(base_result, make_profile(), (make_binding(0, "training-a", "/mnt/data/training-a"),), "other-node", 0)
    a = out[0]
    assert a.quality == "VALID" and not a.allowed and a.reason_codes == ["IDENTITY_MISMATCH"]
    assert a.capacity_domain_id is None and a.target_incarnation is None


def test_incarnation_mismatch_is_a_valid_deny(base_result):
    a = one(base_result, binding=make_binding(0, "training-a", "/mnt/data/training-a", "training-a:6"))
    assert a.quality == "VALID" and not a.allowed and "INCARNATION_MISMATCH" in a.reason_codes
    assert a.target_incarnation == "training-a:7"


def test_export_path_mismatch_is_a_valid_deny(base_result):
    a = one(base_result, binding=make_binding(0, "training-a", "/mnt/data/other", "training-a:7"))
    assert not a.allowed and "EXPORT_PATH_MISMATCH" in a.reason_codes


def test_binding_without_expected_incarnation_is_unknown_not_trusted(base_result):
    """Audit C-05: an unpinned binding proves nothing; the operator pins the
    incarnation from `discover` and rebinds on change."""
    a = one(base_result, binding=make_binding(0, "training-a", "/mnt/data/training-a", None))
    assert a.quality == "UNKNOWN" and not a.allowed
    assert a.reason_codes == ["INCARNATION_UNPINNED"]
    assert a.target_incarnation == "training-a:7"


def test_xmod14_share_absent_complete_vs_partial(base_result):
    b = make_binding(5, "gone", "/mnt/data/gone")
    a = assess(base_result, (b,))["gone"]
    assert a.quality == "VALID" and not a.allowed and a.reason_codes == ["SHARE_ABSENT"]
    r = copy.deepcopy(base_result)
    r["snapshot_status"] = "PARTIAL"
    a2 = assess(r, (b,))["gone"]
    assert a2.quality == "UNKNOWN" and a2.reason_codes == ["SHARE_UNRESOLVED"]


def test_failed_snapshot_blocks_everything(base_result, bindings):
    r = copy.deepcopy(base_result)
    r["snapshot_status"] = "FAILED"
    out = assess(r, bindings)
    assert all(a.quality == "UNKNOWN" and a.reason_codes == ["SOURCE_FAILED"] for a in out.values())


def test_share_collection_error_is_unknown(base_result):
    r = copy.deepcopy(base_result)
    r["shares"][0]["collection_status"] = "UNKNOWN"
    r["shares"][0]["reason_codes"] = ["EXPORT_ABSENT"]
    a = one(r)
    assert a.quality == "UNKNOWN" and "SHARE_COLLECTION_ERROR" in a.reason_codes
    assert a.diagnostics["share_reason_codes"] == ["EXPORT_ABSENT"]


# ---------------------------------------------------------------------------
# Audit C-01: schema-valid but semantically wrong graphs are UNKNOWN
# ---------------------------------------------------------------------------


def _swap_share_ref(r, key, value):
    r = copy.deepcopy(r)
    r["shares"][0][key] = value
    return r


@pytest.mark.parametrize(
    "mutate, what",
    [
        # the share points at another share's export (path differs)
        (lambda r: _swap_share_ref(r, "export_ref", "export:mnt-data-training-b"), "export path"),
        # the share points at a filesystem that does not contain its path
        (lambda r: _swap_share_ref(r, "filesystem_ref", "fs:mnt-data2.mount"), "filesystem does not contain"),
        # the share points at an ARRAY where a FILESYSTEM is expected
        (lambda r: _swap_share_ref(r, "filesystem_ref", "array:data1"), "not a FILESYSTEM"),
        # the share points at the export where the service is expected
        (lambda r: _swap_share_ref(r, "service_ref", "export:mnt-data-training-a"), "not an NFS_SERVICE"),
        # the share points at the nfs service where the export is expected
        (lambda r: _swap_share_ref(r, "export_ref", "nfs:nfs-server"), "not an EXPORT"),
    ],
)
def test_c01_reference_substitution_is_unknown(base_result, mutate, what):
    a = one(mutate(base_result))
    assert a.quality == "UNKNOWN" and not a.allowed, a.reason_codes
    assert "GRAPH_INCONSISTENT" in a.reason_codes
    assert any(what in note for note in a.diagnostics["graph_inconsistent"]), a.diagnostics


def test_c01_data_array_must_be_the_filesystem_source_device(base_result):
    r = copy.deepcopy(base_result)
    fsd = sb.find(r, "fs:mnt-data.mount")["details"]
    fsd["array_refs"] = [{"role": "DATA", "resource_id": "array:data2"}, {"role": "LOG", "resource_id": "array:log1"}]
    a = one(r)
    assert a.quality == "UNKNOWN" and "GRAPH_INCONSISTENT" in a.reason_codes
    assert any("DATA array volume" in n for n in a.diagnostics["graph_inconsistent"])


def test_c01_log_array_must_match_the_logdev_option(base_result):
    r = copy.deepcopy(base_result)
    fsd = sb.find(r, "fs:mnt-data.mount")["details"]
    fsd["array_refs"] = [{"role": "DATA", "resource_id": "array:data1"}, {"role": "LOG", "resource_id": "array:log2"}]
    a = one(r)
    assert a.quality == "UNKNOWN" and "GRAPH_INCONSISTENT" in a.reason_codes
    assert any("LOG array volume" in n for n in a.diagnostics["graph_inconsistent"])


def test_c01_most_specific_filesystem_wins(base_result):
    """A filesystem mounted deeper under the share path than the referenced
    one means the share was pinned to a parent (T-05)."""
    r = copy.deepcopy(base_result)
    nested = sb.filesystem("mnt-data-training--a.mount", "fs-uuid-03", "/mnt/data/training-a", "data2", "log2")
    r["resources"].append(nested)
    a = one(r)
    assert a.quality == "UNKNOWN" and "GRAPH_INCONSISTENT" in a.reason_codes
    assert any("more specific" in n for n in a.diagnostics["graph_inconsistent"])


def test_c01_internal_log_with_a_logdev_is_inconsistent(base_result):
    r = copy.deepcopy(base_result)
    fsd = sb.find(r, "fs:mnt-data.mount")["details"]
    fsd["log_mode"] = "INTERNAL"
    a = one(r)
    assert a.quality == "UNKNOWN" and "GRAPH_INCONSISTENT" in a.reason_codes


def test_c01_healthy_graph_has_no_inconsistency_notes(base_result):
    a = one(base_result)
    assert a.allowed and "graph_inconsistent" not in a.diagnostics


# ---------------------------------------------------------------------------
# Audit C-02: evidence time and age are mandatory on every SUCCESS record
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource_id", ["fs:mnt-data.mount", "export:mnt-data-training-a", "nfs:nfs-server", "array:data1", "array:log1"])
@pytest.mark.parametrize("field", ["evidence_age_ms", "observed_at"])
def test_c02_success_without_evidence_is_unknown(base_result, resource_id, field):
    r = copy.deepcopy(base_result)
    sb.find(r, resource_id)[field] = None
    a = one(r)
    assert a.quality == "UNKNOWN" and not a.allowed
    assert "EVIDENCE_AGE_MISSING" in a.reason_codes


def test_c02_share_without_evidence_is_unknown(base_result):
    r = copy.deepcopy(base_result)
    r["shares"][0]["evidence_age_ms"] = None
    a = one(r)
    assert a.quality == "UNKNOWN" and "EVIDENCE_AGE_MISSING" in a.reason_codes


def test_c02_missing_ages_never_make_the_snapshot_look_fresh(base_result):
    """Every age dropped: the freshness check has nothing to measure."""
    r = copy.deepcopy(base_result)
    for rec in r["resources"] + r["shares"]:
        rec["evidence_age_ms"] = None
    a = one(r)
    assert a.quality == "UNKNOWN" and {"EVIDENCE_AGE_MISSING", "SOURCE_STALE"} <= set(a.reason_codes)
    assert a.evidence_age_ms is None


# ---------------------------------------------------------------------------
# Audit C-03: identity, edition/version and level are proven, not assumed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["uuid", "incarnation"])
@pytest.mark.parametrize("value", [None, ""])
def test_c03_filesystem_identity_is_mandatory(base_result, field, value):
    r = copy.deepcopy(base_result)
    sb.find(r, "fs:mnt-data.mount")["details"][field] = value
    a = one(r)
    assert a.quality == "UNKNOWN" and "FILESYSTEM_IDENTITY_MISSING" in a.reason_codes
    assert a.capacity_domain_id is None


@pytest.mark.parametrize(
    "edition, version",
    [("Opus", "4.4.0"), ("Classic", "9.0.0"), ("Classic", "4.3.2"), ("Classic", "unknown"), ("Classic", None), (None, "4.4.0"), ("Classic", "5.0.0")],
)
def test_c03_unsupported_edition_or_version_is_unknown(base_result, edition, version):
    r = copy.deepcopy(base_result)
    d = sb.find(r, "array:data1")["details"]
    d["edition"] = edition
    d["version"] = version
    a = one(r)
    assert a.quality == "UNKNOWN" and "XIRAID_VERSION_UNSUPPORTED" in a.reason_codes
    assert a.diagnostics["unsupported_versions"] == [{"edition": edition, "version": version}]


@pytest.mark.parametrize("version", ["4.4.0", "4.4.1", "4.4", "4.4.0-43861"])
def test_c03_supported_versions(base_result, version):
    r = copy.deepcopy(base_result)
    sb.find(r, "array:data1")["details"]["version"] = version
    assert one(r).allowed


def test_c03_empty_members_is_unknown(base_result):
    r = copy.deepcopy(base_result)
    sb.find(r, "array:data1")["details"]["members"] = []
    a = one(r)
    assert a.quality == "UNKNOWN" and "MEMBER_STATE_MISSING" in a.reason_codes


@pytest.mark.parametrize("level", ["raid8", "", None, "n+m+1", "raid"])
def test_c03_unknown_raid_level_is_unknown(base_result, level):
    r = copy.deepcopy(base_result)
    sb.find(r, "array:data1")["details"]["raid_level"] = level
    a = one(r)
    assert a.quality == "UNKNOWN" and "RAID_LEVEL_UNSUPPORTED" in a.reason_codes


@pytest.mark.parametrize("level", ["raid0", "raid1", "raid5", "raid6", "raid7", "raid10", "raid50", "raid60", "raid70", "n+m", "5", "RAID5"])
def test_c03_supported_raid_levels(base_result, level):
    r = copy.deepcopy(base_result)
    d = sb.find(r, "array:data1")["details"]
    d["raid_level"] = level
    d["raw_states"] = ["online", "initialized"]
    assert one(r).allowed, level


# ---------------------------------------------------------------------------
# Audit C-06: an unsupported rule present in the export makes access unproven
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rules",
    [
        [sb.rule("*"), sb.rule("nas-clients.example.org", writable=False)],
        [sb.rule("10.10.0.0/16"), sb.rule("@trusted")],
        [sb.rule("10.10.0.0/16"), sb.rule("*.example.org")],
    ],
)
def test_c06_any_unsupported_rule_is_unknown_even_when_ip_rules_cover(base_result, rules):
    r = copy.deepcopy(base_result)
    sb.find(r, "export:mnt-data-training-a")["details"]["rules"] = rules
    a = one(r)
    assert a.quality == "UNKNOWN" and not a.allowed
    assert "EXPORT_RULE_UNSUPPORTED" in a.reason_codes and "EXPORT_ACCESS_MISSING" not in a.reason_codes
    assert a.diagnostics["export_rules_unsupported"] == 1


def test_graph_unresolved(base_result):
    r = copy.deepcopy(base_result)
    r["shares"][0]["filesystem_ref"] = "fs:nowhere"
    a = one(r)
    assert a.quality == "UNKNOWN" and a.reason_codes == ["GRAPH_UNRESOLVED"]


def test_missing_dependencies_fixture(bindings):
    import json, os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "xinas", "missing-dependencies.json")
    with open(path, encoding="utf-8") as fh:
        result = json.load(fh)["result"]
    out = assess(result, bindings)
    assert all(a.quality == "UNKNOWN" for a in out.values())
    # xiNAS already marks the share UNKNOWN when its filesystem lost its arrays.
    assert out["training-a"].reason_codes == ["SHARE_COLLECTION_ERROR"]
    assert out["training-a"].diagnostics["share_reason_codes"] == ["DEPENDENCY_FILESYSTEM_UNAVAILABLE"]


def test_source_freshness_uses_oldest_evidence_plus_request_duration(base_result):
    r = copy.deepcopy(base_result)
    sb.find(r, "array:log1")["evidence_age_ms"] = 18500
    a = one(r, duration=1000)
    assert a.allowed and a.evidence_age_ms == 19500
    b = one(r, duration=1600)
    assert b.quality == "UNKNOWN" and "SOURCE_STALE" in b.reason_codes
    assert b.evidence_age_ms == 20100


def test_capability_missing_fails_closed(base_result):
    r = copy.deepcopy(base_result)
    r["capabilities"].remove("nfs.service")
    a = one(r)
    assert a.quality == "UNKNOWN" and a.reason_codes == ["CAPABILITY_MISSING"]
    assert a.diagnostics["missing_checks"] == ["nfs.service"]
    r2 = copy.deepcopy(base_result)
    for row in r2["coverage"]:
        if row["check"] == "identity":
            row["status"] = "ERROR"
            row["reason"] = "BROKEN"
    assert one(r2).reason_codes == ["CAPABILITY_MISSING"]


# ---------------------------------------------------------------------------
# T-10: multiplier invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ppm", [-1, 1000001, 0.5, float("nan"), "250000", True, None])
def test_t10_invalid_multiplier_never_allows(ppm):
    from lattice_ds_connector.modules.base import Assessment

    a = Assessment(0, 1, "ds", "t", "inc", "VALID", True, ppm, ["NORMAL"], sb.AT, 10, "dom", ["r"]).normalized()
    assert a.quality == "UNKNOWN" and not a.allowed and a.multiplier_ppm == 0
    assert "INVALID_MULTIPLIER" in a.reason_codes


def test_t10_zero_multiplier_never_allows():
    from lattice_ds_connector.modules.base import Assessment

    a = Assessment(0, 1, "ds", "t", "inc", "VALID", True, 0, [], sb.AT, 10, "dom", []).normalized()
    assert not a.allowed and "ZERO_MULTIPLIER" in a.reason_codes


def test_t10_valid_allow_needs_identity_domain_and_evidence():
    from lattice_ds_connector.modules.base import Assessment

    for field in ("target_incarnation", "observed_at", "evidence_age_ms", "capacity_domain_id"):
        kw = dict(target_incarnation="inc", observed_at=sb.AT, evidence_age_ms=10, capacity_domain_id="dom")
        kw[field] = None
        a = Assessment(0, 1, "ds", "t", kw["target_incarnation"], "VALID", True, 1000000, ["NORMAL"], kw["observed_at"], kw["evidence_age_ms"], kw["capacity_domain_id"], []).normalized()
        assert a.quality == "UNKNOWN" and not a.allowed and "EVIDENCE_INCOMPLETE" in a.reason_codes, field


def test_reason_codes_are_bounded(base_result):
    a = one(base_result)
    assert len(a.reason_codes) <= contract.MAX_REASON_CODES
    assert all(code in contract.ALL_REASON_CODES for code in a.reason_codes)
