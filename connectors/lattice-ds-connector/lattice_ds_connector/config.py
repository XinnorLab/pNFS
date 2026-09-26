# SPDX-License-Identifier: MIT
"""Configuration model, validation and digests (CON-19, LAT-22, XMOD-15).

The file is JSON (Appendix A of the requirements package). ``load_config``
returns an immutable :class:`Config` or raises :class:`ConfigError` with every
typed issue found, so an operator fixes a file in one round. Secrets are
referenced by file and never read for the digest.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import contract
from .paths import normalize, path_contains

CONFIG_VERSION = "1.0"
SUPPORTED_MODULES = ("xinas", "fixture")
SUPPORTED_PROFILE_VERSION = "1"

#: The checks the xinas module can evaluate (XMOD-04/05). A profile that
#: *requires* anything else is refused (CON-17, T-30).
XINAS_SUPPORTED_CHECKS = (
    "raid.array_states",
    "raid.member_states",
    "topology.data_log_realtime",
    "identity",
    "filesystem.mounted_rw",
    "export.effective_access",
    "nfs.service",
    "source.freshness",
)
XINAS_NOT_IMPLEMENTED_CHECKS = ("filesystem.integrity", "network.path", "network.performance")


@dataclass(frozen=True)
class ConfigIssue:
    code: str
    path: str
    message: str
    severity: str = "error"  # error | warning

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message, "severity": self.severity}


class ConfigError(Exception):
    def __init__(self, issues: Sequence[ConfigIssue]):
        self.issues = [i for i in issues if i.severity == "error"]
        super().__init__(
            "invalid configuration: "
            + "; ".join(f"{i.path}: {i.code} ({i.message})" for i in self.issues)
        )


@dataclass(frozen=True)
class RuntimeConfig:
    socket_path: str = "/run/lattice-ds-connector/connector.sock"
    socket_group: Optional[str] = None
    collect_interval_ms: int = 5000
    collect_deadline_ms: int = 2000
    max_ds: int = contract.MAX_DS_PER_MDS
    max_instances: int = contract.MAX_INSTANCES
    max_batch_bytes: int = contract.MAX_BATCH_BYTES
    #: Restarts of a stuck worker per hour before the instance stays UNKNOWN.
    worker_restart_limit: int = 3


@dataclass(frozen=True)
class Profile:
    id: str
    version: str
    source_max_age_ms: int = 20000
    recovery_hold_down_ms: int = 10000
    recovery_distinct_cycles: int = 2
    degraded_multiplier_ppm: int = 250000
    need_restripe_multiplier_ppm: int = 250000
    scan_multiplier_ppm: int = contract.PPM_FULL
    required_checks: Tuple[str, ...] = XINAS_SUPPORTED_CHECKS
    #: NFS protocols the share's nfsd must enable (flex-files data servers speak v3).
    required_protocols: Tuple[str, ...] = ("NFSv3",)
    #: ``etab`` (default) demands kernel-effective rules — what xiNAS
    #: publishes since its audit remediation (``details.source: etab``);
    #: ``exports`` also accepts a labelled ``/etc/exports`` source (older
    #: sources, lab fixtures).
    export_source_required: str = "etab"
    digest: str = ""

    def placement_fields(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "source_max_age_ms": self.source_max_age_ms,
            "recovery_hold_down_ms": self.recovery_hold_down_ms,
            "recovery_distinct_cycles": self.recovery_distinct_cycles,
            "degraded_multiplier_ppm": self.degraded_multiplier_ppm,
            "need_restripe_multiplier_ppm": self.need_restripe_multiplier_ppm,
            "scan_multiplier_ppm": self.scan_multiplier_ppm,
            "required_checks": list(self.required_checks),
            "required_protocols": list(self.required_protocols),
            "export_source_required": self.export_source_required,
        }


@dataclass(frozen=True)
class Endpoint:
    server: str
    export_path: str
    protocol: str = "NFS"
    transport: str = "TCP"
    port: int = 2049
    #: The path the MDS registers as ds[N] when the DS lives below the share
    #: (endpoint ds_path design §5); None = the share path itself.
    ds_path: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "server": self.server,
            "export_path": self.export_path,
            "protocol": self.protocol,
            "transport": self.transport,
            "port": self.port,
        }
        if self.ds_path is not None:
            out["ds_path"] = self.ds_path
        return out


@dataclass(frozen=True)
class Binding:
    ds_id: int
    binding_generation: int
    target_id: str
    endpoint: Endpoint
    expected_target_incarnation: Optional[str] = None
    expected_client_networks: Tuple[str, ...] = ()
    expected_security: Tuple[str, ...] = ("sys",)
    #: Generic datastore identity for non-xinas modules; xinas uses the
    #: instance's expected_controller_id.
    datastore_id: Optional[str] = None
    #: Intentional capacity alias of another binding on the same target (CON-18).
    alias: bool = False


@dataclass(frozen=True)
class SourceConfig:
    url: str
    tls_ca_file: Optional[str] = None
    bearer_token_file: Optional[str] = None
    #: Plain http is refused unless this is set explicitly (a lab without an
    #: HTTPS ingress); validate-config warns loudly.
    allow_insecure_http: bool = False


@dataclass(frozen=True)
class Instance:
    id: str
    module: str
    bindings: Tuple[Binding, ...]
    profile_id: Optional[str] = None
    expected_controller_id: Optional[str] = None
    source: Optional[SourceConfig] = None
    fixture_file: Optional[str] = None


@dataclass(frozen=True)
class Config:
    config_version: str
    test_mode: bool
    runtime: RuntimeConfig
    profiles: Dict[str, Profile]
    instances: Tuple[Instance, ...]
    digest: str
    warnings: Tuple[ConfigIssue, ...] = field(default_factory=tuple)

    def profile_for(self, instance: Instance) -> Optional[Profile]:
        if instance.profile_id is None:
            return None
        return self.profiles.get(instance.profile_id)


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.issues: List[ConfigIssue] = []

    def error(self, code: str, path: str, message: str) -> None:
        self.issues.append(ConfigIssue(code, path, message, "error"))

    def warn(self, code: str, path: str, message: str) -> None:
        self.issues.append(ConfigIssue(code, path, message, "warning"))

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


def _int(c: _Collector, obj: Dict[str, Any], key: str, path: str, default: Optional[int], lo: int, hi: int) -> Optional[int]:
    if key not in obj:
        if default is None:
            c.error("MISSING_FIELD", f"{path}.{key}", "required")
            return None
        return default
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, int):
        c.error("TYPE", f"{path}.{key}", "must be an integer")
        return None
    if v < lo or v > hi:
        c.error("RANGE", f"{path}.{key}", f"must be within [{lo}, {hi}]")
        return None
    return v


def _str(c: _Collector, obj: Dict[str, Any], key: str, path: str, required: bool = True) -> Optional[str]:
    if key not in obj:
        if required:
            c.error("MISSING_FIELD", f"{path}.{key}", "required")
        return None
    v = obj[key]
    if not isinstance(v, str) or not v:
        c.error("TYPE", f"{path}.{key}", "must be a non-empty string")
        return None
    return v


def _bool(c: _Collector, obj: Dict[str, Any], key: str, path: str, default: bool) -> bool:
    if key not in obj:
        return default
    v = obj[key]
    if not isinstance(v, bool):
        c.error("TYPE", f"{path}.{key}", "must be a boolean")
        return default
    return v


def _str_list(c: _Collector, obj: Dict[str, Any], key: str, path: str, default: Optional[Sequence[str]]) -> Tuple[str, ...]:
    if key not in obj:
        return tuple(default or ())
    v = obj[key]
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        c.error("TYPE", f"{path}.{key}", "must be a list of non-empty strings")
        return tuple(default or ())
    return tuple(v)


def _check_secret_file(c: _Collector, path_value: str, path: str) -> None:
    try:
        st = os.stat(path_value)
    except OSError as exc:
        c.error("SECRET_FILE", path, f"cannot stat: {exc.strerror}")
        return
    if not stat.S_ISREG(st.st_mode):
        c.error("SECRET_FILE", path, "must be a regular file")
        return
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        c.error("SECRET_PERMISSIONS", path, "must not be group/other accessible (expected mode 0600)")


def _parse_runtime(c: _Collector, raw: Dict[str, Any]) -> RuntimeConfig:
    path = "runtime"
    d = RuntimeConfig()
    interval = _int(c, raw, "collect_interval_ms", path, d.collect_interval_ms, 1000, 60_000) or d.collect_interval_ms
    deadline = _int(c, raw, "collect_deadline_ms", path, d.collect_deadline_ms, 100, 60_000) or d.collect_deadline_ms
    if deadline >= interval:
        c.error("TIMEOUT_RELATION", f"{path}.collect_deadline_ms", "must be smaller than collect_interval_ms")
    socket_path = raw.get("socket_path", d.socket_path)
    if not isinstance(socket_path, str) or not socket_path.startswith("/"):
        c.error("TYPE", f"{path}.socket_path", "must be an absolute path")
        socket_path = d.socket_path
    group = raw.get("socket_group")
    if group is not None and (not isinstance(group, str) or not group):
        c.error("TYPE", f"{path}.socket_group", "must be a group name")
        group = None
    return RuntimeConfig(
        socket_path=socket_path,
        socket_group=group,
        collect_interval_ms=interval,
        collect_deadline_ms=deadline,
        max_ds=_int(c, raw, "max_ds", path, d.max_ds, 1, contract.MAX_DS_PER_MDS) or d.max_ds,
        max_instances=_int(c, raw, "max_instances", path, d.max_instances, 1, contract.MAX_INSTANCES) or d.max_instances,
        max_batch_bytes=_int(c, raw, "max_batch_bytes", path, d.max_batch_bytes, 4096, contract.MAX_BATCH_BYTES) or d.max_batch_bytes,
        worker_restart_limit=_int(c, raw, "worker_restart_limit", path, d.worker_restart_limit, 0, 100) or d.worker_restart_limit,
    )


def _parse_profile(c: _Collector, raw: Dict[str, Any], idx: int, runtime: RuntimeConfig) -> Optional[Profile]:
    path = f"profiles[{idx}]"
    pid = _str(c, raw, "id", path)
    if pid is not None and not re.fullmatch(contract.PROFILE_ID_PATTERN, pid):
        c.error("PROFILE_ID_INVALID", f"{path}.id", "must match [A-Za-z0-9._-]{1,63} (it is a key of the MDS pin map)")
    version = _str(c, raw, "version", path)
    if version is not None and version != SUPPORTED_PROFILE_VERSION:
        c.error("UNSUPPORTED_PROFILE", f"{path}.version", f"only version {SUPPORTED_PROFILE_VERSION} is supported")
    d = Profile(id="", version="")
    max_age = _int(c, raw, "source_max_age_ms", path, d.source_max_age_ms, 1000, contract.MAX_REMAINING_TTL_MS)
    if max_age is not None and max_age < 2 * runtime.collect_interval_ms:
        c.error("TIMEOUT_RELATION", f"{path}.source_max_age_ms", "must be at least twice runtime.collect_interval_ms")
    hold = _int(c, raw, "recovery_hold_down_ms", path, d.recovery_hold_down_ms, 0, 600_000)
    cycles = _int(c, raw, "recovery_distinct_cycles", path, d.recovery_distinct_cycles, 1, 10)
    degraded = _int(c, raw, "degraded_multiplier_ppm", path, d.degraded_multiplier_ppm, 0, contract.PPM_FULL)
    restripe = _int(c, raw, "need_restripe_multiplier_ppm", path, d.need_restripe_multiplier_ppm, 0, contract.PPM_FULL)
    scan = _int(c, raw, "scan_multiplier_ppm", path, d.scan_multiplier_ppm, 0, contract.PPM_FULL)
    checks = _str_list(c, raw, "required_checks", path, XINAS_SUPPORTED_CHECKS)
    for chk in checks:
        if chk not in XINAS_SUPPORTED_CHECKS:
            code = "CHECK_NOT_IMPLEMENTED" if chk in XINAS_NOT_IMPLEMENTED_CHECKS else "CHECK_UNKNOWN"
            c.error(code, f"{path}.required_checks", f"required check '{chk}' is not evaluated by the xinas module")
    protocols = _str_list(c, raw, "required_protocols", path, d.required_protocols)
    export_source = raw.get("export_source_required", d.export_source_required)
    if export_source not in ("exports", "etab"):
        c.error("ENUM", f"{path}.export_source_required", "must be 'exports' or 'etab'")
        export_source = d.export_source_required
    if pid is None or version is None or None in (max_age, hold, cycles, degraded, restripe, scan):
        return None
    profile = Profile(
        id=pid,
        version=version,
        source_max_age_ms=int(max_age),
        recovery_hold_down_ms=int(hold),
        recovery_distinct_cycles=int(cycles),
        degraded_multiplier_ppm=int(degraded),
        need_restripe_multiplier_ppm=int(restripe),
        scan_multiplier_ppm=int(scan),
        required_checks=checks,
        required_protocols=protocols,
        export_source_required=export_source,
    )
    return Profile(**{**profile.__dict__, "digest": sha256_digest(profile.placement_fields())})


def _parse_endpoint(c: _Collector, raw: Any, path: str) -> Optional[Endpoint]:
    if not isinstance(raw, dict):
        c.error("TYPE", path, "must be an object")
        return None
    server = _str(c, raw, "server", path)
    export_path = _str(c, raw, "export_path", path)
    if export_path is not None and not export_path.startswith("/"):
        c.error("FORMAT", f"{path}.export_path", "must be an absolute path")
    protocol = raw.get("protocol", "NFS")
    if protocol != "NFS":
        c.error("ENUM", f"{path}.protocol", "must be NFS")
    transport = raw.get("transport", "TCP")
    if transport not in ("TCP", "RDMA"):
        c.error("ENUM", f"{path}.transport", "must be TCP or RDMA")
    port = _int(c, raw, "port", path, 2049, 1, 65535)
    ds_path = _str(c, raw, "ds_path", path, required=False)
    if ds_path is not None and not ds_path.startswith("/"):
        c.error("FORMAT", f"{path}.ds_path", "must be an absolute path")
        ds_path = None
    if server is None or export_path is None or port is None:
        return None
    export_n = normalize(export_path)
    if ds_path is not None:
        ds_path = normalize(ds_path)
        if export_n == "/" and ds_path != "/":
            c.error("ROOT_EXPORT_PARENT", f"{path}.ds_path",
                    "'/' cannot be the share of a DS path; bind the share that contains it")
        elif not path_contains(export_n, ds_path):
            c.error("DS_PATH_OUTSIDE_EXPORT", f"{path}.ds_path",
                    f"{ds_path} is neither {export_n} nor under it")
    return Endpoint(server=server, export_path=export_n, protocol="NFS",
                    transport=transport, port=port, ds_path=ds_path)


def _parse_binding(c: _Collector, raw: Any, path: str, module: str) -> Optional[Binding]:
    if not isinstance(raw, dict):
        c.error("TYPE", path, "must be an object")
        return None
    ds_id = _int(c, raw, "ds_id", path, None, 0, 4294967295)
    gen = _int(c, raw, "binding_generation", path, None, 1, 2**62)
    target = _str(c, raw, "target_id", path)
    endpoint = _parse_endpoint(c, raw.get("endpoint"), f"{path}.endpoint")
    incarnation = _str(c, raw, "expected_target_incarnation", path, required=False)
    if module == "xinas" and incarnation is None:
        # Audit C-05: an unpinned binding proves nothing; pin it from
        # `lattice-ds-connector discover` and rebind on change (XMOD-14).
        c.error("INCARNATION_REQUIRED", f"{path}.expected_target_incarnation", "pin the share incarnation reported by the source (lattice-ds-connector discover)")
    networks = _str_list(c, raw, "expected_client_networks", path, ())
    for i, n in enumerate(networks):
        try:
            ipaddress.ip_network(n, strict=False)
        except ValueError:
            c.error("FORMAT", f"{path}.expected_client_networks[{i}]", "must be an IP address or CIDR")
    if module == "xinas" and not networks:
        c.error("CLIENT_NETWORKS_REQUIRED", f"{path}.expected_client_networks", "the xinas profile checks export coverage against configured client networks")
    security = _str_list(c, raw, "expected_security", path, ("sys",))
    datastore_id = _str(c, raw, "datastore_id", path, required=False)
    if module != "xinas" and datastore_id is None:
        c.error("MISSING_FIELD", f"{path}.datastore_id", "generic modules need an explicit datastore_id")
    alias = _bool(c, raw, "alias", path, False)
    if ds_id is None or gen is None or target is None or endpoint is None:
        return None
    return Binding(
        ds_id=ds_id,
        binding_generation=gen,
        target_id=target,
        endpoint=endpoint,
        expected_target_incarnation=incarnation,
        expected_client_networks=networks,
        expected_security=security,
        datastore_id=datastore_id,
        alias=alias,
    )


def _parse_source(c: _Collector, raw: Any, path: str, test_mode: bool) -> Optional[SourceConfig]:
    if not isinstance(raw, dict):
        c.error("TYPE", path, "must be an object")
        return None
    url = _str(c, raw, "url", path)
    allow_http = _bool(c, raw, "allow_insecure_http", path, False)
    ca = _str(c, raw, "tls_ca_file", path, required=False)
    token = _str(c, raw, "bearer_token_file", path, required=False)
    if url is not None:
        parts = urlsplit(url)
        if parts.scheme == "https":
            if ca is None:
                c.warn("TLS_SYSTEM_CA", f"{path}.tls_ca_file", "no CA file given; the system trust store is used")
            elif not os.path.isfile(ca):
                c.error("TLS_CA_FILE", f"{path}.tls_ca_file", "file not found")
        elif parts.scheme == "http":
            if not allow_http:
                c.error("INSECURE_HTTP", f"{path}.url", "plain http requires allow_insecure_http: true (CON-20)")
            elif not test_mode:
                c.error("INSECURE_HTTP_PRODUCTION", f"{path}.url", "plain http is accepted only with test_mode: true; production sources are https (CON-20)")
            else:
                c.warn("INSECURE_HTTP", f"{path}.url", "plain http: the bearer token travels unencrypted; lab use only")
        else:
            c.error("FORMAT", f"{path}.url", "must be an https:// (or explicitly allowed http://) URL")
        if not parts.netloc:
            c.error("FORMAT", f"{path}.url", "missing host")
        if parts.username or parts.password:
            c.error("CREDENTIALS_IN_URL", f"{path}.url", "credentials must come from bearer_token_file")
    if token is None:
        c.error("MISSING_FIELD", f"{path}.bearer_token_file", "the xiNAS viewer credential is required")
    else:
        _check_secret_file(c, token, f"{path}.bearer_token_file")
    if url is None:
        return None
    return SourceConfig(url=url, tls_ca_file=ca, bearer_token_file=token, allow_insecure_http=allow_http)


def _parse_instance(c: _Collector, raw: Any, idx: int, test_mode: bool, profiles: Dict[str, Profile]) -> Optional[Instance]:
    path = f"instances[{idx}]"
    if not isinstance(raw, dict):
        c.error("TYPE", path, "must be an object")
        return None
    iid = _str(c, raw, "id", path)
    module = _str(c, raw, "module", path)
    if module is not None and module not in SUPPORTED_MODULES:
        c.error("UNKNOWN_MODULE", f"{path}.module", f"supported: {', '.join(SUPPORTED_MODULES)}")
    if module == "fixture" and not test_mode:
        c.error("FIXTURE_IN_PRODUCTION", f"{path}.module", "the fixture module is test-only (test_mode must be true)")
    bindings_raw = raw.get("bindings")
    if not isinstance(bindings_raw, list) or not bindings_raw:
        c.error("TYPE", f"{path}.bindings", "must be a non-empty list")
        bindings_raw = []
    bindings: List[Binding] = []
    for i, b in enumerate(bindings_raw):
        parsed = _parse_binding(c, b, f"{path}.bindings[{i}]", module or "")
        if parsed is not None:
            bindings.append(parsed)
    # One share bound twice in an instance is a mistake unless both say alias.
    by_target: Dict[str, List[Binding]] = {}
    for b in bindings:
        by_target.setdefault(b.target_id, []).append(b)
    for target, group in by_target.items():
        if len(group) > 1 and not all(b.alias for b in group):
            c.error("DUPLICATE_TARGET", f"{path}.bindings", f"target '{target}' is bound {len(group)} times; intentional aliases need alias: true on every binding (CON-18)")
    profile_id = _str(c, raw, "profile", path, required=False)
    controller = _str(c, raw, "expected_controller_id", path, required=False)
    source = None
    fixture_file = None
    if module == "xinas":
        if profile_id is None:
            c.error("MISSING_FIELD", f"{path}.profile", "xinas instances need a profile")
        elif profile_id not in profiles:
            c.error("UNKNOWN_PROFILE", f"{path}.profile", f"profile '{profile_id}' is not defined")
        if controller is None:
            c.error("MISSING_FIELD", f"{path}.expected_controller_id", "required for identity checks (XMOD-02)")
        source = _parse_source(c, raw.get("source"), f"{path}.source", test_mode)
    elif module == "fixture":
        fixture_file = _str(c, raw, "fixture_file", path)
        if fixture_file is not None and not os.path.isfile(fixture_file):
            c.warn("FIXTURE_FILE", f"{path}.fixture_file", "file not found now; the instance reads UNKNOWN until it appears")
    if iid is None or module is None:
        return None
    return Instance(
        id=iid,
        module=module,
        bindings=tuple(bindings),
        profile_id=profile_id,
        expected_controller_id=controller,
        source=source,
        fixture_file=fixture_file,
    )


def validate_config_dict(raw: Any) -> Tuple[Optional[Config], List[ConfigIssue]]:
    """Validate a parsed JSON document. Returns (config or None, issues)."""
    c = _Collector()
    if not isinstance(raw, dict):
        c.error("TYPE", "$", "the document must be an object")
        return None, c.issues
    version = raw.get("config_version")
    if version != CONFIG_VERSION:
        c.error("UNSUPPORTED_CONFIG_VERSION", "config_version", f"expected '{CONFIG_VERSION}'")
    test_mode = _bool(c, raw, "test_mode", "$", False)
    runtime = _parse_runtime(c, raw.get("runtime", {}) if isinstance(raw.get("runtime", {}), dict) else {})
    if not isinstance(raw.get("runtime", {}), dict):
        c.error("TYPE", "runtime", "must be an object")

    profiles: Dict[str, Profile] = {}
    raw_profiles = raw.get("profiles", [])
    if not isinstance(raw_profiles, list):
        c.error("TYPE", "profiles", "must be a list")
        raw_profiles = []
    if len(raw_profiles) > contract.MAX_PROFILES:
        c.error("LIMIT", "profiles", f"more than {contract.MAX_PROFILES} profiles (the MDS pins at most {contract.MAX_PROFILES})")
    for i, p in enumerate(raw_profiles):
        if not isinstance(p, dict):
            c.error("TYPE", f"profiles[{i}]", "must be an object")
            continue
        parsed = _parse_profile(c, p, i, runtime)
        if parsed is None:
            continue
        if parsed.id in profiles:
            c.error("DUPLICATE_PROFILE", f"profiles[{i}].id", "duplicate profile id")
        profiles[parsed.id] = parsed

    instances: List[Instance] = []
    raw_instances = raw.get("instances", [])
    if not isinstance(raw_instances, list):
        c.error("TYPE", "instances", "must be a list")
        raw_instances = []
    if len(raw_instances) > runtime.max_instances:
        c.error("LIMIT", "instances", f"more than max_instances ({runtime.max_instances})")
    seen_ids: Dict[str, int] = {}
    seen_ds: Dict[int, str] = {}
    seen_paths: Dict[Tuple[str, str], Tuple[int, bool]] = {}
    total_ds = 0
    for i, inst_raw in enumerate(raw_instances):
        inst = _parse_instance(c, inst_raw, i, test_mode, profiles)
        if inst is None:
            continue
        if inst.id in seen_ids:
            c.error("DUPLICATE_INSTANCE", f"instances[{i}].id", "duplicate instance id")
        seen_ids[inst.id] = i
        for b in inst.bindings:
            total_ds += 1
            if b.ds_id in seen_ds:
                c.error("DUPLICATE_DS", f"instances[{i}].bindings", f"ds_id {b.ds_id} is already bound by instance '{seen_ds[b.ds_id]}'")
            seen_ds[b.ds_id] = inst.id
            key = (b.endpoint.server, b.endpoint.ds_path or b.endpoint.export_path)
            if key in seen_paths:
                prior_ds_id, prior_alias = seen_paths[key]
                # Intentional capacity aliases (CON-18, DUPLICATE_TARGET) share
                # a target and endpoint on purpose; only flag anything else.
                if not (b.alias and prior_alias):
                    c.error("DUPLICATE_DS_PATH", f"instances[{i}].bindings",
                            f"{key[0]}:{key[1]} is already bound as ds_id {prior_ds_id}; "
                            "a DS below a share needs its own ds_path")
            seen_paths[key] = (b.ds_id, b.alias)
        instances.append(inst)
    if total_ds > runtime.max_ds:
        c.error("LIMIT", "instances", f"{total_ds} bindings exceed max_ds ({runtime.max_ds})")

    if c.has_errors:
        return None, c.issues
    digest_source = {k: v for k, v in raw.items() if k != "example_only"}
    config = Config(
        config_version=CONFIG_VERSION,
        test_mode=test_mode,
        runtime=runtime,
        profiles=profiles,
        instances=tuple(instances),
        digest=sha256_digest(digest_source),
        warnings=tuple(i for i in c.issues if i.severity == "warning"),
    )
    return config, c.issues


def load_config(path: str) -> Config:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError([ConfigIssue("FILE", path, exc.strerror or str(exc))]) from exc
    except ValueError as exc:
        raise ConfigError([ConfigIssue("JSON", path, str(exc))]) from exc
    config, issues = validate_config_dict(raw)
    if config is None:
        raise ConfigError(issues)
    return config
