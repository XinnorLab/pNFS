#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regenerate the golden xiNAS source fixtures under fixtures/xinas/.

Run from the connector directory:  python3 scripts/gen_fixtures.py
The files are illustrative contract examples (addresses, IDs and digests
are placeholders), not a working cluster's data.
"""

from __future__ import annotations

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import source_builder as sb  # noqa: E402

OUT = os.path.join(ROOT, "fixtures", "xinas")


def write(name: str, result):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as fh:
        json.dump(sb.envelope(result), fh, indent=2)
        fh.write("\n")


def main() -> None:
    base = sb.base_result()
    write("healthy-three-shares.json", base)

    # T-07: data1 and log1 degraded → shares A/B 250000 once, C untouched.
    degraded = sb.with_array_states(base, "data1", ["online", "initialized", "degraded"])
    degraded = sb.with_array_states(degraded, "log1", ["online", "initialized", "degraded"])
    degraded = sb.with_member_states(degraded, "data1", 2, ["offline"])
    write("degraded-data-and-log.json", degraded)

    # T-04: a fault on log1 blocks A/B; C stays eligible.
    log_fault = sb.with_array_states(base, "log1", ["online", "initialized", "reconstructing"])
    write("log-array-fault.json", log_fault)

    # T-06: broken shapes — invalid state shape, unknown word, missing member state.
    broken = sb.with_array_states(base, "data1", ["online"], valid=False)
    broken = sb.with_array_states(broken, "data2", ["online", "initialized", "wibble"])
    broken = sb.with_member_states(broken, "log2", 1, [])
    write("broken-shapes.json", broken)

    # API-07 / T-19: the RAID source failed this cycle; everything else fine.
    missing = copy.deepcopy(base)
    missing["snapshot_status"] = "PARTIAL"
    missing["resources"] = [r for r in missing["resources"] if r["details"]["kind"] != "ARRAY"]
    missing["resources"].insert(0, {
        "id": "array:unavailable", "incarnation": "-", "collection_status": "ERROR",
        "observed_at": None, "evidence_age_ms": None, "reason_codes": ["XIRAID_DAEMON_UNAVAILABLE"],
        "details": {"kind": "ARRAY"},
    })
    for fs_id in ("fs:mnt-data.mount", "fs:mnt-data2.mount"):
        fs = sb.find(missing, fs_id)
        fs["collection_status"] = "UNKNOWN"
        fs["reason_codes"] = ["DEPENDENCY_ARRAY_UNAVAILABLE"]
    for s in missing["shares"]:
        s["collection_status"] = "UNKNOWN"
        s["reason_codes"] = ["DEPENDENCY_FILESYSTEM_UNAVAILABLE"]
    write("missing-dependencies.json", missing)

    # T-08 / T-09: export access variants.
    access = copy.deepcopy(base)
    sb.find(access, "export:mnt-data-training-a")["details"]["rules"] = [sb.rule("10.10.0.0/16", writable=False, options=["ro", "sync", "fsid=7"])]
    sb.find(access, "export:mnt-data-training-b")["details"]["rules"] = [sb.rule("@trusted", options=["rw", "fsid=8"])]
    sb.find(access, "export:mnt-data2-training-c")["details"]["rules"] = [sb.rule("10.99.0.0/16")]
    write("export-access-variants.json", access)

    # T-08: prerequisites — B unmounted fs? (fs is shared) → make fs-02 read-only and nfsd stopped.
    prereq = copy.deepcopy(base)
    fs2 = sb.find(prereq, "fs:mnt-data2.mount")["details"]
    fs2["writable"] = False
    fs2["mount_options"] = ["ro", "noatime"]
    sb.find(prereq, "nfs:nfs-server")["details"]["running"] = False
    write("prerequisites-ro-and-nfsd-stopped.json", prereq)

    # T-05: mount-source mismatch on fs-01, unreadable mountinfo on fs-02.
    mounts = copy.deepcopy(base)
    fs1 = sb.find(mounts, "fs:mnt-data.mount")
    fs1["collection_status"] = "UNKNOWN"
    fs1["reason_codes"] = ["MOUNT_SOURCE_MISMATCH"]
    fs1["details"].update({"mounted": False, "writable": None, "mount_source_mismatch": "/dev/sdb1", "log_mode": "UNKNOWN", "array_refs": [{"role": "DATA", "resource_id": "array:data1"}], "super_options": []})
    fs2 = sb.find(mounts, "fs:mnt-data2.mount")
    fs2["collection_status"] = "UNKNOWN"
    fs2["reason_codes"] = ["MOUNTINFO_UNREADABLE"]
    fs2["details"].update({"mounted": None, "writable": None, "log_mode": "UNKNOWN", "super_options": []})
    for s in mounts["shares"]:
        s["collection_status"] = "UNKNOWN"
        s["reason_codes"] = ["DEPENDENCY_FILESYSTEM_UNAVAILABLE"]
    mounts["snapshot_status"] = "COMPLETE"
    write("mount-ambiguity.json", mounts)

    print(f"wrote fixtures to {OUT}")


if __name__ == "__main__":
    main()
