# SPDX-License-Identifier: MIT
"""`mode set <mode>`: a dry-run diff by default, `--apply` rewrites ONE local
file inside the managed block with a backup, an atomic replace, an audit
line and a re-validation of the result (CLI-03).  It never restarts a
daemon and never touches the connector or xiNAS."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import socket
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .ini import IniDocument
from .manifest import MODE_LEGACY, Manifest
from .validate import validate_document

LEGACY_EXACT = ("placement_policy", "placement_policy_enabled", "placement_capacity_weighting")
LEGACY_PREFIXES = ("ds_weight.",)
MODE_PREFIXES = ("placement_", "ds_capacity_domain.", "ds_connector_")
KEEP_IN_LEGACY = ("placement_policy", "placement_policy_enabled", "placement_capacity_weighting")


class SetRefused(Exception):
    """The plan or the rewritten file is not acceptable; `errors` say why."""

    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Plan:
    mode: str
    old_mode: str
    before: str
    after: str
    diff: str
    removed_keys: List[str]
    managed: List[Tuple[str, str]]
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def as_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "old_mode": self.old_mode, "changed": self.changed, "diff": self.diff,
                "removed_keys": list(self.removed_keys), "managed": [list(p) for p in self.managed],
                "warnings": list(self.warnings), "notes": list(self.notes)}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_mode(doc: IniDocument) -> str:
    return doc.get("placement_mode") or MODE_LEGACY


def _check_extra(extra: Dict[str, str], mode: str, manifest: Manifest) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for key, value in extra.items():
        spec = manifest.spec_for(key)
        if spec is None:
            raise SetRefused(["unknown key %s: only contract-manifest keys are managed" % key])
        if key == "placement_mode":
            raise SetRefused(["placement_mode is set by the command, not by --set"])
        if key == "ds_connector_enabled":
            raise SetRefused(["ds_connector_enabled is derived from the mode; do not set it"])
        if not manifest.applies(spec, mode):
            raise SetRefused(["%s does not apply to %s (applies to: %s)" % (key, mode, ", ".join(spec.applies_to))])
        if value == "":
            raise SetRefused(["%s has an empty value" % key])
        pairs.append((key, value))
    return pairs


def plan_set(text: str, mode: str, extra: Dict[str, str], manifest: Manifest,
             legacy_policy: str = "wrr", ds_weights: Optional[Dict[int, int]] = None,
             path: str = "mds.conf") -> Plan:
    """Pure: the file after `mode set <mode>`, as a diff."""
    if mode not in manifest.modes and mode != MODE_LEGACY:
        raise SetRefused(["unknown mode %r" % mode])
    doc = IniDocument.parse(text)
    old_mode = current_mode(doc)
    begin, end = manifest.managed_block
    removed: List[str] = []
    managed: List[Tuple[str, str]] = []
    notes: List[str] = []

    if mode == MODE_LEGACY:
        if legacy_policy not in manifest.legacy_policy_values:
            raise SetRefused(["--legacy-policy %s is not one of %s" % (legacy_policy, ", ".join(manifest.legacy_policy_values))])
        if extra:
            raise SetRefused(["--set is for rr/fill/smart; legacy takes --legacy-policy and --ds-weight"])
        # the managed block first, then every mode key wherever it lives
        doc.replace_managed_block(begin, end, [])
        removed += doc.remove_keys(keys=(), prefixes=MODE_PREFIXES)
        removed += doc.remove_keys(keys=LEGACY_EXACT, prefixes=LEGACY_PREFIXES)
        managed = [("placement_policy_enabled", "true"), ("placement_policy", legacy_policy)]
        for ds_id, w in sorted((ds_weights or {}).items()):
            if ds_id < 0 or ds_id >= 256 or w <= 0:
                raise SetRefused(["--ds-weight %d=%d: id 0..255, weight > 0" % (ds_id, w)])
            managed.append(("ds_weight.%d" % ds_id, str(w)))
        doc.replace_managed_block(begin, end, managed)
    else:
        pairs = _check_extra(extra, mode, manifest)
        removed += doc.remove_keys(keys=LEGACY_EXACT, prefixes=LEGACY_PREFIXES)
        # keys the block owns must not survive outside it (last key wins in config.c)
        removed += doc.remove_keys(keys=[k for k, _ in pairs] + ["placement_mode", "ds_connector_enabled"], prefixes=())
        # keys the manifest says were removed (e.g. the single-digest pin)
        # are a config error wherever they sit; migrate them away too.
        removed_key_specs = manifest.raw.get("removed_keys", [])
        removed += doc.remove_keys(keys=[r["key"] for r in removed_key_specs], prefixes=())
        for r in removed_key_specs:
            if r["key"] in removed:
                notes.append("%s was removed; use %s instead" % (r["key"], r["replaced_by"]))
        managed = [("placement_mode", mode)] + pairs
        doc.replace_managed_block(begin, end, managed)

    after = doc.render()
    diff = "".join(difflib.unified_diff(text.splitlines(True), after.splitlines(True),
                                        fromfile=path, tofile=path + " (after mode set %s)" % mode))
    plan = Plan(mode=mode, old_mode=old_mode, before=text, after=after, diff=diff,
                removed_keys=removed, managed=managed, notes=notes)
    if old_mode == "smart" and mode != "smart":
        plan.warnings.append("leaving smart disables the health veto: a denied or UNKNOWN DS will "
                             "receive new objects again")
    if mode == "smart" and old_mode != "smart":
        plan.notes.append("smart admits no DS until the first fresh VALID assessment; run `mode verify` "
                          "after the restart")
    if mode != MODE_LEGACY:
        rep = validate_document(IniDocument.parse(after), mode, manifest, assume_set=False)
        plan.warnings += rep.warnings
        if not rep.ready:
            raise SetRefused(rep.errors)
    else:
        rep = validate_document(IniDocument.parse(after), MODE_LEGACY, manifest, assume_set=False)
        if not rep.ready:
            raise SetRefused(rep.errors)
    return plan


@dataclass
class ApplyResult:
    path: str
    backup: str
    sha_before: str
    sha_after: str
    audit_line: Dict[str, Any]


def apply_plan(path: str, plan: Plan, manifest: Manifest, audit_log: str,
               now: Optional[datetime] = None, user: Optional[str] = None,
               sudo_user: Optional[str] = None, host: Optional[str] = None) -> ApplyResult:
    """Rewrite `path` with `plan.after`: backup, atomic replace, audit,
    re-validation (the backup comes back when the result fails)."""
    now = now or datetime.now(timezone.utc)
    user = user or os.environ.get("USER") or str(os.getuid())
    sudo_user = sudo_user if sudo_user is not None else os.environ.get("SUDO_USER")
    host = host or socket.gethostname()
    with open(path, "r", encoding="utf-8") as fh:
        current = fh.read()
    if current != plan.before:
        raise SetRefused(["%s changed since the plan was made; run the command again" % path])
    stamp = now.strftime("%Y%m%d-%H%M%S")
    backup = "%s.%s.bak" % (path, stamp)
    n = 1
    while os.path.exists(backup):
        backup = "%s.%s-%d.bak" % (path, stamp, n)
        n += 1
    shutil.copy2(path, backup)
    st = os.stat(path)
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".mds.conf.", suffix=".lattice-placement", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(plan.after)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # re-validate what is on disk now; roll back on failure
    with open(path, "r", encoding="utf-8") as fh:
        written = fh.read()
    rep = validate_document(IniDocument.parse(written), plan.mode, manifest, assume_set=False)
    if not rep.ready or written != plan.after:
        shutil.copy2(backup, path)
        raise SetRefused(["the rewritten file failed validation, backup restored: " + "; ".join(rep.errors)])
    line = {
        "ts": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "user": user, "sudo_user": sudo_user, "host": host, "file": os.path.abspath(path),
        "old_mode": plan.old_mode, "new_mode": plan.mode,
        "old_sha256": sha256_text(plan.before), "new_sha256": sha256_text(plan.after),
        "backup": backup, "managed": [list(p) for p in plan.managed], "removed_keys": list(plan.removed_keys),
    }
    os.makedirs(os.path.dirname(os.path.abspath(audit_log)), mode=0o750, exist_ok=True)
    with open(audit_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return ApplyResult(path=path, backup=backup, sha_before=line["old_sha256"],
                       sha_after=line["new_sha256"], audit_line=line)
