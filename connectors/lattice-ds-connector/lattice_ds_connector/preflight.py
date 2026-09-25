# SPDX-License-Identifier: MIT
"""``lattice-ds-connector preflight`` -- read-only readiness report for an MDS
about to run ``placement_mode = smart`` (design section 11).

It reads ``/healthz`` and ``/v1/assessments`` from the local connector and
checks what the MDS will check: contract major, every expected DS bound,
no UNKNOWN or expired record, one digest per profile id, domain consistency
(records that share a ``capacity_domain_id`` share a ``datastore_id``).
It never changes anything and knows nothing about the MDS placement mode.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from . import contract

EXPECTED_CONTRACT_MAJOR = 1


def _major(version: Any) -> Optional[int]:
    if not isinstance(version, str) or not version:
        return None
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def parse_profile_pins(text: str) -> Dict[str, str]:
    """``id=digest[,id=digest...]`` -> dict; ValueError on the MDS's own
    config errors (same rule as pm_parse_profile_pins)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty value")
    out: Dict[str, str] = {}
    for item in text.split(","):
        if "=" not in item:
            raise ValueError("item %r is not id=digest" % item.strip())
        pid, dig = (s.strip() for s in item.split("=", 1))
        if not re.fullmatch(contract.PROFILE_ID_PATTERN, pid):
            raise ValueError("profile id %r must match [A-Za-z0-9._-]{1,63}" % pid)
        if not dig:
            raise ValueError("profile %s: empty digest" % pid)
        if len(dig) > contract.MAX_DIGEST_LEN:
            raise ValueError("profile %s: digest must be 1..%d bytes" % (pid, contract.MAX_DIGEST_LEN))
        if pid in out:
            raise ValueError("profile %s pinned twice" % pid)
        out[pid] = dig
    if len(out) > contract.MAX_PROFILES:
        raise ValueError("more than %d profiles" % contract.MAX_PROFILES)
    return dict(sorted(out.items()))


def format_profiles(profiles: Dict[str, str]) -> str:
    return ",".join("%s=%s" % kv for kv in sorted(profiles.items())) or "-"


def evaluate(health: Optional[Dict[str, Any]], batch: Optional[Dict[str, Any]],
             expect_ds: Sequence[int] = (),
             expect_profiles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Pure: the report the CLI prints. ``ready`` is true only when every
    check passes; ``reasons`` lists the failed ones with the DS they name."""
    reasons: List[str] = []
    rows: List[Dict[str, Any]] = []
    profiles: Dict[str, str] = {}
    inconsistent: List[str] = []
    domains: Dict[str, str] = {}
    seen_ds: Dict[int, str] = {}
    if not isinstance(health, dict):
        reasons.append("HEALTHZ_UNAVAILABLE")
    elif not health.get("ready"):
        reasons.append("CONNECTOR_NOT_READY")
    if not isinstance(batch, dict):
        reasons.append("ASSESSMENTS_UNAVAILABLE")
        return {"ready": False, "reasons": reasons, "ds": rows}
    if _major(batch.get("contract_version")) != EXPECTED_CONTRACT_MAJOR:
        reasons.append("CONTRACT_MAJOR:%s" % batch.get("contract_version"))
    for inst in batch.get("instances", []) or []:
        iid = inst.get("connector_instance_id", "?")
        failed = inst.get("snapshot_status") == contract.SNAPSHOT_FAILED
        for a in inst.get("assessments", []) or []:
            ds = a.get("ds_id")
            placement = a.get("placement") or {}
            profile = a.get("profile") or {}
            resources = a.get("resources") or {}
            quality = a.get("quality")
            if failed:
                quality = contract.QUALITY_UNKNOWN
            row = {
                "ds_id": ds,
                "instance": iid,
                "bound": True,
                "quality": quality,
                "allowed": bool(placement.get("allowed")),
                "multiplier_ppm": placement.get("multiplier_ppm"),
                "remaining_ttl_ms": a.get("remaining_ttl_ms"),
                "capacity_domain_id": resources.get("capacity_domain_id"),
                "datastore_id": a.get("datastore_id"),
                "target_id": a.get("target_id"),
                "target_incarnation": a.get("target_incarnation"),
                "binding_generation": a.get("binding_generation"),
                "reason_codes": list(placement.get("reason_codes") or []),
            }
            rows.append(row)
            if isinstance(ds, int):
                if ds in seen_ds:
                    reasons.append("DUPLICATE_DS:%d" % ds)
                seen_ds[ds] = iid
            pid, pdig = profile.get("id"), profile.get("digest")
            if isinstance(pid, str) and isinstance(pdig, str):
                if pid in profiles and profiles[pid] != pdig and pid not in inconsistent:
                    inconsistent.append(pid)
                profiles.setdefault(pid, pdig)
            if quality != contract.QUALITY_VALID:
                reasons.append("UNKNOWN:ds%s" % ds)
            if not isinstance(a.get("remaining_ttl_ms"), int) or a.get("remaining_ttl_ms", 0) <= 0:
                reasons.append("EXPIRED:ds%s" % ds)
            dom = resources.get("capacity_domain_id")
            if isinstance(dom, str) and dom:
                owner = a.get("datastore_id")
                if dom in domains and domains[dom] != owner:
                    reasons.append("DOMAIN_INCONSISTENT:%s" % dom)
                domains.setdefault(dom, owner)
    for want in expect_ds:
        if want not in seen_ds:
            reasons.append("UNBOUND:ds%d" % want)
    for pid in inconsistent:
        reasons.append("PROFILE_INCONSISTENT:%s" % pid)
    if expect_profiles is not None:
        for pid in sorted(profiles):
            if pid not in expect_profiles:
                reasons.append("PROFILE_NOT_PINNED:%s" % pid)
            elif expect_profiles[pid] != profiles[pid]:
                reasons.append("PROFILE_PIN_MISMATCH:%s" % pid)
    report = {
        "ready": not reasons,
        "reasons": reasons,
        "contract_version": batch.get("contract_version"),
        "runtime_epoch": batch.get("runtime_epoch"),
        "config_digest": batch.get("config_digest"),
        "profiles": dict(sorted(profiles.items())),
        "generated_at": batch.get("generated_at"),
        "instances": [
            {"id": i.get("connector_instance_id"), "module": i.get("module_type"), "epoch": i.get("epoch"),
             "sequence": i.get("sequence"), "snapshot_status": i.get("snapshot_status")}
            for i in (batch.get("instances") or [])
        ],
        "ds": sorted(rows, key=lambda r: (r["ds_id"] if isinstance(r["ds_id"], int) else 1 << 30)),
        "expected_ds": list(expect_ds),
    }
    return report


def render(report: Dict[str, Any]) -> str:
    lines = []
    lines.append("READY" if report["ready"] else "NOT_READY " + " ".join(report["reasons"]))
    if "contract_version" in report:
        lines.append(
            "contract=%s runtime_epoch=%s config_digest=%s profiles=%s"
            % (report.get("contract_version"), report.get("runtime_epoch"),
               report.get("config_digest"), format_profiles(report.get("profiles") or {}))
        )
    for i in report.get("instances", []):
        lines.append("instance %s (%s) epoch=%s seq=%s snapshot=%s" % (i["id"], i["module"], i["epoch"], i["sequence"], i["snapshot_status"]))
    for r in report.get("ds", []):
        lines.append(
            "  ds %3s %-8s allowed=%-5s ppm=%-7s ttl_ms=%-6s domain=%s datastore=%s target=%s gen=%s reasons=%s"
            % (r["ds_id"], r["quality"], r["allowed"], r["multiplier_ppm"], r["remaining_ttl_ms"],
               r["capacity_domain_id"], r["datastore_id"], r["target_id"], r["binding_generation"],
               ",".join(r["reason_codes"]))
        )
    for want in report.get("expected_ds", []):
        if not any(r["ds_id"] == want for r in report.get("ds", [])):
            lines.append("  ds %3s UNBOUND" % want)
    return "\n".join(lines)


def run(socket_path: str, expect_ds: Sequence[int], as_json: bool,
        expect_profiles: Optional[Dict[str, str]] = None) -> int:
    from .server import get_json

    health = batch = None
    try:
        _st, health = get_json(socket_path, "/healthz", timeout_s=2.0)
    except OSError:
        health = None
    try:
        st, doc = get_json(socket_path, "/v1/assessments", timeout_s=2.0)
        batch = doc if st == 200 and isinstance(doc, dict) else None
    except OSError:
        batch = None
    report = evaluate(health if isinstance(health, dict) else None, batch, expect_ds,
                       expect_profiles=expect_profiles)
    print(json.dumps(report, indent=2) if as_json else render(report))
    return 0 if report["ready"] else 1
