# SPDX-License-Identifier: MIT
"""The production ``xinas`` module: source client + policy (XMOD-01, CON-20).

``collect`` performs one bounded HTTPS GET of the xiNAS placement
observations with the viewer bearer read from a 0600 file, verifies TLS
against the configured CA, follows no redirect (a redirect would carry the
Authorization header to another host), caps the body at the source's own
16 MiB limit, and turns every failure into a typed :class:`CollectionError`.
``evaluate`` delegates to :mod:`xinas_policy`.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import time
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from .. import contract
from ..config import Binding, ConfigIssue, Instance, Profile
from .base import Assessment, CollectionError, Module, SourceBatch
from .xinas_policy import evaluate_result

MAX_SOURCE_BYTES = 16 * 1024 * 1024
SUPPORTED_SOURCE_VERSIONS = (contract.XINAS_SOURCE_SCHEMA_VERSION,)
#: xiRAID Classic versions this module's fixtures were validated against (XMOD-01).
COMPATIBLE_XIRAID = ("4.4",)


def _read_token(path: Optional[str]) -> str:
    if not path:
        raise CollectionError("SOURCE_AUTH_FAILED", "no bearer_token_file configured", retryable=False)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise CollectionError("SOURCE_AUTH_FAILED", f"cannot read the bearer token file: {exc.strerror}", retryable=False) from exc
    if not token:
        raise CollectionError("SOURCE_AUTH_FAILED", "the bearer token file is empty", retryable=False)
    return token


def fetch_observations(
    url: str,
    token: str,
    deadline_s: float,
    tls_ca_file: Optional[str] = None,
    allow_insecure_http: bool = False,
) -> Dict[str, Any]:
    """One GET; returns the parsed envelope or raises CollectionError.

    Implemented on ``http.client`` rather than ``urllib`` so redirects are
    never followed and the body can be capped while streaming.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "https":
        ctx = ssl.create_default_context(cafile=tls_ca_file) if tls_ca_file else ssl.create_default_context()
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, parts.port or 443, timeout=deadline_s, context=ctx)
    elif parts.scheme == "http" and allow_insecure_http:
        conn = http.client.HTTPConnection(host, parts.port or 80, timeout=deadline_s)
    else:
        raise CollectionError("SOURCE_UNAVAILABLE", "unsupported URL scheme", retryable=False)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request(
            "GET",
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "lattice-ds-connector/xinas",
            },
        )
        resp = conn.getresponse()
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise CollectionError("SOURCE_SCHEMA_INVALID", "response exceeds 16 MiB", retryable=False)
            chunks.append(chunk)
        body = b"".join(chunks)
        status = resp.status
    except CollectionError:
        raise
    except socket.timeout as exc:
        raise CollectionError("SOURCE_TIMEOUT", "the source did not answer within the deadline") from exc
    except ssl.SSLError as exc:
        raise CollectionError("SOURCE_TLS_FAILED", f"TLS failure: {exc.__class__.__name__}", retryable=False) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise CollectionError("SOURCE_UNAVAILABLE", f"transport failure: {exc.__class__.__name__}") from exc
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close is best effort
            pass

    if status in (301, 302, 303, 307, 308):
        raise CollectionError("SOURCE_UNAVAILABLE", "redirect refused (CON-20)", retryable=False, details={"status": status})
    if status in (401, 403):
        raise CollectionError("SOURCE_AUTH_FAILED", f"HTTP {status}", retryable=False, details={"status": status})
    if status == 429:
        raise CollectionError("SOURCE_UNAVAILABLE", "HTTP 429", retryable=True, details={"status": status})
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        if status >= 500:
            raise CollectionError("SOURCE_UNAVAILABLE", f"HTTP {status} (non-JSON body)", details={"status": status}) from exc
        raise CollectionError("SOURCE_SCHEMA_INVALID", "response is not JSON", retryable=False, details={"status": status}) from exc
    if status == 503:
        code = "SOURCE_UNAVAILABLE"
        errors = doc.get("errors") if isinstance(doc, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            src_code = errors[0].get("code")
            if src_code in ("SOURCE_NOT_READY", "SOURCE_STALE", "SOURCE_FAILED", "SNAPSHOT_TOO_LARGE"):
                code = src_code
        # The source says its own data is not valid: revoke, do not retain.
        raise CollectionError(code, f"HTTP 503 {code}", retryable=False, details={"status": status})
    if status != 200:
        raise CollectionError("SOURCE_UNAVAILABLE", f"HTTP {status}", retryable=status >= 500, details={"status": status})
    if not isinstance(doc, dict):
        raise CollectionError("SOURCE_SCHEMA_INVALID", "envelope is not an object", retryable=False)
    return doc


_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "contracts", "xinas-observations.schema.json")
_schema_lock = threading.Lock()
_schema_validator: Any = None
_schema_state = "unloaded"  # unloaded | ready | unavailable


def _load_schema_validator() -> Any:
    """The shipped source schema compiled with ``jsonschema`` when that
    package is installed (python3-jsonschema on RHEL 9); None otherwise. The
    structural checks below run regardless (audit C-02)."""
    global _schema_validator, _schema_state
    with _schema_lock:
        if _schema_state != "unloaded":
            return _schema_validator
        try:
            import jsonschema  # type: ignore

            with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
                schema = json.load(fh)
            _schema_validator = jsonschema.Draft7Validator(schema)
            _schema_state = "ready"
        except Exception:  # noqa: BLE001 - optional dependency or file
            _schema_validator = None
            _schema_state = "unavailable"
        return _schema_validator


def schema_validation_available() -> bool:
    return _load_schema_validator() is not None


_RECORD_STR = ("observed_at",)


def _check_record(rec: Any, what: str, idx: int) -> None:
    """Type checks for the fields the policy dereferences on any record."""
    if not isinstance(rec, dict):
        raise CollectionError("SOURCE_SCHEMA_INVALID", f"{what}[{idx}] is not an object", retryable=False)
    status = rec.get("collection_status")
    if status not in ("SUCCESS", "ERROR", "UNKNOWN"):
        raise CollectionError("SOURCE_SCHEMA_INVALID", f"{what}[{idx}].collection_status invalid", retryable=False)
    if not isinstance(rec.get("reason_codes"), list):
        raise CollectionError("SOURCE_SCHEMA_INVALID", f"{what}[{idx}].reason_codes missing", retryable=False)
    age = rec.get("evidence_age_ms")
    if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 0):
        raise CollectionError("SOURCE_SCHEMA_INVALID", f"{what}[{idx}].evidence_age_ms invalid", retryable=False)
    at = rec.get("observed_at")
    if at is not None and not isinstance(at, str):
        raise CollectionError("SOURCE_SCHEMA_INVALID", f"{what}[{idx}].observed_at invalid", retryable=False)
    if status == "SUCCESS" and (age is None or not at):
        raise CollectionError("SOURCE_SCHEMA_INVALID", f"{what}[{idx}]: SUCCESS without evidence time/age", retryable=False)
    if status != "SUCCESS" and not rec.get("reason_codes"):
        raise CollectionError("SOURCE_SCHEMA_INVALID", f"{what}[{idx}]: {status} without a reason", retryable=False)


def validate_envelope(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Contract checks the policy relies on; returns ``result``.

    The full JSON Schema (contracts/xinas-observations.schema.json) is
    applied when ``jsonschema`` is installed; the structural checks below
    run always, so a shape surprise becomes SOURCE_SCHEMA_INVALID, never a
    traceback or a lucky allow (audit C-02).
    """
    for key in ("request_id", "state_revision", "errors", "result"):
        if key not in doc:
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"envelope lacks '{key}'", retryable=False)
    result = doc.get("result")
    if result is None:
        errors = doc.get("errors")
        code = "SOURCE_UNAVAILABLE"
        if isinstance(errors, list) and errors and isinstance(errors[0], dict) and isinstance(errors[0].get("code"), str):
            code = errors[0]["code"]
        raise CollectionError(code, "result is null", retryable=False)
    if not isinstance(result, dict):
        raise CollectionError("SOURCE_SCHEMA_INVALID", "result is not an object", retryable=False)
    version = result.get("schema_version")
    if not isinstance(version, str) or version.split(".")[0] != str(contract.XINAS_SOURCE_SCHEMA_MAJOR):
        raise CollectionError("SOURCE_SCHEMA_INVALID", f"unsupported schema_version {version!r}", retryable=False)
    for key, typ in (
        ("controller_id", str),
        ("server_epoch", str),
        ("source_generation", int),
        ("snapshot_status", str),
        ("generated_at", str),
        ("collection_period_ms", int),
        ("capabilities", list),
        ("coverage", list),
        ("shares", list),
        ("resources", list),
    ):
        if not isinstance(result.get(key), typ) or isinstance(result.get(key), bool):
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"result.{key} has the wrong type", retryable=False)
    if result["snapshot_status"] not in (contract.SNAPSHOT_COMPLETE, contract.SNAPSHOT_PARTIAL, contract.SNAPSHOT_FAILED):
        raise CollectionError("SOURCE_SCHEMA_INVALID", "unknown snapshot_status", retryable=False)
    if len(result["shares"]) > 256:
        raise CollectionError("SNAPSHOT_TOO_LARGE", "more than 256 shares", retryable=False)
    validator = _load_schema_validator()
    if validator is not None:
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            raise CollectionError(
                "SOURCE_SCHEMA_INVALID",
                f"schema: {'/'.join(str(p) for p in first.path)}: {first.message[:120]}",
                retryable=False,
                details={"errors": len(errors)},
            )
    seen_shares: set = set()
    for i, s in enumerate(result["shares"]):
        _check_record(s, "shares", i)
        sid = s.get("share_id")
        if not isinstance(sid, str) or not sid:
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"shares[{i}].share_id missing", retryable=False)
        if sid in seen_shares:
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"duplicate share_id {sid!r}", retryable=False)
        seen_shares.add(sid)
    seen_res: set = set()
    for i, r in enumerate(result["resources"]):
        _check_record(r, "resources", i)
        rid = r.get("id")
        if not isinstance(rid, str) or not rid:
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"resources[{i}].id missing", retryable=False)
        if rid in seen_res:
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"duplicate resource id {rid!r}", retryable=False)
        seen_res.add(rid)
        if not isinstance(r.get("details"), dict) or r["details"].get("kind") not in ("ARRAY", "FILESYSTEM", "EXPORT", "NFS_SERVICE"):
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"resources[{i}].details.kind invalid", retryable=False)
    return result


class XinasModule(Module):
    name = "xinas"

    def __init__(self, instance: Instance, fetch=fetch_observations, read_token=_read_token):
        self._instance = instance
        self._fetch = fetch
        self._read_token = read_token

    def describe(self) -> Dict[str, Any]:
        return {
            "module": self.name,
            "version": "1",
            "supported_source_versions": list(SUPPORTED_SOURCE_VERSIONS),
            "compatible_xiraid": list(COMPATIBLE_XIRAID),
            "capabilities": [
                "raid.array_states",
                "raid.member_states",
                "topology.data_log_realtime",
                "identity",
                "filesystem.mounted_rw",
                "export.effective_access",
                "nfs.service",
                "source.freshness",
            ],
        }

    def validate(self, instance: Instance, profile: Optional[Profile]) -> List[ConfigIssue]:
        issues: List[ConfigIssue] = []
        base = f"instances.{instance.id}"
        if instance.source is None:
            issues.append(ConfigIssue("MISSING_FIELD", f"{base}.source", "required"))
        if profile is None:
            issues.append(ConfigIssue("MISSING_FIELD", f"{base}.profile", "required"))
        if not instance.expected_controller_id:
            issues.append(ConfigIssue("MISSING_FIELD", f"{base}.expected_controller_id", "required"))
        return issues

    def collect(self, deadline_s: float) -> SourceBatch:
        src = self._instance.source
        if src is None:
            raise CollectionError("SOURCE_UNAVAILABLE", "no source configured", retryable=False)
        token = self._read_token(src.bearer_token_file)
        started = time.monotonic()
        doc = self._fetch(src.url, token, deadline_s, src.tls_ca_file, src.allow_insecure_http)
        done = time.monotonic()
        result = validate_envelope(doc)
        return SourceBatch(
            payload=result,
            fetched_mono=done,
            request_duration_ms=int((done - started) * 1000),
            source_epoch=result["server_epoch"],
            source_generation=result["source_generation"],
            snapshot_status=result["snapshot_status"],
        )

    def evaluate(self, batch: SourceBatch, profile: Optional[Profile], bindings: Sequence[Binding]) -> List[Assessment]:
        if profile is None:
            raise CollectionError("SOURCE_SCHEMA_INVALID", "no profile", retryable=False)
        return evaluate_result(
            batch.payload,
            profile,
            bindings,
            self._instance.expected_controller_id or "",
            batch.request_duration_ms,
        )
