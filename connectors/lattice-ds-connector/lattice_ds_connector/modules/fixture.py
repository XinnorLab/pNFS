# SPDX-License-Identifier: MIT
"""The test-only ``fixture`` module (CON-02, T-02, T-35).

Reads a JSON file on every collect so a test can rewrite it between cycles
to drive transitions. It knows nothing about xiRAID: a "ZFS-shaped" fixture
yields the same generic assessments the selector consumes, which is exactly
what proves the core interface has no backend branch.

File format::

    {
      "profile": {"id": "fixture-zfs-shaped", "version": "1"},
      "snapshot_status": "COMPLETE",
      "source_epoch": "fixture-epoch-1",
      "source_generation": 7,            // omit → one per collect
      "observed_at": "2026-09-22T12:00:00Z",   // omit → now
      "evidence_age_ms": 0,
      "delay_ms": 0,                     // simulate a slow/hanging source
      "fail": {"code": "SOURCE_TIMEOUT", "retryable": true},   // simulate a failure
      "targets": {
        "dataset-a": {
          "quality": "VALID", "allowed": true, "multiplier_ppm": 1000000,
          "reason_codes": ["FIXTURE_HEALTHY"],
          "target_incarnation": "dataset-a-inc-1",
          "capacity_domain_id": "fixture-zfs-01/pool-01",
          "shared_resource_ids": ["fixture-zfs-01/pool-01"],
          "coverage": [{"check": "fixture.health", "required": true, "status": "EVALUATED"}]
        }
      }
    }
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .. import contract
from ..config import Binding, ConfigIssue, Instance, Profile, sha256_digest
from .base import Assessment, CollectionError, Module, SourceBatch, unknown_assessment

FIXTURE_PROFILE_ID = "fixture-zfs-shaped"


class FixtureModule(Module):
    name = "fixture"

    def __init__(self, instance: Instance):
        self._instance = instance
        self._path = instance.fixture_file or ""
        self._counter = 0
        self._lock = threading.Lock()

    def describe(self) -> Dict[str, Any]:
        return {
            "module": self.name,
            "version": "1",
            "supported_source_versions": ["fixture-1"],
            "capabilities": ["fixture.health"],
            "test_only": True,
        }

    def validate(self, instance: Instance, profile: Optional[Profile]) -> List[ConfigIssue]:
        issues: List[ConfigIssue] = []
        if not instance.fixture_file:
            issues.append(ConfigIssue("MISSING_FIELD", f"instances.{instance.id}.fixture_file", "required"))
        return issues

    def collect(self, deadline_s: float) -> SourceBatch:
        started = time.monotonic()
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except OSError as exc:
            raise CollectionError("SOURCE_UNAVAILABLE", f"fixture file: {exc.strerror}", retryable=True) from exc
        except ValueError as exc:
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"fixture file is not JSON: {exc}", retryable=False) from exc
        if not isinstance(doc, dict):
            raise CollectionError("SOURCE_SCHEMA_INVALID", "fixture document must be an object", retryable=False)
        delay = doc.get("delay_ms", 0)
        if isinstance(delay, (int, float)) and delay > 0:
            time.sleep(min(float(delay) / 1000.0, 3600.0))
        if time.monotonic() - started > deadline_s:
            raise CollectionError("SOURCE_TIMEOUT", "fixture delay exceeded the deadline", retryable=True)
        fail = doc.get("fail")
        if isinstance(fail, dict) and fail.get("code"):
            raise CollectionError(str(fail["code"]), "fixture-injected failure", retryable=bool(fail.get("retryable", True)))
        with self._lock:
            self._counter += 1
            counter = self._counter
        generation = doc.get("source_generation", counter)
        if isinstance(generation, bool) or not isinstance(generation, int):
            generation = counter
        status = doc.get("snapshot_status", contract.SNAPSHOT_COMPLETE)
        if status not in (contract.SNAPSHOT_COMPLETE, contract.SNAPSHOT_PARTIAL, contract.SNAPSHOT_FAILED):
            raise CollectionError("SOURCE_SCHEMA_INVALID", f"bad snapshot_status {status!r}", retryable=False)
        done = time.monotonic()
        return SourceBatch(
            payload=doc,
            fetched_mono=done,
            request_duration_ms=int((done - started) * 1000),
            source_epoch=str(doc.get("source_epoch", "fixture-epoch")),
            source_generation=generation,
            snapshot_status=status,
        )

    def evaluate(self, batch: SourceBatch, profile: Optional[Profile], bindings: Sequence[Binding]) -> List[Assessment]:
        doc: Dict[str, Any] = batch.payload
        targets = doc.get("targets")
        if not isinstance(targets, dict):
            targets = {}
        observed_at = doc.get("observed_at") or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        base_age = doc.get("evidence_age_ms", 0)
        if isinstance(base_age, bool) or not isinstance(base_age, int) or base_age < 0:
            base_age = 0
        out: List[Assessment] = []
        for b in bindings:
            datastore_id = b.datastore_id or self._instance.id
            if batch.snapshot_status == contract.SNAPSHOT_FAILED:
                out.append(unknown_assessment(b, datastore_id, "SOURCE_FAILED"))
                continue
            t = targets.get(b.target_id)
            if not isinstance(t, dict):
                code = "SHARE_ABSENT" if batch.snapshot_status == contract.SNAPSHOT_COMPLETE else "SHARE_UNRESOLVED"
                a = unknown_assessment(b, datastore_id, code)
                if code == "SHARE_ABSENT":
                    # Proven absence is a VALID deny (XMOD-14 semantics, generic form).
                    a.quality = contract.QUALITY_VALID
                    a.observed_at = observed_at
                    a.evidence_age_ms = base_age + batch.request_duration_ms
                out.append(a)
                continue
            incarnation = t.get("target_incarnation")
            reasons = [str(r) for r in t.get("reason_codes", []) if isinstance(r, str)]
            if b.expected_target_incarnation and incarnation != b.expected_target_incarnation:
                reasons = ["INCARNATION_MISMATCH"] + reasons
                allowed = False
                quality = contract.QUALITY_VALID
            else:
                allowed = bool(t.get("allowed", False))
                quality = str(t.get("quality", contract.QUALITY_UNKNOWN))
            out.append(
                Assessment(
                    ds_id=b.ds_id,
                    binding_generation=b.binding_generation,
                    datastore_id=datastore_id,
                    target_id=b.target_id,
                    target_incarnation=incarnation if isinstance(incarnation, str) else None,
                    quality=quality,
                    allowed=allowed,
                    multiplier_ppm=t.get("multiplier_ppm", 0),
                    reason_codes=reasons,
                    observed_at=observed_at,
                    evidence_age_ms=base_age + batch.request_duration_ms,
                    capacity_domain_id=t.get("capacity_domain_id") if isinstance(t.get("capacity_domain_id"), str) else None,
                    shared_resource_ids=[s for s in t.get("shared_resource_ids", []) if isinstance(s, str)],
                    coverage=[c for c in t.get("coverage", []) if isinstance(c, dict)],
                    diagnostics={"test_only": True, "source_generation": batch.source_generation},
                )
            )
        return out


def fixture_profile(doc_profile: Optional[Dict[str, Any]] = None) -> Profile:
    """The profile a fixture instance reports (its digest is over these fields)."""
    pid = FIXTURE_PROFILE_ID
    version = "1"
    if isinstance(doc_profile, dict):
        pid = str(doc_profile.get("id", pid))
        version = str(doc_profile.get("version", version))
    p = Profile(id=pid, version=version, required_checks=("fixture.health",))
    return Profile(**{**p.__dict__, "digest": sha256_digest(p.placement_fields())})
