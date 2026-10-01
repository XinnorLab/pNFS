# SPDX-License-Identifier: MIT
"""The verdict state file (smart verdict retention design §4.4).

The runtime writes its verdict store here so a connector restart keeps the
verdicts in force: after the cycles that change the store, at most once per
``collect_interval_ms``, as a temp file in the same directory that is
fsynced, renamed over the old one, and followed by an fsync of the
directory. The file is 0600 and carries no credentials and no endpoints --
only the binding identity and the verdict.

Monotonic time does not survive a restart, so the file carries wall-clock
UTC stamps: ``observed_at`` (the observation the published record's age
counts from) and, for a critical verdict, ``critical_observed_at`` (the
observation its hold counts from; earlier than ``observed_at`` while the
recovery hold-down withholds an allow). Both are derived from the store's
monotonic times at write time, never copied from the source's record.

Reading is forgiving by design: a missing file is a clean start, an
unreadable, unparsable or wrong-version file is renamed to
``<path>.corrupt-<ts>`` and the connector starts with an empty store. It
never refuses to start over its state file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import contract

STATE_VERSION = 1
DEFAULT_STATE_PATH = "/var/lib/lattice-ds-connector/verdicts.json"
#: A file larger than this is not ours (256 bindings are well under 1 MiB).
MAX_STATE_BYTES = 4 * 1024 * 1024

_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$")


def iso(dt: datetime) -> str:
    """UTC, millisecond precision, ``Z`` suffix -- the batch's own format."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(text: Any) -> Optional[datetime]:
    """An RFC 3339 timestamp with an explicit offset, as an aware datetime;
    None for anything else (Python 3.9's ``fromisoformat`` takes no ``Z``)."""
    if not isinstance(text, str):
        return None
    m = _ISO.match(text)
    if m is None:
        return None
    frac = (m.group(2) or "").ljust(6, "0")
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        return datetime.fromisoformat("%s.%s%s" % (m.group(1), frac, tz))
    except ValueError:  # e.g. month 13
        return None


def entry_from(key, v, now_mono: float, now_wall: datetime) -> Dict[str, Any]:
    """One stored verdict as a file entry. ``key`` is a ``VerdictKey``, ``v``
    a ``StoredVerdict``; its monotonic times are dated against the pair
    (``now_mono``, ``now_wall``) taken together."""
    pa = v.published
    a = pa.assessment

    def wall_of(mono: float) -> str:
        return iso(now_wall - timedelta(seconds=now_mono - mono))

    # The observation the published record's age counts from: the fetch
    # less its evidence age (the fetch itself for a deny without evidence).
    observed_mono = pa.fetched_mono - (a.evidence_age_ms or 0) / 1000.0
    return {
        "instance": key.instance,
        "ds_id": key.ds_id,
        "binding_generation": key.binding_generation,
        "target_id": key.target_id,
        "expected_target_incarnation": key.expected_target_incarnation,
        "profile_id": v.profile_id,
        "allowed": bool(a.allowed),
        "multiplier_ppm": a.multiplier_ppm,
        "reason_codes": list(a.reason_codes),
        "capacity_domain_id": a.capacity_domain_id,
        "shared_resource_ids": list(a.shared_resource_ids),
        "datastore_id": a.datastore_id,
        "target_incarnation": a.target_incarnation,
        "observed_at": wall_of(observed_mono),
        "critical": bool(v.critical),
        # The observation the hold counts from; only a critical verdict has one.
        "critical_observed_at": wall_of(v.hold_origin_mono) if v.critical else None,
    }


def check_entry(e: Any) -> Optional[str]:
    """Why ``e`` cannot be restored, or None when it is well-formed. Only a
    record the batch schema and the CON-06 invariants accept comes back."""
    if not isinstance(e, dict):
        return "not an object"

    def is_int(x: Any) -> bool:
        return isinstance(x, int) and not isinstance(x, bool)

    def is_str(x: Any) -> bool:
        return isinstance(x, str) and bool(x)

    if not is_str(e.get("instance")) or not is_str(e.get("target_id")) or not is_str(e.get("profile_id")):
        return "instance, target_id and profile_id must be non-empty strings"
    if not is_int(e.get("ds_id")) or e["ds_id"] < 0 or not is_int(e.get("binding_generation")) or e["binding_generation"] < 1:
        return "ds_id and binding_generation must be integers"
    if e.get("expected_target_incarnation") is not None and not is_str(e.get("expected_target_incarnation")):
        return "expected_target_incarnation must be a string or null"
    allowed, critical, ppm = e.get("allowed"), e.get("critical"), e.get("multiplier_ppm")
    if not isinstance(allowed, bool) or not isinstance(critical, bool) or critical is allowed:
        return "allowed and critical must be booleans, critical exactly when not allowed"
    if not is_int(ppm) or not (1 if allowed else 0) <= ppm <= (contract.PPM_FULL if allowed else 0):
        return "multiplier_ppm out of range for the verdict"
    reasons = e.get("reason_codes")
    if (
        not isinstance(reasons, list)
        or not 1 <= len(reasons) <= contract.MAX_REASON_CODES
        or not all(isinstance(r, str) and r in contract.ALL_REASON_CODES for r in reasons)
    ):
        return "reason_codes must be 1..%d catalogue codes" % contract.MAX_REASON_CODES
    shared = e.get("shared_resource_ids")
    if not isinstance(shared, list) or len(shared) > contract.MAX_SHARED_RESOURCE_IDS or not all(is_str(s) for s in shared):
        return "shared_resource_ids must be a list of non-empty strings"
    if not is_str(e.get("datastore_id")):
        return "datastore_id must be a non-empty string"
    for name in ("capacity_domain_id", "target_incarnation"):
        value = e.get(name)
        if (allowed or value is not None) and not is_str(value):
            return "%s must be a non-empty string%s" % (name, "" if allowed else " or null")
    if parse_utc(e.get("observed_at")) is None:
        return "observed_at must be an RFC 3339 time"
    if critical and e.get("critical_observed_at") is not None and parse_utc(e.get("critical_observed_at")) is None:
        return "critical_observed_at must be an RFC 3339 time or null"
    return None


def save(path: str, entries: List[Dict[str, Any]], runtime_epoch: str, now_wall: datetime) -> None:
    """Replace the file atomically: temp file in the same directory (0600),
    fsync, rename, fsync of the directory. Raises OSError; a failed write
    leaves the previous file untouched and no temp file behind."""
    d = os.path.dirname(os.path.abspath(path))
    body = json.dumps(
        {"version": STATE_VERSION, "written_at": iso(now_wall), "runtime_epoch": runtime_epoch, "verdicts": entries},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".verdicts.", suffix=".tmp", dir=d)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(body)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load(path: str) -> Tuple[List[Any], Optional[str]]:
    """``(entries, warning)``. A missing file is ``([], None)``; a file that
    cannot be used is renamed to ``<path>.corrupt-<ts>`` and yields
    ``([], "STATE_FILE_UNREADABLE: …")``. Entries are returned unchecked
    (see :func:`check_entry`); a bad entry costs that entry only."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("larger than %d bytes" % MAX_STATE_BYTES)
        data = json.loads(raw.decode("utf-8"))
        version = data.get("version") if isinstance(data, dict) else None
        if isinstance(version, bool) or version != STATE_VERSION or not isinstance(data.get("verdicts"), list):
            raise ValueError("unsupported state file shape or version (expected version %d)" % STATE_VERSION)
        return list(data["verdicts"]), None
    except FileNotFoundError:
        return [], None
    except Exception as exc:  # noqa: BLE001 - any unusable file is a clean start, never a refusal
        moved = "%s.corrupt-%s" % (path, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
        try:
            os.replace(path, moved)
        except OSError:
            moved = None
        detail = "%s: %s" % (exc.__class__.__name__, exc)
        return [], "STATE_FILE_UNREADABLE: %s%s" % (detail, "; kept as %s" % moved if moved else "")
