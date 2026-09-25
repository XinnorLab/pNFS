# SPDX-License-Identifier: MIT
"""Live state of one MDS for `mode show` / `mode verify` (design section 10):
`mds-admin config show --json` is the primary source, `/metrics` the
secondary one, the desired mode comes from the config file (local or over
`ssh`) and is never inferred from the effective mode."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .ini import IniDocument
from .manifest import MODE_LEGACY
from .profiles import parse_pins

_KV = re.compile(r"(\S+?)=(\S*)")


@dataclass
class DsRow:
    ds_id: int
    domain: str = ""
    state: str = ""
    capacity_age_ms: Optional[int] = None
    avail: Optional[int] = None
    total: Optional[int] = None
    assessment_age_ms: Optional[int] = None
    quality: Optional[str] = None
    allowed: Optional[bool] = None
    ppm: Optional[int] = None
    ttl_ms: Optional[int] = None
    weight: Optional[int] = None
    reason: str = ""

    @property
    def eligible(self) -> bool:
        return self.reason == "NONE" and (self.weight or 0) > 0

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Readiness:
    mode_active: bool = False
    connector_config_valid: bool = False
    connector_reachable: bool = False
    last_batch_valid: bool = False
    coverage: str = "none"
    registered_ds: int = 0
    covered_ds: int = 0
    eligible_ds: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class MdsState:
    host: str
    ok: bool = False
    error: Optional[str] = None
    desired_mode: Optional[str] = None      # None = not read ("?")
    mode: Optional[str] = None              # the file the daemon loaded
    mode_effective: Optional[str] = None
    generation: Optional[str] = None
    kernel_id: Optional[str] = None
    build: Dict[str, int] = field(default_factory=dict)
    readiness: Optional[Readiness] = None
    config_digest: Optional[str] = None
    profiles: Optional[Dict[str, str]] = None
    last_detail: Optional[str] = None
    ds: List[DsRow] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    metrics_error: Optional[str] = None     # the scrape failed (e.g. /metrics bound to loopback)
    raw: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host, "ok": self.ok, "error": self.error,
            "desired_mode": self.desired_mode, "mode": self.mode, "mode_effective": self.mode_effective,
            "generation": self.generation, "kernel_id": self.kernel_id, "build": dict(self.build),
            "readiness": self.readiness.as_dict() if self.readiness else None,
            "config_digest": self.config_digest, "profiles": self.profiles,
            "last_detail": self.last_detail, "ds": [r.as_dict() for r in self.ds],
            "metrics": dict(self.metrics), "metrics_error": self.metrics_error,
        }


# ---------------------------------------------------------------------------
# parsers (pure)
# ---------------------------------------------------------------------------

def parse_config_show(text: str) -> Dict[str, str]:
    """`config show --json` ({"key": "value", …}) or the plain `key = value` dump."""
    text = text.strip()
    if text.startswith("{"):
        data = json.loads(text)
        return {str(k): str(v) for k, v in data.items()}
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _kv(value: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in _KV.finditer(value)}


def _int(v: Optional[str]) -> Optional[int]:
    if v is None or v == "none" or v == "-":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def parse_ds_row(ds_id: int, value: str) -> DsRow:
    kv = _kv(value)
    quality = kv.get("quality")
    allowed = kv.get("allowed")
    return DsRow(
        ds_id=ds_id,
        domain=kv.get("domain", ""),
        state=kv.get("state", ""),
        capacity_age_ms=_int(kv.get("capacity_age_ms")),
        avail=_int(kv.get("avail")),
        total=_int(kv.get("total")),
        assessment_age_ms=_int(kv.get("assessment_age_ms")),
        quality=None if quality in (None, "NONE") else quality,
        allowed=None if allowed is None else allowed == "1",
        ppm=_int(kv.get("ppm")),
        ttl_ms=_int(kv.get("ttl_ms")),
        weight=_int(kv.get("weight")),
        reason=kv.get("reason", ""),
    )


def parse_readiness(value: str) -> Readiness:
    kv = _kv(value)
    return Readiness(
        mode_active=kv.get("mode_active") == "1",
        connector_config_valid=kv.get("connector_config_valid") == "1",
        connector_reachable=kv.get("connector_reachable") == "1",
        last_batch_valid=kv.get("last_batch_valid") == "1",
        coverage=kv.get("coverage", "none"),
        registered_ds=_int(kv.get("registered_ds")) or 0,
        covered_ds=_int(kv.get("covered_ds")) or 0,
        eligible_ds=_int(kv.get("eligible_ds")) or 0,
    )


def parse_build(value: str) -> Dict[str, int]:
    return {k: int(v) for k, v in _kv(value).items() if v.isdigit()}


def parse_metrics(text: str) -> Dict[str, float]:
    """Only the placement/connector series; labelled series keep their labels."""
    out: Dict[str, float] = {}
    for line in text.splitlines():
        if not line.startswith("pnfs_mds_placement_") and not line.startswith("pnfs_mds_connector_"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return out


def state_from_show(host: str, show: Dict[str, str], metrics: Optional[Dict[str, float]] = None,
                    desired: Optional[str] = None) -> MdsState:
    st = MdsState(host=host, ok=True, raw=dict(show), desired_mode=desired)
    st.mode = show.get("placement_mode")
    st.mode_effective = show.get("placement_mode_effective")
    gen = show.get("placement_config_generation")
    st.generation = None if gen in (None, "", "-") else gen
    st.kernel_id = show.get("placement_kernel_id")
    if "placement_build" in show:
        st.build = parse_build(show["placement_build"])
    if "placement_readiness" in show:
        st.readiness = parse_readiness(show["placement_readiness"])
    for key in ("placement_connector_config_digest",):
        v = show.get(key)
        if v is not None and v != "-":
            setattr(st, key.replace("placement_connector_", ""), v)
    raw_profiles = show.get("placement_connector_profiles")
    if raw_profiles not in (None, "", "-"):
        try:
            st.profiles = parse_pins(raw_profiles)
        except ValueError:
            st.profiles = {"<unparsable>": raw_profiles}
    st.last_detail = show.get("placement_connector_last_detail")
    rows: List[DsRow] = []
    for key, value in show.items():
        if key.startswith("placement_ds."):
            try:
                ds_id = int(key[len("placement_ds."):])
            except ValueError:
                continue
            rows.append(parse_ds_row(ds_id, value))
    st.ds = sorted(rows, key=lambda r: r.ds_id)
    st.metrics = dict(metrics or {})
    return st


# ---------------------------------------------------------------------------
# readers (subprocess / urllib)
# ---------------------------------------------------------------------------

def is_local_address(host: str) -> bool:
    """True when `host` is an address of this machine: a UDP socket can be
    bound to it.  Lets `--ssh` read the local file directly instead of
    requiring root to ssh into itself."""
    import socket

    try:
        infos = socket.getaddrinfo(host, 0, 0, socket.SOCK_DGRAM)
    except OSError:
        return False
    for family, kind, proto, _canon, addr in infos:
        try:
            s = socket.socket(family, kind, proto)
        except OSError:
            continue
        try:
            s.bind(addr)
            return True
        except OSError:
            continue
        finally:
            s.close()
    return False


def read_desired_mode(path: str, ssh_target: Optional[str] = None, timeout_s: float = 10.0) -> Optional[str]:
    """The `placement_mode` the file names (legacy when absent), None when
    the file cannot be read."""
    try:
        if ssh_target and "@" in ssh_target and is_local_address(ssh_target.split("@", 1)[1]):
            ssh_target = None
        if ssh_target:
            proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh_target, "cat", path],
                                  capture_output=True, text=True, timeout=timeout_s, check=False)
            if proc.returncode != 0:
                return None
            text = proc.stdout
        else:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
    except (OSError, subprocess.SubprocessError):
        return None
    return IniDocument.parse(text).get("placement_mode") or MODE_LEGACY


def read_mds(host: str, *, mds_admin: str = "mds-admin", port: int = 50051,
             env: Optional[Dict[str, str]] = None, metrics_port: Optional[int] = 9090,
             desired: Optional[str] = None, timeout_s: float = 10.0) -> MdsState:
    import os

    cmd = [mds_admin, "config", "show", "--mds-host", host, "--mds-port", str(port), "--json"]
    full_env = dict(os.environ)
    full_env.update(env or {})
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False, env=full_env)
    except (OSError, subprocess.SubprocessError) as exc:
        return MdsState(host=host, ok=False, error="mds-admin: %s" % exc, desired_mode=desired)
    if proc.returncode != 0 or not proc.stdout.strip():
        return MdsState(host=host, ok=False, desired_mode=desired,
                        error="mds-admin config show failed (rc %d): %s" % (proc.returncode, proc.stderr.strip()[:200]))
    try:
        show = parse_config_show(proc.stdout)
    except ValueError as exc:
        return MdsState(host=host, ok=False, error="config show: unparsable output (%s)" % exc, desired_mode=desired)
    if "placement_mode" not in show:
        return MdsState(host=host, ok=False, desired_mode=desired,
                        error="config show has no placement_mode row: the daemon predates the placement modes")
    metrics: Dict[str, float] = {}
    metrics_error: Optional[str] = None
    if metrics_port:
        url = "http://%s:%d/metrics" % (host, metrics_port)
        for attempt in (1, 2):        # the MDS metrics listener resets a connection now and then
            try:
                with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                    metrics = parse_metrics(resp.read().decode("utf-8", "replace"))
                metrics_error = None
                break
            except Exception as exc:      # noqa: BLE001 -- the metrics are secondary; the row says so
                metrics_error = "%s: %s" % (url, exc)
    st = state_from_show(host, show, metrics, desired)
    st.metrics_error = metrics_error
    return st
