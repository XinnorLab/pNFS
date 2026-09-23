# SPDX-License-Identifier: MIT
"""Builders for xiNAS placement-observations envelopes used by tests and the
golden fixtures under ``fixtures/xinas/``.

The base topology is the T-04 stand: shares A and B on filesystem fs-01
(data1 + external log1), share C on fs-02 (data2 + external log2), one NFS
service, one export per share. Everything is illustrative.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

AT = "2026-09-22T12:00:00Z"
CONTROLLER = "xinas-node-01"
CAPABILITIES = [
    "raid.array_states",
    "raid.member_states",
    "topology.data_log_realtime",
    "identity",
    "filesystem.mounted_rw",
    "export.effective_access",
    "nfs.service",
    "source.freshness",
]


def coverage_rows() -> List[Dict[str, Any]]:
    rows = [{"check": c, "required": True, "status": "EVALUATED"} for c in CAPABILITIES]
    rows[5] = {**rows[5], "details": {"source": "etab"}}
    for check in ("filesystem.integrity", "network.path", "network.performance"):
        rows.append({"check": check, "required": False, "status": "NOT_IMPLEMENTED", "reason": "OUT_OF_MVP"})
    return rows


def member(index: int, path: str, states: Optional[List[str]] = None, valid: bool = True) -> Dict[str, Any]:
    return {
        "id": f"disk-{path.split('/')[-1]}",
        "index": index,
        "group": None,
        "device_path": path,
        "state_valid": valid,
        "raw_states": ["online"] if states is None else states,
    }


def array(name: str, level: str, members: List[Dict[str, Any]], states: Optional[List[str]] = None, age: int = 1000, valid: bool = True) -> Dict[str, Any]:
    return {
        "id": f"array:{name}",
        "incarnation": f"{name}:/dev/xi_{name}:{level}:{len(members)}",
        "collection_status": "SUCCESS",
        "observed_at": AT,
        "evidence_age_ms": age,
        "reason_codes": [],
        "details": {
            "kind": "ARRAY",
            "name": name,
            "volume_path": f"/dev/xi_{name}",
            "edition": "Classic",
            "version": "4.4.0",
            "build": "4.4.0-43861",
            "raid_level": level,
            "state_valid": valid,
            "raw_states": ["online", "initialized"] if states is None else states,
            "progress": {"init_pct": 100, "recon_pct": None, "restripe_pct": None, "sdc_pct": None},
            "members": members,
        },
    }


def filesystem(unit: str, uuid: str, mountpoint: str, data: str, log: Optional[str] = None, rt: Optional[str] = None, age: int = 1000, **over: Any) -> Dict[str, Any]:
    refs = [{"role": "DATA", "resource_id": f"array:{data}"}]
    super_options = ["rw"]
    if log:
        refs.append({"role": "LOG", "resource_id": f"array:{log}"})
        super_options.append(f"logdev=/dev/xi_{log}")
    if rt:
        refs.append({"role": "REALTIME", "resource_id": f"array:{rt}"})
        super_options.append(f"rtdev=/dev/xi_{rt}")
    details = {
        "kind": "FILESYSTEM",
        "uuid": uuid,
        "incarnation": f"{uuid}:/dev/xi_{data}",
        "mountpoint": mountpoint,
        "source_device": f"/dev/xi_{data}",
        "fs_type": "xfs",
        "mounted": True,
        "writable": True,
        "mount_options": ["rw", "noatime"],
        "super_options": super_options,
        "external_dependencies_resolved": True,
        "array_refs": refs,
        "log_mode": "EXTERNAL" if log else "INTERNAL",
    }
    details.update(over)
    return {
        "id": f"fs:{unit}",
        "incarnation": f"{uuid}:/dev/xi_{data}",
        "collection_status": "SUCCESS",
        "observed_at": AT,
        "evidence_age_ms": age,
        "reason_codes": [],
        "details": details,
    }


def export(path: str, rules: Optional[List[Dict[str, Any]]] = None, present: bool = True, age: int = 1000, source: str = "etab") -> Dict[str, Any]:
    enc = path.strip("/").replace("/", "-")
    return {
        "id": f"export:{enc}",
        "incarnation": f"export:{enc}:7",
        "collection_status": "SUCCESS",
        "observed_at": AT,
        "evidence_age_ms": age,
        "reason_codes": [],
        "details": {
            "kind": "EXPORT",
            "export_path": path,
            "present": present,
            "source": source,
            "rules": rules if rules is not None else [rule()],
        },
    }


def rule(client: str = "10.10.0.0/16", writable: Optional[bool] = True, security: Optional[List[str]] = None, options: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "client": client,
        "writable": writable,
        "security": ["sys"] if security is None else security,
        "options": ["rw", "sync", "no_subtree_check", "fsid=7"] if options is None else options,
    }


def nfs_service(running: Optional[bool] = True, protocols: Optional[List[str]] = None, age: int = 1100) -> Dict[str, Any]:
    return {
        "id": "nfs:nfs-server",
        "incarnation": "nfs:nfs-server:active",
        "collection_status": "SUCCESS",
        "observed_at": AT,
        "evidence_age_ms": age,
        "reason_codes": [],
        "details": {
            "kind": "NFS_SERVICE",
            "running": running,
            "protocols": ["NFSv3", "NFSv4.1", "NFSv4.2"] if protocols is None else protocols,
            "reason_codes": [],
            "unit_active_state": "active" if running is not None else None,
            "threads": 8 if running else (0 if running is False else None),
        },
    }


def share(share_id: str, path: str, fs_ref: str, age: int = 1000, status: str = "SUCCESS", reasons: Optional[List[str]] = None) -> Dict[str, Any]:
    enc = path.strip("/").replace("/", "-")
    return {
        "share_id": share_id,
        "incarnation": f"{share_id}:7",
        "export_path": path,
        "collection_status": status,
        "observed_at": AT,
        "evidence_age_ms": age,
        "filesystem_ref": fs_ref,
        "export_ref": f"export:{enc}",
        "service_ref": "nfs:nfs-server",
        "reason_codes": reasons or [],
    }


def base_result(generation: int = 120, epoch: str = "publisher-epoch-1", snapshot_status: str = "COMPLETE") -> Dict[str, Any]:
    data1 = array("data1", "5", [member(0, "/dev/nvme0n2"), member(1, "/dev/nvme1n2"), member(2, "/dev/nvme2n2")])
    log1 = array("log1", "10", [member(0, "/dev/nvme0n1"), member(1, "/dev/nvme1n1")])
    data2 = array("data2", "5", [member(0, "/dev/nvme3n2"), member(1, "/dev/nvme4n2"), member(2, "/dev/nvme5n2")])
    log2 = array("log2", "10", [member(0, "/dev/nvme3n1"), member(1, "/dev/nvme4n1")])
    fs1 = filesystem("mnt-data.mount", "fs-uuid-01", "/mnt/data", "data1", "log1")
    fs2 = filesystem("mnt-data2.mount", "fs-uuid-02", "/mnt/data2", "data2", "log2")
    return {
        "schema_version": "1.0",
        "controller_id": CONTROLLER,
        "server_epoch": epoch,
        "source_generation": generation,
        "generated_at": "2026-09-22T12:00:01Z",
        "snapshot_status": snapshot_status,
        "collection_period_ms": 5000,
        "capabilities": list(CAPABILITIES),
        "coverage": coverage_rows(),
        "shares": [
            share("training-a", "/mnt/data/training-a", "fs:mnt-data.mount"),
            share("training-b", "/mnt/data/training-b", "fs:mnt-data.mount"),
            share("training-c", "/mnt/data2/training-c", "fs:mnt-data2.mount"),
        ],
        "resources": [
            data1,
            log1,
            data2,
            log2,
            fs1,
            fs2,
            export("/mnt/data/training-a"),
            export("/mnt/data/training-b"),
            export("/mnt/data2/training-c"),
            nfs_service(),
        ],
    }


def envelope(result: Optional[Dict[str, Any]], errors: Optional[List[Dict[str, Any]]] = None, revision: int = 42) -> Dict[str, Any]:
    return {
        "request_id": "example-request-1",
        "correlation_id": "example-correlation-1",
        "state_revision": revision,
        "warnings": [],
        "errors": errors or [],
        "links": {"self": "/api/v1/placement/observations"},
        "result": result,
    }


def find(result: Dict[str, Any], resource_id: str) -> Dict[str, Any]:
    for r in result["resources"]:
        if r["id"] == resource_id:
            return r
    raise KeyError(resource_id)


def with_array_states(result: Dict[str, Any], name: str, states: Any, valid: bool = True) -> Dict[str, Any]:
    out = copy.deepcopy(result)
    d = find(out, f"array:{name}")["details"]
    d["raw_states"] = states
    d["state_valid"] = valid
    return out


def with_member_states(result: Dict[str, Any], name: str, index: int, states: Any, valid: bool = True) -> Dict[str, Any]:
    out = copy.deepcopy(result)
    m = find(out, f"array:{name}")["details"]["members"][index]
    m["raw_states"] = states
    m["state_valid"] = valid
    return out


def with_resource_error(result: Dict[str, Any], resource_id: str, reason: str) -> Dict[str, Any]:
    out = copy.deepcopy(result)
    r = find(out, resource_id)
    kind = r["details"]["kind"]
    r.update({"collection_status": "ERROR", "observed_at": None, "evidence_age_ms": None, "reason_codes": [reason], "details": {"kind": kind}})
    out["snapshot_status"] = "PARTIAL"
    return out
