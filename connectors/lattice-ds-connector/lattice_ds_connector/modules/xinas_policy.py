# SPDX-License-Identifier: MIT
"""The xinas-mvp v1 decision policy (XMOD-03..14) as pure functions.

Input: one xiNAS ``GET /api/v1/placement/observations`` result (schema 1.0),
the profile and the instance's bindings. Output: one deterministic
:class:`Assessment` per binding. No clocks, no I/O; the runtime adds TTL and
hold-down afterwards.

The vocabulary and the decision table come from the requirements package
(section 5) and xiRAID Classic 4.4's "Showing RAID state" page.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .. import contract
from ..config import Binding, Profile
from .base import Assessment

# Array words → veto reason (XMOD table).
_ARRAY_VETO = {
    "reconstructing": "RECONSTRUCTION_ACTIVE",
    "initing": "INITIALIZATION_ACTIVE",
    "restriping": "RESTRIPE_ACTIVE",
    "need_recon": "RECONSTRUCTION_REQUIRED",
    "need_init": "INITIALIZATION_REQUIRED",
    "inconsistent": "INTEGRITY_ERROR",
    "unrecovered": "UNRECOVERED",
    "read_only": "READ_ONLY",
    "offline": "ARRAY_UNAVAILABLE",
    "none": "ARRAY_UNAVAILABLE",
}
_MEMBER_VETO = {
    "reconstructing": "MEMBER_RECONSTRUCTION_ACTIVE",
    "need_recon": "MEMBER_RECONSTRUCTION_REQUIRED",
}

OPTIONAL_NOT_IMPLEMENTED = (
    ("filesystem.integrity", "OUT_OF_MVP"),
    ("network.path", "OUT_OF_MVP"),
    ("network.performance", "OUT_OF_MVP"),
)


class Verdict:
    """Accumulates the three reason classes for one binding."""

    def __init__(self) -> None:
        self.unknown: List[str] = []
        self.veto: List[str] = []
        self.penalties: List[Tuple[int, str]] = []
        self.info: List[str] = []
        self.diag: Dict[str, Any] = {}

    def add_unknown(self, code: str) -> None:
        if code not in self.unknown:
            self.unknown.append(code)

    def add_veto(self, code: str) -> None:
        if code not in self.veto:
            self.veto.append(code)

    def add_penalty(self, ppm: int, code: str) -> None:
        self.penalties.append((ppm, code))

    def add_info(self, code: str) -> None:
        if code not in self.info:
            self.info.append(code)

    @property
    def is_unknown(self) -> bool:
        return bool(self.unknown)

    def reasons(self) -> List[str]:
        out: List[str] = []
        for code in self.unknown + self.veto + [c for _, c in sorted(self.penalties)] + self.info:
            if code not in out:
                out.append(code)
        return out

    def multiplier(self) -> int:
        if self.unknown or self.veto:
            return 0
        if not self.penalties:
            return contract.PPM_FULL
        return min(p for p, _ in self.penalties)


# ---------------------------------------------------------------------------
# Arrays and members (XMOD-06..10)
# ---------------------------------------------------------------------------


def normalize_level(raw: Any) -> str:
    """xiNAS publishes ``raid5`` / ``raid10`` (its parser's spelling); the
    package's examples say ``5``. Both mean the same level."""
    text = str(raw if raw is not None else "").strip().lower()
    return text[4:] if text.startswith("raid") else text


def evaluate_array(details: Dict[str, Any], profile: Profile, v: Verdict) -> None:
    """Apply the decision table to one ARRAY resource's details."""
    words_raw = details.get("raw_states")
    valid = details.get("state_valid") is True
    words: List[str] = [w for w in words_raw if isinstance(w, str)] if isinstance(words_raw, list) else []
    if not valid or not isinstance(words_raw, list) or not words_raw or len(words) != len(words_raw):
        v.add_unknown("SOURCE_UNKNOWN")
        return
    word_set = set(words)
    unknown_words = word_set - contract.XIRAID_ARRAY_WORDS
    if unknown_words:
        v.add_unknown("SOURCE_UNKNOWN")
        v.diag.setdefault("unknown_words", sorted(unknown_words))
    for w in words:
        code = _ARRAY_VETO.get(w)
        if code:
            v.add_veto(code)
    if "online" not in word_set:
        v.add_veto("ARRAY_UNAVAILABLE")
    level = normalize_level(details.get("raid_level"))
    if "online" in word_set and level not in contract.LEVELS_WITHOUT_INITIALIZATION:
        if "initialized" not in word_set and not ({"initing", "need_init"} & word_set):
            # Online, but no proof of initialization readiness where it
            # applies (an offline/none array is already a proven veto).
            v.add_unknown("SOURCE_UNKNOWN")
    if "degraded" in word_set:
        v.add_penalty(profile.degraded_multiplier_ppm, "REDUNDANCY_DEGRADED")
    if "need_restripe" in word_set:
        v.add_penalty(profile.need_restripe_multiplier_ppm, "RESTRIPE_PENDING")
    if "sdc_scanning" in word_set:
        v.add_penalty(profile.scan_multiplier_ppm, "SCAN_ACTIVE")
    members = details.get("members")
    if not isinstance(members, list):
        v.add_unknown("MEMBER_STATE_MISSING")
        return
    for m in members:
        evaluate_member(m if isinstance(m, dict) else {}, profile, v)


def evaluate_member(member: Dict[str, Any], profile: Profile, v: Verdict) -> None:
    raw = member.get("raw_states")
    if member.get("state_valid") is not True:
        v.add_unknown("MEMBER_STATE_INVALID")
        return
    if not isinstance(raw, list) or not raw:
        v.add_unknown("MEMBER_STATE_MISSING")
        return
    words = [w for w in raw if isinstance(w, str)]
    if len(words) != len(raw) or set(words) - contract.XIRAID_MEMBER_WORDS:
        v.add_unknown("MEMBER_STATE_INVALID")
        return
    for w in words:
        code = _MEMBER_VETO.get(w)
        if code:
            v.add_veto(code)
    if "offline" in words:
        v.add_penalty(profile.degraded_multiplier_ppm, "MEMBER_OFFLINE")


# ---------------------------------------------------------------------------
# Export access (XMOD-13, T-09)
# ---------------------------------------------------------------------------


def _parse_client(client: str) -> Optional[Any]:
    """An IPv4/IPv6 address or network, ``*`` for everything, None if unsupported."""
    if client == "*":
        return "*"
    try:
        return ipaddress.ip_network(client, strict=False)
    except ValueError:
        return None


def evaluate_export_access(rules: Sequence[Dict[str, Any]], binding: Binding, v: Verdict) -> None:
    supported: List[Tuple[Any, Dict[str, Any]]] = []
    unsupported = 0
    for r in rules:
        client = r.get("client")
        parsed = _parse_client(client) if isinstance(client, str) else None
        if parsed is None:
            unsupported += 1
            continue
        supported.append((parsed, r))
    v.diag["export_rules_unsupported"] = unsupported
    for net_text in binding.expected_client_networks:
        try:
            net = ipaddress.ip_network(net_text, strict=False)
        except ValueError:
            v.add_unknown("BINDING_INVALID")
            continue
        covering = [
            r
            for parsed, r in supported
            if parsed == "*" or (parsed.version == net.version and net.subnet_of(parsed))
        ]
        if not covering:
            if unsupported:
                v.add_unknown("EXPORT_RULE_UNSUPPORTED")
            else:
                v.add_veto("EXPORT_ACCESS_MISSING")
            continue
        writables = {r.get("writable") for r in covering}
        if None in writables or writables == {True, False}:
            v.add_unknown("EXPORT_RULES_CONFLICT")
            continue
        if writables == {False}:
            v.add_veto("EXPORT_READ_ONLY")
            continue
        wanted = set(binding.expected_security)
        if not any(wanted <= {s for s in r.get("security", []) if isinstance(s, str)} for r in covering):
            v.add_veto("EXPORT_SECURITY_MISMATCH")


# ---------------------------------------------------------------------------
# The per-binding assessment (XMOD-03, 04, 11, 12, 14)
# ---------------------------------------------------------------------------


def _resources_by_id(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in result.get("resources", []):
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            out[r["id"]] = r
    return out


def _shares_by_id(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for s in result.get("shares", []):
        if isinstance(s, dict) and isinstance(s.get("share_id"), str):
            out[s["share_id"]] = s
    return out


def _age(rec: Optional[Dict[str, Any]]) -> Optional[int]:
    if rec is None:
        return None
    a = rec.get("evidence_age_ms")
    return a if isinstance(a, int) and not isinstance(a, bool) and a >= 0 else None


def _coverage(profile: Profile, log_mode: Optional[str], has_rt: bool) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = [
        {"check": c, "required": True, "status": contract.COVERAGE_EVALUATED} for c in profile.required_checks
    ]
    if log_mode == "INTERNAL":
        rows.append({"check": "topology.log", "required": False, "status": contract.COVERAGE_NOT_APPLICABLE, "reason": "INTERNAL_LOG"})
    elif log_mode == "EXTERNAL":
        rows.append({"check": "topology.log", "required": False, "status": contract.COVERAGE_EVALUATED})
    if log_mode in ("INTERNAL", "EXTERNAL"):
        rows.append(
            {"check": "topology.realtime", "required": False, "status": contract.COVERAGE_EVALUATED}
            if has_rt
            else {"check": "topology.realtime", "required": False, "status": contract.COVERAGE_NOT_APPLICABLE, "reason": "NO_REALTIME_DEVICE"}
        )
    for check, reason in OPTIONAL_NOT_IMPLEMENTED:
        rows.append({"check": check, "required": False, "status": contract.COVERAGE_NOT_IMPLEMENTED, "reason": reason})
    return rows[: contract.MAX_COVERAGE_ROWS]


def assess_binding(
    result: Dict[str, Any],
    profile: Profile,
    binding: Binding,
    expected_controller_id: str,
    request_duration_ms: int,
    shares: Optional[Dict[str, Dict[str, Any]]] = None,
    resources: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Assessment:
    shares = shares if shares is not None else _shares_by_id(result)
    resources = resources if resources is not None else _resources_by_id(result)
    v = Verdict()
    controller = result.get("controller_id")
    snapshot_status = result.get("snapshot_status")
    diag: Dict[str, Any] = {
        "source_generation": result.get("source_generation"),
        "source_epoch": result.get("server_epoch"),
        "snapshot_status": snapshot_status,
    }
    v.diag = diag

    def finish(
        target_incarnation: Optional[str],
        observed_at: Optional[str],
        evidence_age: Optional[int],
        domain: Optional[str],
        shared: List[str],
        log_mode: Optional[str] = None,
        has_rt: bool = False,
    ) -> Assessment:
        quality = contract.QUALITY_UNKNOWN if v.is_unknown else contract.QUALITY_VALID
        allowed = quality == contract.QUALITY_VALID and not v.veto
        reasons = v.reasons()
        if allowed and not reasons:
            reasons = ["NORMAL"]
        if len(reasons) > contract.MAX_REASON_CODES:
            diag["reason_overflow"] = len(reasons) - contract.MAX_REASON_CODES
            reasons = reasons[: contract.MAX_REASON_CODES]
        return Assessment(
            ds_id=binding.ds_id,
            binding_generation=binding.binding_generation,
            datastore_id=expected_controller_id,
            target_id=binding.target_id,
            target_incarnation=target_incarnation,
            quality=quality,
            allowed=allowed,
            multiplier_ppm=v.multiplier() if allowed else 0,
            reason_codes=reasons,
            observed_at=observed_at,
            evidence_age_ms=None if evidence_age is None else evidence_age + max(0, request_duration_ms),
            capacity_domain_id=domain,
            shared_resource_ids=shared[: contract.MAX_SHARED_RESOURCE_IDS],
            coverage=_coverage(profile, log_mode, has_rt),
            diagnostics=diag,
        ).normalized()

    # --- identity and capabilities ---------------------------------------
    if controller != expected_controller_id:
        v.add_veto("IDENTITY_MISMATCH")
        diag["controller_id"] = controller
        return finish(None, None, None, None, [])
    caps = {c for c in result.get("capabilities", []) if isinstance(c, str)}
    evaluated = {
        row.get("check")
        for row in result.get("coverage", [])
        if isinstance(row, dict) and row.get("status") == contract.COVERAGE_EVALUATED
    }
    missing = [c for c in profile.required_checks if c not in caps or c not in evaluated]
    if missing:
        v.add_unknown("CAPABILITY_MISSING")
        diag["missing_checks"] = missing
        return finish(None, None, None, None, [])
    if snapshot_status == contract.SNAPSHOT_FAILED:
        v.add_unknown("SOURCE_FAILED")
        return finish(None, None, None, None, [])

    # --- the share ----------------------------------------------------------
    share = shares.get(binding.target_id)
    if share is None:
        if snapshot_status == contract.SNAPSHOT_COMPLETE:
            v.add_veto("SHARE_ABSENT")
        else:
            v.add_unknown("SHARE_UNRESOLVED")
        return finish(None, None, None, None, [])
    diag["share_reason_codes"] = [r for r in share.get("reason_codes", []) if isinstance(r, str)]
    incarnation = share.get("incarnation") if isinstance(share.get("incarnation"), str) else None
    if share.get("export_path") != binding.endpoint.export_path:
        v.add_veto("EXPORT_PATH_MISMATCH")
        diag["source_export_path"] = share.get("export_path")
    if binding.expected_target_incarnation is not None and incarnation != binding.expected_target_incarnation:
        v.add_veto("INCARNATION_MISMATCH")
        diag["source_incarnation"] = incarnation
    if share.get("collection_status") != "SUCCESS":
        v.add_unknown("SHARE_COLLECTION_ERROR")
        return finish(incarnation, share.get("observed_at"), _age(share), None, [])

    # --- the graph ----------------------------------------------------------
    fs = resources.get(share.get("filesystem_ref") or "")
    ex = resources.get(share.get("export_ref") or "")
    svc = resources.get(share.get("service_ref") or "")
    if fs is None or ex is None or svc is None:
        v.add_unknown("GRAPH_UNRESOLVED")
        return finish(incarnation, share.get("observed_at"), _age(share), None, [])
    ages: List[Optional[int]] = [_age(share), _age(fs), _age(ex), _age(svc)]
    for dep, name in ((fs, "filesystem"), (ex, "export"), (svc, "service")):
        if dep.get("collection_status") != "SUCCESS":
            v.add_unknown("DEPENDENCY_ERROR")
            diag[f"{name}_reason_codes"] = [r for r in dep.get("reason_codes", []) if isinstance(r, str)]
    fsd = fs.get("details") if isinstance(fs.get("details"), dict) else {}
    exd = ex.get("details") if isinstance(ex.get("details"), dict) else {}
    svd = svc.get("details") if isinstance(svc.get("details"), dict) else {}
    domain = None
    shared: List[str] = []
    log_mode = None
    has_rt = False
    if fs.get("collection_status") == "SUCCESS":
        domain = f"{controller}/{fsd.get('uuid')}/{fsd.get('incarnation')}"
        # --- filesystem prerequisites (XMOD-11, 12) ---
        if fsd.get("fs_type") != "xfs":
            v.add_unknown("FILESYSTEM_TYPE_UNSUPPORTED")
        if "mount_source_mismatch" in fsd:
            v.add_unknown("FILESYSTEM_SOURCE_MISMATCH")
        mounted = fsd.get("mounted")
        if mounted is None:
            v.add_unknown("FILESYSTEM_MOUNT_UNKNOWN")
        elif mounted is False:
            v.add_veto("FILESYSTEM_NOT_MOUNTED")
        else:
            writable = fsd.get("writable")
            if writable is None:
                v.add_unknown("FILESYSTEM_MOUNT_UNKNOWN")
            elif writable is False:
                v.add_veto("FILESYSTEM_READ_ONLY")
        refs = {r.get("role"): r.get("resource_id") for r in fsd.get("array_refs", []) if isinstance(r, dict)}
        log_mode = fsd.get("log_mode") if isinstance(fsd.get("log_mode"), str) else None
        if "DATA" not in refs:
            v.add_unknown("DATA_ARRAY_UNRESOLVED")
        if fsd.get("external_dependencies_resolved") is False:
            v.add_unknown("EXTERNAL_DEVICE_UNRESOLVED")
        if log_mode == "EXTERNAL" and "LOG" not in refs:
            v.add_unknown("EXTERNAL_DEVICE_UNRESOLVED")
        elif log_mode not in ("INTERNAL", "EXTERNAL"):
            v.add_unknown("LOG_MODE_UNKNOWN")
        has_rt = "REALTIME" in refs
        raw_states: Dict[str, Any] = {}
        for role in ("DATA", "LOG", "REALTIME"):
            rid = refs.get(role)
            if not isinstance(rid, str):
                continue
            shared.append(f"{controller}/{rid}")
            arr = resources.get(rid)
            if arr is None:
                v.add_unknown("GRAPH_UNRESOLVED")
                continue
            ages.append(_age(arr))
            if arr.get("collection_status") != "SUCCESS":
                v.add_unknown("DEPENDENCY_ERROR")
                diag.setdefault("array_reason_codes", {})[rid] = [r for r in arr.get("reason_codes", []) if isinstance(r, str)]
                continue
            ad = arr.get("details") if isinstance(arr.get("details"), dict) else {}
            raw_states[rid] = ad.get("raw_states")
            evaluate_array(ad, profile, v)
        diag["array_raw_states"] = raw_states
    # --- export (XMOD-13) ---
    if ex.get("collection_status") == "SUCCESS":
        present = exd.get("present")
        diag["export_source"] = exd.get("source")
        if present is None:
            v.add_unknown("EXPORT_ABSENT")
        elif present is False:
            v.add_veto("EXPORT_ABSENT")
        else:
            src = exd.get("source")
            accepted = ("etab",) if profile.export_source_required == "etab" else ("etab", "/etc/exports")
            if src not in accepted:
                v.add_unknown("EXPORT_SOURCE_NOT_EFFECTIVE")
            rules = [r for r in exd.get("rules", []) if isinstance(r, dict)]
            evaluate_export_access(rules, binding, v)
    # --- service (XMOD-04) ---
    if svc.get("collection_status") == "SUCCESS":
        running = svd.get("running")
        if running is None:
            v.add_unknown("NFS_SERVICE_UNKNOWN")
        elif running is False:
            v.add_veto("NFS_SERVICE_STOPPED")
        else:
            protocols = {p for p in svd.get("protocols", []) if isinstance(p, str)}
            if any(p not in protocols for p in profile.required_protocols):
                v.add_veto("NFS_PROTOCOL_MISSING")
                diag["protocols"] = sorted(protocols)
    # --- freshness (CON-10, source.freshness) ---
    known_ages = [a for a in ages if a is not None]
    oldest = max(known_ages) if known_ages else None
    if oldest is None:
        v.add_unknown("SOURCE_STALE")
    elif oldest + max(0, request_duration_ms) > profile.source_max_age_ms:
        v.add_unknown("SOURCE_STALE")
        diag["oldest_evidence_age_ms"] = oldest
    return finish(incarnation, share.get("observed_at"), oldest, domain, shared, log_mode, has_rt)


def evaluate_result(
    result: Dict[str, Any],
    profile: Profile,
    bindings: Sequence[Binding],
    expected_controller_id: str,
    request_duration_ms: int,
) -> List[Assessment]:
    """One assessment per binding; a shared index of the snapshot is built once."""
    shares = _shares_by_id(result)
    resources = _resources_by_id(result)
    return [
        assess_binding(result, profile, b, expected_controller_id, request_duration_ms, shares, resources)
        for b in bindings
    ]
