# SPDX-License-Identifier: MIT
"""Read-only validation of an `mds.conf` for a placement mode (CLI-02).

`validate_document` mirrors the MDS rules (`src/common/config.c`,
`src/common/placement_config.c` of the fork) with the same error codes:
PLACEMENT_MODE_CONFLICT, RANGE, DOMAIN_WEIGHT_FORBIDDEN,
MIRROR_COUNT_UNSUPPORTED.  It is pure; the connector half
(`run_preflight` + `fold_preflight`) shells out to
`lattice-ds-connector preflight --json` and folds its verdict in.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .ini import IniDocument
from .manifest import MODE_LEGACY, Manifest
from .profiles import parse_pins

MAX_DS = 256
DOMAIN_ID_MAX = 127
SUN_PATH_MAX = 108
_NUM = re.compile(r"^[0-9]+$")


@dataclass
class Report:
    ready: bool
    mode: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    effective: Dict[str, str] = field(default_factory=dict)
    connector: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "mode": self.mode,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "effective": dict(self.effective),
            "connector": self.connector,
        }


def _u64(value: str) -> Optional[int]:
    return int(value) if _NUM.match(value) else None


def _bool(value: str) -> Optional[bool]:
    v = value.lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return None


def validate_document(doc: IniDocument, mode: str, manifest: Manifest,
                      assume_set: bool = False) -> Report:
    """Validate `doc` for `mode`.

    With `assume_set` the document is read the way `mode set <mode>` would
    leave it: `placement_mode` is taken as `mode` and the legacy keys are
    reported as warnings ("set removes them") instead of a conflict.  The
    strict form (`assume_set=False`) is what `set --apply` re-validates
    and what the MDS itself would say about the file.
    """
    rep = Report(ready=True, mode=mode)
    eff = doc.effective()
    err = rep.errors
    warn = rep.warnings

    for removed in manifest.raw.get("removed_keys", []):
        if removed["key"] in eff:
            hint = removed.get("hint")
            if hint:
                err.append("LEGACY_KEY: %s was removed; use %s = %s"
                           % (removed["key"], removed["replaced_by"], hint))
            else:
                err.append("LEGACY_KEY: %s was removed; use %s"
                           % (removed["key"], removed["replaced_by"]))

    if mode not in manifest.modes and mode != MODE_LEGACY:
        err.append("RANGE: unknown placement mode %r (rr, fill, smart, legacy)" % mode)
        rep.ready = False
        return rep

    for key in ("placement_mode", "placement_policy", "ds_capacity_poll_ms"):
        if doc.count(key) > 1:
            warn.append("duplicate key %s: the last value wins (%s)" % (key, eff.get(key)))

    file_mode = eff.get("placement_mode")
    if file_mode is not None and file_mode not in manifest.modes:
        err.append("RANGE: placement_mode = %s is not one of %s" % (file_mode, ", ".join(manifest.modes)))
    if assume_set:
        file_mode = None if mode == MODE_LEGACY else mode
    elif mode == MODE_LEGACY and file_mode is not None:
        err.append("PLACEMENT_MODE_CONFLICT: the file sets placement_mode = %s; legacy means no placement_mode key" % file_mode)
    elif mode != MODE_LEGACY and file_mode != mode:
        err.append("PLACEMENT_MODE_CONFLICT: the file sets placement_mode = %s, not %s"
                   % (file_mode if file_mode is not None else "<absent>", mode))

    legacy_present = [k for k in ("placement_policy", "placement_policy_enabled",
                                  "placement_capacity_weighting") if k in eff]
    legacy_present += ["ds_weight.%s" % i for i in doc.prefixed("ds_weight.")]
    conn_enabled_raw = eff.get("ds_connector_enabled")
    conn_enabled = _bool(conn_enabled_raw) if conn_enabled_raw is not None else None
    if conn_enabled_raw is not None and conn_enabled is None:
        err.append("RANGE: ds_connector_enabled = %s is not a boolean" % conn_enabled_raw)

    # ---- legacy ---------------------------------------------------------
    if mode == MODE_LEGACY:
        pol = eff.get("placement_policy")
        if pol is not None and pol not in manifest.legacy_policy_values:
            err.append("RANGE: placement_policy = %s is not one of %s" % (pol, ", ".join(manifest.legacy_policy_values)))
        for ds, w in doc.prefixed("ds_weight.").items():
            if not _NUM.match(ds) or int(ds) >= MAX_DS:
                err.append("RANGE: ds_weight.%s: the DS id must be a number below %d" % (ds, MAX_DS))
            elif _u64(w) is None or int(w) == 0:
                err.append("RANGE: ds_weight.%s = %s must be a positive number" % (ds, w))
        if conn_enabled:
            err.append("PLACEMENT_MODE_CONFLICT: ds_connector_enabled = true needs placement_mode = smart")
        for k in ("placement_capacity_max_age_ms", "placement_min_free_bytes", "placement_stripe_shrink",
                  "placement_allow_manual_base_weights"):
            if k in eff:
                warn.append("%s is ignored without placement_mode" % k)
        if doc.prefixed("ds_capacity_domain."):
            warn.append("ds_capacity_domain.<id> is ignored without placement_mode")
        if doc.prefixed("placement_domain_weight."):
            err.append("DOMAIN_WEIGHT_FORBIDDEN: placement_domain_weight.<domain> needs placement_mode = smart")
        rep.effective = {"placement_mode": "legacy", "placement_policy": pol or "rr",
                         "placement_policy_enabled": eff.get("placement_policy_enabled", "false")}
        rep.ready = not err
        return rep

    # ---- a mode -----------------------------------------------------------
    if legacy_present:
        if assume_set:
            warn.append("legacy keys present (%s): `mode set %s` removes them" % (", ".join(legacy_present), mode))
        else:
            err.append("PLACEMENT_MODE_CONFLICT: placement_policy, placement_policy_enabled, "
                       "placement_capacity_weighting and ds_weight.<id> cannot be combined with "
                       "placement_mode (remove the legacy keys: %s)" % ", ".join(legacy_present))
    profile = eff.get("workload_profile")
    if profile is not None and profile != "default":
        if profile in manifest.profiles_with_placement_policy:
            err.append("PLACEMENT_MODE_CONFLICT: workload_profile = %s sets a placement policy; "
                       "use the default profile or drop placement_mode" % profile)
        else:
            warn.append("workload_profile = %s is not a shipped profile; the MDS ignores unknown profiles" % profile)
    if mode == "fill" and doc.prefixed("ds_weight.") and not assume_set:
        err.append("PLACEMENT_MODE_CONFLICT: ds_weight.<id> is not allowed in fill (weights come from the fill level)")
    if mode == "smart" and conn_enabled is False:
        err.append("PLACEMENT_MODE_CONFLICT: smart needs the connector; ds_connector_enabled = false contradicts it")
    if mode != "smart" and conn_enabled is True:
        err.append("PLACEMENT_MODE_CONFLICT: ds_connector_enabled = true is only valid with placement_mode = smart")

    # numbers and ranges from the manifest
    def num(key: str, default: Optional[int]) -> Optional[int]:
        raw = eff.get(key)
        if raw is None:
            return default
        v = _u64(raw)
        if v is None:
            err.append("RANGE: %s = %s is not a number" % (key, raw))
            return None
        spec = manifest.spec_for(key)
        if spec is not None and spec.range is not None and not (spec.range[0] <= v <= spec.range[1]):
            err.append("RANGE: %s = %d is outside %d..%d" % (key, v, spec.range[0], spec.range[1]))
            return None
        return v

    poll = num("ds_capacity_poll_ms", int(manifest.keys["ds_capacity_poll_ms"].default))
    max_age = num("placement_capacity_max_age_ms", int(manifest.keys["placement_capacity_max_age_ms"].default))
    min_free = num("placement_min_free_bytes", 0)
    if mode in ("fill", "smart"):
        if poll == 0:
            err.append("RANGE: ds_capacity_poll_ms = 0 disables the probe; fill/smart need it")
        if poll is not None and max_age is not None and poll != 0 and max_age <= poll:
            err.append("RANGE: placement_capacity_max_age_ms (%d) must exceed ds_capacity_poll_ms (%d)" % (max_age, poll))
        if min_free is not None and min_free >= (1 << 50):
            warn.append("placement_min_free_bytes = %d is above 1 PiB: check the unit (bytes)" % min_free)
    shrink = eff.get("placement_stripe_shrink", "allow")
    if shrink not in ("allow", "strict"):
        err.append("RANGE: placement_stripe_shrink = %s is not allow|strict" % shrink)
    for ds, dom in doc.prefixed("ds_capacity_domain.").items():
        if not _NUM.match(ds) or int(ds) >= MAX_DS:
            err.append("RANGE: ds_capacity_domain.%s: the DS id must be a number below %d" % (ds, MAX_DS))
        elif dom == "":
            err.append("RANGE: ds_capacity_domain.%s is empty" % ds)
        elif len(dom.encode("utf-8")) > DOMAIN_ID_MAX:
            err.append("RANGE: ds_capacity_domain.%s is longer than %d bytes" % (ds, DOMAIN_ID_MAX))
    allow_manual_raw = eff.get("placement_allow_manual_base_weights")
    allow_manual = _bool(allow_manual_raw) if allow_manual_raw is not None else False
    if allow_manual_raw is not None and allow_manual is None:
        err.append("RANGE: placement_allow_manual_base_weights = %s is not a boolean" % allow_manual_raw)
        allow_manual = False
    weights = doc.prefixed("placement_domain_weight.")
    if weights:
        if mode != "smart":
            err.append("DOMAIN_WEIGHT_FORBIDDEN: placement_domain_weight.<domain> needs placement_mode = smart")
        elif not allow_manual:
            err.append("DOMAIN_WEIGHT_FORBIDDEN: placement_domain_weight.<domain> needs placement_allow_manual_base_weights = true")
        lo, hi = manifest.prefixed["placement_domain_weight."].range or (1, 10000)
        for dom, w in weights.items():
            v = _u64(w)
            if v is None or not (lo <= v <= hi):
                err.append("RANGE: placement_domain_weight.%s = %s is outside %d..%d" % (dom, w, lo, hi))
    mirrors = num("default_mirror_count", 1)
    if mode == "smart" and mirrors is not None and mirrors > 1:
        err.append("MIRROR_COUNT_UNSUPPORTED: smart needs default_mirror_count = 1 (got %d)" % mirrors)

    effective: Dict[str, str] = {
        "placement_mode": mode,
        "ds_capacity_poll_ms": str(poll) if poll is not None else "?",
        "placement_capacity_max_age_ms": str(max_age) if max_age is not None else "?",
        "placement_min_free_bytes": str(min_free) if min_free is not None else "?",
        "placement_stripe_shrink": shrink,
    }
    for ds, dom in sorted(doc.prefixed("ds_capacity_domain.").items(), key=lambda kv: kv[0]):
        effective["ds_capacity_domain.%s" % ds] = dom

    if mode == "smart":
        socket = eff.get("ds_connector_socket", str(manifest.keys["ds_connector_socket"].default))
        if not socket.startswith("/"):
            err.append("RANGE: ds_connector_socket must be an absolute path")
        elif len(socket.encode("utf-8")) >= SUN_PATH_MAX:
            err.append("RANGE: ds_connector_socket must be shorter than %d bytes (sun_path)" % SUN_PATH_MAX)
        cpoll = num("ds_connector_poll_ms", int(manifest.keys["ds_connector_poll_ms"].default))
        deadline = num("ds_connector_request_deadline_ms", int(manifest.keys["ds_connector_request_deadline_ms"].default))
        if cpoll is not None and deadline is not None and deadline > cpoll:
            err.append("RANGE: ds_connector_request_deadline_ms (%d) must not exceed ds_connector_poll_ms (%d)" % (deadline, cpoll))
        major = num("ds_connector_expected_contract_major", int(manifest.keys["ds_connector_expected_contract_major"].default))
        if major == 0:
            err.append("RANGE: ds_connector_expected_contract_major must be positive")
        max_ds = num("ds_connector_max_ds", int(manifest.keys["ds_connector_max_ds"].default))
        scope = eff.get("ds_connector_access_scope", str(manifest.keys["ds_connector_access_scope"].default))
        if scope == "":
            err.append("RANGE: ds_connector_access_scope is empty")
        if "ds_connector_expected_config_digest" in eff and eff["ds_connector_expected_config_digest"] == "":
            err.append("RANGE: ds_connector_expected_config_digest is empty (remove the key to leave it unpinned)")
        if "ds_connector_expected_profiles" in eff:
            try:
                parse_pins(eff["ds_connector_expected_profiles"])
            except ValueError as exc:
                err.append("RANGE: ds_connector_expected_profiles: %s (remove the key to leave profiles unpinned)" % exc)
        if "ds_connector_expected_config_digest" not in eff:
            warn.append("ds_connector_expected_config_digest is not pinned: `mode verify` prints the digest "
                        "every MDS sees; pin it after the first switch")
        effective.update({
            "ds_connector_socket": socket,
            "ds_connector_poll_ms": str(cpoll) if cpoll is not None else "?",
            "ds_connector_request_deadline_ms": str(deadline) if deadline is not None else "?",
            "ds_connector_expected_contract_major": str(major) if major is not None else "?",
            "ds_connector_max_ds": str(max_ds) if max_ds is not None else "?",
            "ds_connector_access_scope": scope,
            "placement_allow_manual_base_weights": "true" if allow_manual else "false",
        })
        for dom, w in sorted(weights.items()):
            effective["placement_domain_weight.%s" % dom] = w

    rep.effective = effective
    rep.ready = not err
    return rep


def run_preflight(connector_cli: str, socket: Optional[str], expect_ds: Sequence[int],
                  timeout_s: float = 10.0, expect_profiles: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """`lattice-ds-connector preflight --json [--socket S] [--expect-ds …] [--expect-profiles …]`;
    None when the command cannot run or prints no JSON."""
    cmd = [connector_cli, "preflight", "--json"]
    if socket:
        cmd += ["--socket", socket]
    if expect_ds:
        cmd += ["--expect-ds", ",".join(str(d) for d in expect_ds)]
    if expect_profiles:
        cmd += ["--expect-profiles", expect_profiles]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def fold_preflight(report: Report, preflight: Optional[Dict[str, Any]]) -> Report:
    """smart only: the connector's verdict joins the report."""
    if report.mode != "smart":
        return report
    report.connector = preflight
    if preflight is None:
        report.errors.append("CONNECTOR:UNAVAILABLE (lattice-ds-connector preflight did not answer)")
    elif not preflight.get("ready"):
        for reason in preflight.get("reasons", []) or ["NOT_READY"]:
            report.errors.append("CONNECTOR:%s" % reason)
    report.ready = not report.errors
    return report
