# SPDX-License-Identifier: MIT
"""Pure verdicts over live MDS states: the `show` rendering and the
`verify` exit rules (design section 10, CLI-01/CLI-04 step 5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .live import DsRow, MdsState
from .profiles import format_pins

EXIT_OK = 0
EXIT_DIFFER = 1
EXIT_UNREADABLE = 2


@dataclass
class Verdict:
    exit_code: int
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    per_mds: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"exit_code": self.exit_code, "ok": self.exit_code == EXIT_OK, "errors": list(self.errors),
                "warnings": list(self.warnings), "mds": list(self.per_mds)}


def _differences(states: List[MdsState]) -> List[str]:
    """Facts that must be identical across MDS."""
    errs: List[str] = []

    def spread(label: str, values: Dict[str, Optional[str]]) -> None:
        distinct = set(values.values())
        if len(distinct) > 1:
            errs.append("%s: %s" % (label, ", ".join("%s=%s" % (h, v) for h, v in values.items())))

    spread("MODE_MISMATCH", {s.host: s.mode_effective for s in states})
    spread("GENERATION_MISMATCH", {s.host: (s.generation or "")[:12] for s in states})
    spread("BUILD_MISMATCH", {s.host: " ".join("%s=%d" % kv for kv in sorted(s.build.items())) for s in states})
    smart = [s for s in states if s.mode_effective == "smart"]
    if len(smart) > 1:
        spread("CONNECTOR_CONFIG_DIGEST_MISMATCH", {s.host: s.config_digest for s in smart})
        spread("CONNECTOR_PROFILES_MISMATCH", {s.host: format_pins(s.profiles) if s.profiles else None for s in smart})
    return errs


def _has_verdict(row: DsRow) -> bool:
    """A verdict in force for this DS.  An MDS that predates the retention
    rows renders no `verdict=`; there an assessment (quality) is the verdict."""
    if row.verdict is not None:
        return row.verdict in ("fresh", "retained")
    return row.quality is not None


def _neutral_reason(row: DsRow) -> str:
    """What to tell the operator about a DS placed neutrally: the gate's
    reason when it also excludes the DS, else that it simply has no verdict."""
    return row.reason if row.reason not in ("", "NONE") else "no verdict"


def verdict(states: List[MdsState], require_full_coverage: bool = False) -> Verdict:
    v = Verdict(exit_code=EXIT_OK)
    for s in states:
        v.per_mds.append(s.as_dict())
        if not s.ok:
            v.errors.append("MDS_UNREADABLE:%s: %s" % (s.host, s.error))
    if any(not s.ok for s in states):
        v.exit_code = EXIT_UNREADABLE
        return v
    v.errors += _differences(states)
    for s in states:
        if s.desired_mode is not None and s.mode_effective is not None and s.desired_mode != s.mode_effective:
            v.errors.append("DESIRED_NE_EFFECTIVE:%s: the file says %s, the daemon runs %s (restart pending?)"
                            % (s.host, s.desired_mode, s.mode_effective))
        if s.profiles_error is not None:
            v.errors.append("CONNECTOR_PROFILES_INVALID:%s: %s" % (s.host, s.profiles_error))
        if s.mode_effective == "smart":
            r = s.readiness
            if r is None:
                v.errors.append("NO_READINESS:%s: smart without a placement_readiness row" % s.host)
                continue
            if not s.profiles_row:
                v.errors.append("CONNECTOR_PROFILES_MISSING:%s" % s.host)
            if not r.connector_config_valid:
                v.errors.append("CONNECTOR_CONFIG_INVALID:%s" % s.host)
            # A retention-aware MDS without steering still places (a DS without a verdict
            # in force is neutral), so an unreachable connector or no coverage is a
            # warning there; --require-full-coverage is the gate for operators who need
            # steering. An MDS that predates retention refuses such a DS (ENOSPC): there
            # both stay errors, as before retention.
            if not r.connector_reachable:
                msg = "CONNECTOR_UNREACHABLE:%s: %s" % (s.host, s.last_detail or "")
                (v.warnings if r.retention_aware else v.errors).append(msg)
            if r.coverage == "none":
                if r.retention_aware:
                    msg = "STEERING_OFF:%s: no data store has a verdict in force; placing neutrally" % s.host
                    (v.errors if require_full_coverage else v.warnings).append(msg)
                else:
                    v.errors.append("COVERAGE_NONE:%s: no DS has a verdict and this MDS predates verdict "
                                    "retention: it refuses every new file (%s)" % (s.host, s.last_detail or ""))
            elif r.coverage == "partial":
                neutral = ", ".join("ds %d %s" % (row.ds_id, _neutral_reason(row)) for row in s.ds if not _has_verdict(row))
                msg = "COVERAGE_PARTIAL:%s: %d of %d DS have a verdict in force (neutral: %s)" % (
                    s.host, r.covered_ds, r.registered_ds, neutral)
                (v.errors if require_full_coverage else v.warnings).append(msg)
            # On any build: every registered DS denied, zeroed or refused -> ENOSPC for every file.
            if r.registered_ds > 0 and r.eligible_ds == 0:
                v.errors.append("NO_ELIGIBLE_DS:%s: the MDS admits no DS (%d registered, 0 eligible)"
                                % (s.host, r.registered_ds))
            if require_full_coverage and r.retained_ds > 0:
                v.errors.append("COVERAGE_RETAINED:%s: %d DS held by retained verdicts "
                                "(the connector is not observing them)" % (s.host, r.retained_ds))
    if v.errors:
        v.exit_code = EXIT_DIFFER
    return v


def _fmt_age(ms: Optional[int]) -> str:
    return "-" if ms is None else ("%.1fs" % (ms / 1000.0))


def _fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    x = float(n)
    for u in units:
        if x < 1000 or u == units[-1]:
            return "%.1f%s" % (x, u) if u != "B" else "%d%s" % (n, u)
        x /= 1000.0
    return str(n)


def render_row(row: DsRow) -> str:
    parts = ["ds %-3d %-8s domain=%s cap_age=%s avail=%s/%s" % (
        row.ds_id, row.state, row.domain or "-", _fmt_age(row.capacity_age_ms),
        _fmt_bytes(row.avail), _fmt_bytes(row.total))]
    if row.quality is not None or row.assessment_age_ms is not None:
        parts.append("assess=%s allowed=%s ppm=%s ttl=%s age=%s" % (
            row.quality or "NONE", "-" if row.allowed is None else int(row.allowed),
            row.ppm if row.ppm is not None else "-", _fmt_age(row.ttl_ms), _fmt_age(row.assessment_age_ms)))
    parts.append("weight=%s reason=%s" % (row.weight if row.weight is not None else "-", row.reason or "-"))
    if row.verdict is not None:
        parts.append("verdict=%s hold_left=%s" % (row.verdict, _fmt_age(row.hold_left_ms)))
    return "  " + " ".join(parts)


def _fmt_profiles(s: MdsState) -> str:
    if s.profiles_error is not None:
        return "INVALID(%s)" % s.profiles_error
    return format_pins(s.profiles) if s.profiles else "-"


def render_show(states: List[MdsState]) -> str:
    lines: List[str] = []
    for s in states:
        if not s.ok:
            lines.append("%s: UNREADABLE: %s" % (s.host, s.error))
            continue
        lines.append("%s: desired=%s effective=%s generation=%s kernel=%s build=%s" % (
            s.host, s.desired_mode or "?", s.mode_effective or "?", (s.generation or "?")[:12],
            s.kernel_id or "?", " ".join("%s=%d" % kv for kv in sorted(s.build.items())) or "?"))
        if s.readiness is not None:
            r = s.readiness
            lines.append("  readiness: mode_active=%d connector_config_valid=%d connector_reachable=%d "
                         "last_batch_valid=%d coverage=%s registered=%d covered=%d eligible=%d "
                         "retained=%d neutral=%d" % (
                             r.mode_active, r.connector_config_valid, r.connector_reachable, r.last_batch_valid,
                             r.coverage, r.registered_ds, r.covered_ds, r.eligible_ds, r.retained_ds, r.neutral_ds))
            lines.append("  connector: config_digest=%s profiles=%s last=%s" % (
                s.config_digest or "-", _fmt_profiles(s), s.last_detail or "-"))
        for row in s.ds:
            lines.append(render_row(row))
        m = s.metrics
        if s.metrics_error:
            lines.append("  metrics: unavailable (%s)" % s.metrics_error)
        if m:
            elig = m.get("pnfs_mds_placement_eligible_ds")
            rej = {k[len("pnfs_mds_placement_rejections_total{reason=\""):-2]: int(val)
                   for k, val in m.items() if k.startswith("pnfs_mds_placement_rejections_total{") and val > 0}
            lines.append("  metrics: eligible_ds=%s rejections=%s" % (
                int(elig) if elig is not None else "-",
                " ".join("%s=%d" % kv for kv in sorted(rej.items())) or "none"))
    diffs = _differences([s for s in states if s.ok])
    for d in diffs:
        lines.append("WARN: MDS differ -- " + d)
    return "\n".join(lines)
