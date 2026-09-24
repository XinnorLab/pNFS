# SPDX-License-Identifier: MIT
"""The contract manifest (`docs/placement-modes/contract-manifest.json`) is
the single source of key names, defaults, ranges and error codes.  A copy
is bundled under `lattice_placement/data/`; `scripts/check-manifests.py`
keeps the two identical."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "contract-manifest.json")

MODE_LEGACY = "legacy"


@dataclass(frozen=True)
class KeySpec:
    key: str
    type: Optional[str]
    default: Any
    range: Optional[Tuple[int, int]]
    values: Optional[List[str]]
    applies_to: List[str]
    max_len: Optional[int]
    note: str

    @property
    def prefix(self) -> Optional[str]:
        """`ds_capacity_domain.<ds_id>` -> `ds_capacity_domain.`"""
        i = self.key.find("<")
        return self.key[:i] if i > 0 else None


class Manifest:
    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        self.version = str(raw.get("version", "?"))
        self.modes: List[str] = list(raw["modes"])
        self.legacy_policy_values: List[str] = list(raw["legacy_policy_values"])
        self.validation_errors: List[str] = list(raw["validation_errors"])
        self.profiles_with_placement_policy: List[str] = list(raw.get("profiles_with_placement_policy", []))
        self.cli: Dict[str, Any] = dict(raw.get("cli", {}))
        self.weight: Dict[str, Any] = dict(raw.get("weight", {}))
        self.config_show_keys: List[str] = list(raw.get("config_show_keys", []))
        self.keys: Dict[str, KeySpec] = {}
        self.prefixed: Dict[str, KeySpec] = {}
        for k in raw["keys"]:
            rng = k.get("range")
            spec = KeySpec(
                key=k["key"],
                type=k.get("type"),
                default=k.get("default"),
                range=(int(rng[0]), int(rng[1])) if rng else None,
                values=list(k["values"]) if k.get("values") else None,
                applies_to=list(k.get("applies_to", [])),
                max_len=k.get("max_len"),
                note=k.get("note", ""),
            )
            if spec.prefix:
                self.prefixed[spec.prefix] = spec
            else:
                self.keys[spec.key] = spec

    def spec_for(self, key: str) -> Optional[KeySpec]:
        if key in self.keys:
            return self.keys[key]
        for prefix, spec in self.prefixed.items():
            if key.startswith(prefix) and len(key) > len(prefix):
                return spec
        return None

    def applies(self, spec: KeySpec, mode: str) -> bool:
        return "all" in spec.applies_to or mode in spec.applies_to

    @property
    def legacy_keys(self) -> List[str]:
        return list(self.cli.get("legacy_keys", ["placement_policy", "placement_policy_enabled",
                                                  "placement_capacity_weighting", "ds_weight.<id>"]))

    @property
    def managed_block(self) -> Tuple[str, str]:
        return (self.cli.get("managed_block_begin", "# lattice-placement managed block"),
                self.cli.get("managed_block_end", "# end lattice-placement managed block"))


def load(path: Optional[str] = None) -> Manifest:
    with open(path or DATA_FILE, "r", encoding="utf-8") as fh:
        return Manifest(json.load(fh))
