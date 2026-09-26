# SPDX-License-Identifier: MIT
"""Contract constants: versions, limits, enums and the reason-code catalogue.

Everything a reader of ``contracts/connector-batch.schema.json`` may need to
interpret a batch is named here once. Reason codes are a closed set (CON-08):
a consumer that meets an unknown *core* enum fails closed, an unknown
optional diagnostic reason must not break placement.
"""

from __future__ import annotations

CONTRACT_VERSION = "1.0"
CONTRACT_MAJOR = 1

# The source schema this connector's xinas module understands (API-21).
XINAS_SOURCE_SCHEMA_VERSION = "1.0"
XINAS_SOURCE_SCHEMA_MAJOR = 1

SCOPE_NEW_ALLOCATION = "NEW_ALLOCATION"
ACCESS_SCOPE_CLUSTER_DEFAULT = "cluster-default"

QUALITY_VALID = "VALID"
QUALITY_UNKNOWN = "UNKNOWN"

SNAPSHOT_COMPLETE = "COMPLETE"
SNAPSHOT_PARTIAL = "PARTIAL"
SNAPSHOT_FAILED = "FAILED"

PPM_FULL = 1_000_000

# CON-21 limits.
MAX_DS_PER_MDS = 256
MAX_INSTANCES = 64
MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_REASON_CODES = 16
MAX_DIAGNOSTICS_BYTES = 4096
MAX_SHARED_RESOURCE_IDS = 128
MAX_COVERAGE_ROWS = 64
# The schema caps remaining_ttl_ms at 20 000 (the MVP profile's max source age).
MAX_REMAINING_TTL_MS = 20_000

# CON-12 scheduling bounds.
MAX_JITTER_MS = 500
MAX_RETRY_BACKOFF_MS = 5_000

# Coverage statuses.
COVERAGE_EVALUATED = "EVALUATED"
COVERAGE_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
COVERAGE_NOT_APPLICABLE = "NOT_APPLICABLE"
COVERAGE_ERROR = "ERROR"

# ---------------------------------------------------------------------------
# Reason-code catalogue (metrics label set and operator documentation).
# ---------------------------------------------------------------------------

#: Placement outcome codes from the xinas-mvp decision table (XMOD §5).
ARRAY_DECISION_REASONS = {
    "NORMAL": "online with initialization readiness proven; no penalty",
    "SCAN_ACTIVE": "sdc_scanning with healthy prerequisites; no penalty",
    "REDUNDANCY_DEGRADED": "degraded without any veto; degraded multiplier",
    "RESTRIPE_PENDING": "need_restripe while online; restripe multiplier",
    "RECONSTRUCTION_ACTIVE": "reconstructing; veto",
    "INITIALIZATION_ACTIVE": "initing; veto",
    "RESTRIPE_ACTIVE": "restriping; veto",
    "RECONSTRUCTION_REQUIRED": "need_recon; veto",
    "INITIALIZATION_REQUIRED": "need_init; veto",
    "INTEGRITY_ERROR": "inconsistent; veto",
    "UNRECOVERED": "unrecovered; veto",
    "READ_ONLY": "read_only; veto",
    "ARRAY_UNAVAILABLE": "offline or none; veto",
    "SOURCE_UNKNOWN": "unknown raw word or shape, or no proof of initialization where required",
}

#: Per-member codes (XMOD-09).
MEMBER_REASONS = {
    "MEMBER_OFFLINE": "a member is offline; at least the degraded penalty",
    "MEMBER_RECONSTRUCTION_ACTIVE": "a member is reconstructing; veto",
    "MEMBER_RECONSTRUCTION_REQUIRED": "a member needs reconstruction; veto",
    "MEMBER_STATE_MISSING": "a mandatory member state is missing; UNKNOWN",
    "MEMBER_STATE_INVALID": "a member state has an invalid shape or unknown word; UNKNOWN",
}

#: Filesystem, export and service prerequisites (XMOD-11..14).
PREREQUISITE_REASONS = {
    "FILESYSTEM_NOT_MOUNTED": "the filesystem is not mounted; VALID deny",
    "FILESYSTEM_READ_ONLY": "the filesystem is mounted read-only; VALID deny",
    "FILESYSTEM_MOUNT_UNKNOWN": "mountinfo unreadable or mount state unproven; UNKNOWN",
    "FILESYSTEM_SOURCE_MISMATCH": "the mountpoint is served by another device; UNKNOWN",
    "FILESYSTEM_TYPE_UNSUPPORTED": "the filesystem is not XFS; UNKNOWN",
    "DATA_ARRAY_UNRESOLVED": "no DATA array reference; UNKNOWN",
    "EXTERNAL_DEVICE_UNRESOLVED": "an external log/realtime device is not an array; UNKNOWN",
    "LOG_MODE_UNKNOWN": "internal vs external log could not be proven; UNKNOWN",
    "EXPORT_ABSENT": "no effective export for the share path; VALID deny",
    "EXPORT_ACCESS_MISSING": "a configured client network is not covered by a rule; VALID deny",
    "EXPORT_READ_ONLY": "the covering export rule is read-only; VALID deny",
    "EXPORT_SECURITY_MISMATCH": "the covering rule does not offer the expected security; VALID deny",
    "EXPORT_RULE_UNSUPPORTED": "netgroup/hostname rule needed for coverage; UNKNOWN",
    "EXPORT_RULES_CONFLICT": "conflicting rules cover the same network; UNKNOWN",
    "EXPORT_SOURCE_NOT_EFFECTIVE": "the profile demands kernel-effective rules; the source reads /etc/exports; UNKNOWN",
    "NFS_SERVICE_STOPPED": "nfsd is not running; VALID deny",
    "NFS_PROTOCOL_MISSING": "a required NFS protocol is not enabled; VALID deny",
    "NFS_SERVICE_UNKNOWN": "the service state could not be proven; UNKNOWN",
}

#: Identity, graph, freshness and runtime codes (CON §4, XMOD-02..04, 14).
RUNTIME_REASONS = {
    "SHARE_ABSENT": "the bound share is not in a COMPLETE source snapshot; VALID deny",
    "SHARE_UNRESOLVED": "the bound share is missing from a PARTIAL/FAILED snapshot; UNKNOWN",
    "SHARE_COLLECTION_ERROR": "the source reports the share as ERROR/UNKNOWN; UNKNOWN",
    "IDENTITY_MISMATCH": "controller_id differs from the binding's expectation; VALID deny",
    "INCARNATION_MISMATCH": "the share incarnation differs from the binding's expectation; VALID deny (rebind)",
    "EXPORT_PATH_MISMATCH": "the source's export path differs from the endpoint; VALID deny",
    "DS_PATH_UNDER_NESTED_SHARE": "another observed share or export lies between the bound share and the binding's ds_path; VALID deny",
    "GRAPH_UNRESOLVED": "a mandatory reference does not resolve in the snapshot; UNKNOWN",
    "DEPENDENCY_ERROR": "a referenced resource is ERROR/UNKNOWN; UNKNOWN",
    "SOURCE_STALE": "evidence older than the profile's max source age; UNKNOWN",
    "SOURCE_UNAVAILABLE": "the source could not be read; UNKNOWN",
    "SOURCE_TIMEOUT": "the source did not answer within the deadline; UNKNOWN",
    "SOURCE_AUTH_FAILED": "the source refused the credential; UNKNOWN",
    "SOURCE_SCHEMA_INVALID": "the source response failed contract validation; UNKNOWN",
    "SOURCE_NOT_READY": "the source answered 503 SOURCE_NOT_READY; UNKNOWN",
    "SOURCE_FAILED": "the source snapshot is FAILED; UNKNOWN",
    "CAPABILITY_MISSING": "a required check is not in the source's capabilities; UNKNOWN",
    "EVIDENCE_EXPIRED": "the assessment's remaining TTL reached 0; UNKNOWN",
    "RECOVERY_HOLD_DOWN": "allow is withheld until the hold-down completes; VALID deny",
    "NO_ASSESSMENT": "no assessment has been produced yet; UNKNOWN",
    "BINDING_INVALID": "the binding failed validation; UNKNOWN",
    "INVALID_MULTIPLIER": "a module returned a multiplier outside [0, 1000000] or not an integer; UNKNOWN",
    "INVALID_QUALITY": "a module returned a quality outside the enum; UNKNOWN",
    "EVIDENCE_INCOMPLETE": "a VALID allow lacked identity, domain or evidence time; UNKNOWN",
    "ZERO_MULTIPLIER": "a module allowed with multiplier 0; deny",
    "DENIED": "deny without a module-supplied reason (should not happen)",
    "SOURCE_TLS_FAILED": "TLS verification against the configured CA failed; UNKNOWN",
    "SOURCE_REPLAY": "the source answered with a generation not newer than the last accepted one; ignored",
    "COLLECT_IN_FLIGHT": "the previous collect is still running; this tick was skipped (UNKNOWN once the lease expires)",
    "GRAPH_INCONSISTENT": "a reference resolves to a record that contradicts the share (kind, path, mountpoint, device); UNKNOWN",
    "EVIDENCE_AGE_MISSING": "a mandatory SUCCESS record carries no evidence time or age; UNKNOWN",
    "FILESYSTEM_IDENTITY_MISSING": "the filesystem has no uuid or incarnation; UNKNOWN",
    "INCARNATION_UNPINNED": "the binding pins no expected_target_incarnation; UNKNOWN (see `discover`)",
    "XIRAID_VERSION_UNSUPPORTED": "the array's edition/version is outside the profile's compatibility (Classic 4.4); UNKNOWN",
    "RAID_LEVEL_UNSUPPORTED": "the array's level is not one the policy knows; UNKNOWN",
    "MODULE_ERROR": "an uncaught module error; UNKNOWN",
    "WORKER_STUCK": "the collect worker exceeded its restart budget; UNKNOWN until reload",
    "FIXTURE_HEALTHY": "fixture module: healthy (test only)",
}

ALL_REASON_CODES = {
    **ARRAY_DECISION_REASONS,
    **MEMBER_REASONS,
    **PREREQUISITE_REASONS,
    **RUNTIME_REASONS,
}

#: xiRAID Classic 4.4 raw array state vocabulary (vendor page cited in the
#: requirements: showing_raid_state.html). ``none`` is the daemon's spelling
#: for "no state" and is treated as unavailable.
XIRAID_ARRAY_WORDS = frozenset(
    {
        "online",
        "offline",
        "none",
        "degraded",
        "initialized",
        "initing",
        "need_init",
        "reconstructing",
        "need_recon",
        "restriping",
        "need_restripe",
        "sdc_scanning",
        "need_resize",
        "read_only",
        "inconsistent",
        "unrecovered",
    }
)

#: Per-member state words the module understands.
XIRAID_MEMBER_WORDS = frozenset({"online", "offline", "reconstructing", "need_recon"})

#: RAID levels that carry an initialization phase (XMOD-07): everything but 0.
LEVELS_WITHOUT_INITIALIZATION = frozenset({"0"})

#: A profile id is used verbatim as a key of the MDS pin map
#: (ds_connector_expected_profiles = id=digest,...), so it cannot carry
#: '=', ',' or whitespace. Shared with the batch schema and the MDS parser.
PROFILE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,63}$"
#: PM_PROFILES_MAX in the MDS.
MAX_PROFILES = 8
#: PM_DIGEST_MAX in the MDS is 128 bytes including the NUL terminator, so
#: 127 is the longest digest string the MDS will accept.
MAX_DIGEST_LEN = 127
