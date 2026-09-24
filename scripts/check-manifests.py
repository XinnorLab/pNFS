#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Cross-repo consistency of the placement-modes manifests.

Checks (each prints one line per finding, exit 1 when any fails):
  (a) every patch listed in mds/manifest.json exists with the recorded sha256
  (b) the helper's bundled contract manifest equals docs/placement-modes/contract-manifest.json
  (c) --fork <checkout at fork_sha>: `git format-patch` reproduces the patch files byte for byte
  (d) --fork: the defaults/limits in the contract manifest equal the macros in include/placement_modes.h,
      and the kernel id is declared by the WRR module on both sides
  (e) --fork: profiles_with_placement_policy equals the non-default names in src/common/config.c
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MDS_MANIFEST = os.path.join(HERE, "mds", "manifest.json")
CONTRACT = os.path.join(HERE, "docs", "placement-modes", "contract-manifest.json")
BUNDLED = os.path.join(HERE, "tools", "lattice-placement", "lattice_placement", "data", "contract-manifest.json")
WRR_DIR = os.path.join(HERE, "modules", "wrr")

MACRO_TO_KEY = {
    "PM_DEFAULT_CAP_MAX_AGE_MS": ("placement_capacity_max_age_ms", "default"),
    "PM_DEFAULT_CONN_POLL_MS": ("ds_connector_poll_ms", "default"),
    "PM_DEFAULT_CONN_DEADLINE_MS": ("ds_connector_request_deadline_ms", "default"),
    "PM_DEFAULT_CONN_MAX_DS": ("ds_connector_max_ds", "default"),
    "PM_DEFAULT_CONN_CONTRACT_MAJOR": ("ds_connector_expected_contract_major", "default"),
    "PM_DEFAULT_CONN_ACCESS_SCOPE": ("ds_connector_access_scope", "default"),
    "PM_DEFAULT_CONN_SOCKET": ("ds_connector_socket", "default"),
}


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def macros(text: str) -> dict:
    out = {}
    for m in re.finditer(r'^#define\s+(PM_[A-Z0-9_]+)\s+(.+?)\s*(?:/\*.*)?$', text, re.M):
        val = m.group(2).strip()
        out[m.group(1)] = val
    return out


def macro_number(val: str):
    m = re.match(r'^(0x[0-9a-fA-F]+|\d+)[uUlL]*$', val)
    if not m:
        return None
    return int(m.group(1), 0)


def macro_string(val: str):
    m = re.match(r'^"(.*)"$', val)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fork", default=None, help="a checkout of XinnorLab/pnfs-lattice at mds/manifest.json fork_sha")
    args = ap.parse_args()
    findings = []

    with open(MDS_MANIFEST) as fh:
        mds = json.load(fh)
    with open(CONTRACT) as fh:
        contract = json.load(fh)
    patches_dir = os.path.join(os.path.dirname(MDS_MANIFEST), mds["patches_dir"])

    # (a)
    for p in mds["patches"]:
        path = os.path.join(patches_dir, p["file"])
        if not os.path.exists(path):
            findings.append("(a) missing patch %s" % p["file"])
        elif sha256_file(path) != p["sha256"]:
            findings.append("(a) sha256 differs for %s" % p["file"])
    listed = {p["file"] for p in mds["patches"]}
    for name in sorted(os.listdir(patches_dir)):
        if name.endswith(".patch") and name not in listed:
            findings.append("(a) patch on disk not in the manifest: %s" % name)

    # (b)
    if sha256_file(CONTRACT) != sha256_file(BUNDLED):
        findings.append("(b) the bundled contract manifest differs from docs/placement-modes/contract-manifest.json "
                        "(cp docs/placement-modes/contract-manifest.json tools/lattice-placement/lattice_placement/data/)")

    if args.fork:
        fork = os.path.abspath(args.fork)
        head = subprocess.run(["git", "-C", fork, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        if head != mds["fork_sha"]:
            findings.append("(c) the fork checkout is at %s, the manifest records %s" % (head[:12], mds["fork_sha"][:12]))
        # (c) reproduce the patches
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "-C", fork, "format-patch", "-q", "-o", tmp, "--zero-commit", "--no-signature",
                            "%s..%s" % (mds["upstream_sha"], head)], check=True)
            produced = sorted(f for f in os.listdir(tmp) if f.endswith(".patch"))
            if produced != sorted(listed):
                findings.append("(c) patch set differs: produced %d, listed %d" % (len(produced), len(listed)))
            for name in produced:
                if name in listed and sha256_file(os.path.join(tmp, name)) != sha256_file(os.path.join(patches_dir, name)):
                    findings.append("(c) %s is not what git format-patch produces at %s" % (name, head[:12]))
        # (d) defaults and limits
        with open(os.path.join(fork, "include", "placement_modes.h")) as fh:
            macs = macros(fh.read())
        keys = {k["key"]: k for k in contract["keys"]}
        for macro, (key, attr) in MACRO_TO_KEY.items():
            if macro not in macs:
                findings.append("(d) %s not found in placement_modes.h" % macro)
                continue
            want = keys[key][attr]
            got = macro_number(macs[macro])
            if got is None:
                got = macro_string(macs[macro])
            if got != want:
                findings.append("(d) %s = %r in the fork, the manifest says %s.%s = %r" % (macro, got, key, attr, want))
        dw = macro_number(macs.get("PM_DOMAIN_WEIGHT_MAX", ""))
        if dw != contract["weight"]["domain_weight_manual_range"][1]:
            findings.append("(d) PM_DOMAIN_WEIGHT_MAX = %r vs manifest %r" % (dw, contract["weight"]["domain_weight_manual_range"][1]))
        scale = macro_number(macs.get("PM_WEIGHT_SCALE", ""))
        if scale != contract["weight"]["scale"]:
            findings.append("(d) PM_WEIGHT_SCALE = %r vs manifest %r" % (scale, contract["weight"]["scale"]))
        rng = keys["placement_domain_weight.<domain>"]["range"]
        if rng != [1, contract["weight"]["domain_weight_manual_range"][1]]:
            findings.append("(d) placement_domain_weight range %r disagrees with weight.domain_weight_manual_range" % rng)
        kid = int(contract["kernel"]["id"], 16)
        for side, files in (("this repo", [os.path.join(WRR_DIR, n) for n in ("wrr.h", "wrr.c")]),
                            ("the fork", [os.path.join(fork, "include", "wrr.h"),
                                          os.path.join(fork, "src", "modules", "wrr", "wrr.c")])):
            ids = set()
            for path in files:
                if os.path.exists(path):
                    with open(path) as fh:
                        ids |= {int(x, 16) for x in re.findall(r'\b(0x[0-9a-fA-F]{8})[uUlL]*\b', fh.read())}
            if kid not in ids:
                findings.append("(d) kernel id %s not declared in %s (%s)" % (contract["kernel"]["id"], side,
                                                                               ", ".join(os.path.relpath(f, HERE) for f in files)))
        # (e) profiles
        with open(os.path.join(fork, "src", "common", "config.c")) as fh:
            cfg = fh.read()
        names = re.findall(r'^\s*\{\s*"([a-z_]+)",\s*(ALL_TUNING_BITS|0)\s*,', cfg, re.M)
        with_policy = sorted(n for n, bits in names if bits == "ALL_TUNING_BITS")
        if with_policy != sorted(contract.get("profiles_with_placement_policy", [])):
            findings.append("(e) profiles with a placement policy: fork %r, manifest %r"
                            % (with_policy, sorted(contract.get("profiles_with_placement_policy", []))))

    for f in findings:
        print("MANIFEST-CHECK: " + f)
    if not findings:
        print("MANIFEST-CHECK: ok (%d patches, fork %s%s)" % (len(listed), mds["fork_sha"][:12], ", fork verified" if args.fork else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
