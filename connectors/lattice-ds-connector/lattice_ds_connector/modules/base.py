# SPDX-License-Identifier: MIT
"""The module interface (CON-02, CON-03) and the normalized assessment.

A module never has the authority to format, mount, rebuild or change an
export: it reads a source, evaluates, and returns typed results. ``evaluate``
is deterministic for the same source batch and profile; the hysteresis
(hold-down) state is the runtime's and is applied after ``evaluate``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .. import contract
from ..config import Binding, ConfigIssue, Instance, Profile


class CollectionError(Exception):
    """A typed collection failure.

    ``retryable`` marks transport-style failures whose last VALID decision may
    be retained until its original expiry (CON-12); everything else revokes
    allow immediately.
    """

    def __init__(self, code: str, message: str, retryable: bool = True, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


@dataclass(frozen=True)
class SourceBatch:
    """One collection: the module-specific payload plus timing facts."""

    payload: Any
    #: ``time.monotonic()`` when the response had been fully received.
    fetched_mono: float
    #: Whole measured request duration (CON-10): counts against freshness.
    request_duration_ms: int
    source_epoch: str
    source_generation: int
    snapshot_status: str

    @property
    def cycle_key(self) -> str:
        """Identity of the source snapshot; a repeated snapshot is one sample (CON-16)."""
        return f"{self.source_epoch}:{self.source_generation}"


@dataclass
class Assessment:
    """The deterministic per-DS result of ``evaluate`` (before TTL and hold-down)."""

    ds_id: int
    binding_generation: int
    datastore_id: str
    target_id: str
    target_incarnation: Optional[str]
    quality: str
    allowed: bool
    multiplier_ppm: int
    reason_codes: List[str]
    observed_at: Optional[str]
    #: Age at ``fetched_mono`` — the source's own age plus the request duration.
    evidence_age_ms: Optional[int]
    capacity_domain_id: Optional[str]
    shared_resource_ids: List[str] = field(default_factory=list)
    coverage: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> "Assessment":
        """Enforce the CON-06 invariants so a buggy module can never allow.

        UNKNOWN ⇒ allowed false / ppm 0; allowed false ⇒ ppm 0 and at least one
        reason; an invalid multiplier (not an int in [0, 1e6]) or a VALID allow
        without identity, domain and evidence time is downgraded to UNKNOWN.
        """
        a = Assessment(**self.__dict__)
        a.reason_codes = list(a.reason_codes)
        ppm = a.multiplier_ppm
        if isinstance(ppm, bool) or not isinstance(ppm, int) or ppm < 0 or ppm > contract.PPM_FULL:
            a.quality = contract.QUALITY_UNKNOWN
            a.allowed = False
            a.multiplier_ppm = 0
            _add(a.reason_codes, "INVALID_MULTIPLIER")
        if a.quality not in (contract.QUALITY_VALID, contract.QUALITY_UNKNOWN):
            a.quality = contract.QUALITY_UNKNOWN
            _add(a.reason_codes, "INVALID_QUALITY")
        if a.quality == contract.QUALITY_UNKNOWN:
            a.allowed = False
            a.multiplier_ppm = 0
        if a.allowed and (
            not a.target_incarnation
            or not a.observed_at
            or a.evidence_age_ms is None
            or not a.capacity_domain_id
        ):
            a.quality = contract.QUALITY_UNKNOWN
            a.allowed = False
            a.multiplier_ppm = 0
            _add(a.reason_codes, "EVIDENCE_INCOMPLETE")
        if a.allowed and a.multiplier_ppm == 0:
            a.allowed = False
            _add(a.reason_codes, "ZERO_MULTIPLIER")
        if not a.allowed:
            a.multiplier_ppm = 0
            if not a.reason_codes:
                a.reason_codes.append("DENIED")
        if a.evidence_age_ms is not None and a.evidence_age_ms < 0:
            a.evidence_age_ms = 0
        a.reason_codes = a.reason_codes[: contract.MAX_REASON_CODES]
        return a


def _add(codes: List[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def unknown_assessment(binding: Binding, datastore_id: str, code: str, diagnostics: Optional[Dict[str, Any]] = None) -> Assessment:
    """The record every binding gets until proven otherwise."""
    return Assessment(
        ds_id=binding.ds_id,
        binding_generation=binding.binding_generation,
        datastore_id=datastore_id,
        target_id=binding.target_id,
        target_incarnation=None,
        quality=contract.QUALITY_UNKNOWN,
        allowed=False,
        multiplier_ppm=0,
        reason_codes=[code],
        observed_at=None,
        evidence_age_ms=None,
        capacity_domain_id=None,
        shared_resource_ids=[],
        coverage=[],
        diagnostics=dict(diagnostics or {}),
    )


class Module(abc.ABC):
    """One registered module type (``xinas``, ``fixture``)."""

    name: str = ""

    @abc.abstractmethod
    def describe(self) -> Dict[str, Any]:
        """module / version / supported source versions / capabilities."""

    @abc.abstractmethod
    def validate(self, instance: Instance, profile: Optional[Profile]) -> List[ConfigIssue]:
        """Typed configuration errors for this instance."""

    @abc.abstractmethod
    def collect(self, deadline_s: float) -> SourceBatch:
        """Read the source within ``deadline_s`` seconds or raise CollectionError."""

    @abc.abstractmethod
    def evaluate(self, batch: SourceBatch, profile: Optional[Profile], bindings: Sequence[Binding]) -> List[Assessment]:
        """Deterministic per-binding assessments for ``batch``."""

    def close(self) -> None:
        """Bounded shutdown; the default has nothing to release."""
