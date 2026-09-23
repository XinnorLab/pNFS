#!/bin/bash
# export-patches.sh -- regenerate mds/patches/<base>/ from the fork branch
# and write mds/manifest.json (upstream SHA, fork SHA, patch digests, flags).
# Usage: scripts/export-patches.sh [fork-checkout]   (default ~/Documents/GitHub/pnfs-lattice)
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
FORK=${1:-$HOME/Documents/GitHub/pnfs-lattice}
BASE=6b4dcde3cd6a8b02e8a012695ff0c29e93a0caf4
BRANCH=xinnor/placement-modes
OUT="$HERE/mds/patches/${BASE:0:7}"
rm -rf "$OUT"; mkdir -p "$OUT"
git -C "$FORK" format-patch -q -o "$OUT" --zero-commit --no-signature "$BASE..$BRANCH"
FORK_SHA=$(git -C "$FORK" rev-parse "$BRANCH")
PROFILE_DIGEST=$(python3 -c "import json;d=json.load(open('$HERE/connectors/lattice-ds-connector/docs/compatibility-manifest.json'));print(d['modules']['xinas']['profiles'][0]['id']+'@'+d['modules']['xinas']['profiles'][0]['version'])")
python3 - "$OUT" "$FORK_SHA" "$PROFILE_DIGEST" "$HERE/mds/manifest.json" <<'PY'
import hashlib, json, os, sys
out, fork_sha, profile, dest = sys.argv[1:5]
patches = []
for name in sorted(os.listdir(out)):
    with open(os.path.join(out, name), 'rb') as fh:
        patches.append({"file": name, "sha256": hashlib.sha256(fh.read()).hexdigest()})
manifest = {
    "upstream": "PEAK-AIO/pnfs-lattice",
    "upstream_sha": "6b4dcde3cd6a8b02e8a012695ff0c29e93a0caf4",
    "fork": "XinnorLab/pnfs-lattice",
    "fork_branch": "xinnor/placement-modes",
    "fork_sha": fork_sha,
    "patches_dir": os.path.relpath(out, os.path.dirname(dest)),
    "patches": patches,
    "cmake_flags": ["-DENABLE_WRR=ON", "-DENABLE_DS_PREALLOC=OFF", "-DENABLE_TESTS=ON", "-DENABLE_EBPF=OFF"],
    "connector_profile": profile,
    "contract_manifest": "docs/placement-modes/contract-manifest.json",
}
with open(dest, 'w') as fh:
    json.dump(manifest, fh, indent=2); fh.write("\n")
print(f"{len(patches)} patches, fork {fork_sha[:12]}")
PY
