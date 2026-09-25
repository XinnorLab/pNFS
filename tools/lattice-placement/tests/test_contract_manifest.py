# SPDX-License-Identifier: MIT
"""The connector_client.batch_drops list in the contract manifest must
name every enum ds_connector_drop reason (fork include/placement_modes.h),
including the profile-pin drops added by this change, and the bundled
copy under lattice_placement/data/ must stay byte-identical to the docs
copy (scripts/check-manifests.py enforces the latter at the file level;
this test pins the actual drop-reason content)."""

from __future__ import annotations

import json
import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DOCS_MANIFEST = os.path.join(REPO_ROOT, "docs", "placement-modes", "contract-manifest.json")
BUNDLED_MANIFEST = os.path.join(
    os.path.dirname(__file__), "..", "lattice_placement", "data", "contract-manifest.json"
)

EXPECTED_BATCH_DROPS = [
    "JSON",
    "SCHEMA",
    "CONTRACT_MAJOR",
    "REPLAY",
    "OLD_GENERATED_AT",
    "CONFIG_DIGEST",
    "TOO_LARGE",
    "PROFILE_INCONSISTENT",
    "PROFILE_LIMIT",
]


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_docs_manifest_lists_profile_drop_reasons():
    manifest = _load(DOCS_MANIFEST)
    assert manifest["connector_client"]["batch_drops"] == EXPECTED_BATCH_DROPS


def test_bundled_manifest_matches_docs_manifest_byte_for_byte():
    with open(DOCS_MANIFEST, "rb") as f:
        docs_bytes = f.read()
    with open(BUNDLED_MANIFEST, "rb") as f:
        bundled_bytes = f.read()
    assert bundled_bytes == docs_bytes
