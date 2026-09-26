# Profile `xinas-mvp` v1

The placement policy the `xinas` module applies to one xiNAS placement
observations snapshot (schema 1.0). The profile digest
(`sha256:` over the placement-affecting fields below) is published in every
assessment; both MDS of a cluster must run the same digest (LAT-24).

## Parameters (XMOD-15)

| Field | Default | Range | Effect |
|---|---|---|---|
| `source_max_age_ms` | 20000 | 1000..20000, ≥ 2 × collect interval | oldest evidence (+ request duration) older than this → `UNKNOWN` / `SOURCE_STALE`; also the TTL base |
| `recovery_hold_down_ms` | 10000 | 0..600000 | continuous allow interval before an allow is published |
| `recovery_distinct_cycles` | 2 | 1..10 | distinct source snapshots (`server_epoch:source_generation`) that must agree |
| `degraded_multiplier_ppm` | 250000 | 0..1000000 | `degraded` array word and `offline` member |
| `need_restripe_multiplier_ppm` | 250000 | 0..1000000 | `need_restripe` array word |
| `scan_multiplier_ppm` | 1000000 | 0..1000000 | `sdc_scanning` array word |
| `required_checks` | the eight below | subset of the eight | a required check the source does not evaluate → `UNKNOWN` / `CAPABILITY_MISSING`; `network.*` / `filesystem.integrity` cannot be required (validate-config refuses, T-30) |
| `required_protocols` | `["NFSv3"]` | | missing from nfsd → `NFS_PROTOCOL_MISSING` |
| `export_source_required` | `etab` | `etab` \| `exports` | `etab` (default) accepts only kernel-effective rules (`details.source: etab`, what xiNAS publishes since 2026-09-23); `exports` also accepts a labelled `/etc/exports` source. Anything else → `UNKNOWN` / `EXPORT_SOURCE_NOT_EFFECTIVE` |

The veto words and the UNKNOWN rules are not configurable.

## Array decision table (XMOD-06..08)

Words are the xiRAID Classic 4.4 vocabulary
(<https://xinnor.io/docs/xiRAID-4.4.0/E/en/AG/1/showing_raid_state.html>),
order-independent, all evaluated. Any veto beats any allow; unknown
evidence makes the record `UNKNOWN` while a proven veto is still reported.

| Condition | Allowed | ppm | Reason |
|---|---|---|---|
| `online` + `initialized` (levels ≠ 0), other prerequisites met | yes | 1000000 | `NORMAL` |
| `initialized` / `need_resize` as additional words | — | — | no penalty |
| `sdc_scanning` with healthy prerequisites | yes | 1000000 | `SCAN_ACTIVE` |
| `degraded` without any veto | yes | 250000 | `REDUNDANCY_DEGRADED` |
| `need_restripe` while online, without any veto | yes | 250000 | `RESTRIPE_PENDING` |
| `reconstructing` | no | 0 | `RECONSTRUCTION_ACTIVE` |
| `initing` | no | 0 | `INITIALIZATION_ACTIVE` |
| `restriping` | no | 0 | `RESTRIPE_ACTIVE` |
| `need_recon` | no | 0 | `RECONSTRUCTION_REQUIRED` |
| `need_init` | no | 0 | `INITIALIZATION_REQUIRED` |
| `inconsistent` | no | 0 | `INTEGRITY_ERROR` |
| `unrecovered` | no | 0 | `UNRECOVERED` |
| `read_only` | no | 0 | `READ_ONLY` |
| `offline` / `none` / no `online` word | no | 0 | `ARRAY_UNAVAILABLE` |
| unknown word, invalid shape (`state_valid: false`, empty, non-string), or `online` without `initialized` on a level that initializes | no | 0 | `SOURCE_UNKNOWN` (quality `UNKNOWN`) |

RAID 0 has no initialization phase, so `online` alone is enough for it
(XMOD-07). The level comes from the source (`raid_level`).

## Members (XMOD-09)

| Member state | Effect | Reason |
|---|---|---|
| `online` | neutral | |
| `offline` | at least the degraded penalty (250000) | `MEMBER_OFFLINE` |
| `reconstructing` | veto | `MEMBER_RECONSTRUCTION_ACTIVE` |
| `need_recon` | veto | `MEMBER_RECONSTRUCTION_REQUIRED` |
| missing / empty (an array without member records is not "all healthy") | `UNKNOWN` | `MEMBER_STATE_MISSING` |
| invalid shape / unknown word | `UNKNOWN` | `MEMBER_STATE_INVALID` |

Penalties are not summed per member and group failures are not turned into
a universal counter (a RAID 10/50/60 policy needs level-aware rules).

## Aggregation (XMOD-10)

For each share: the union of every veto over its filesystem, DATA/LOG/RT
arrays, members, export and service; otherwise the **minimum** of the
component multipliers. Data 0.25 and log 0.25 give 0.25. Two shares on the
same filesystem get the same storage penalties; export-specific problems
differ per share.

## Prerequisites (XMOD-11..14)

| Observation | Result | Reason |
|---|---|---|
| filesystem `mounted: false` | VALID deny | `FILESYSTEM_NOT_MOUNTED` |
| `writable: false` | VALID deny | `FILESYSTEM_READ_ONLY` |
| `mounted`/`writable: null` (mountinfo unreadable) | UNKNOWN | `FILESYSTEM_MOUNT_UNKNOWN` |
| `mount_source_mismatch` present | UNKNOWN (no fallback to a parent) | `FILESYSTEM_SOURCE_MISMATCH` |
| not XFS | UNKNOWN | `FILESYSTEM_TYPE_UNSUPPORTED` |
| no DATA array reference | UNKNOWN | `DATA_ARRAY_UNRESOLVED` |
| `logdev=`/`rtdev=` not an array, or `log_mode: EXTERNAL` without a LOG ref | UNKNOWN | `EXTERNAL_DEVICE_UNRESOLVED` |
| `log_mode: UNKNOWN` | UNKNOWN | `LOG_MODE_UNKNOWN` |
| `log_mode: INTERNAL` | allowed; coverage `topology.log` = `NOT_APPLICABLE` / `INTERNAL_LOG` | |
| export `present: false` | VALID deny | `EXPORT_ABSENT` |
| a configured client network not covered by an IP/CIDR/`*` rule | VALID deny | `EXPORT_ACCESS_MISSING` |
| covered only by a read-only rule | VALID deny | `EXPORT_READ_ONLY` |
| covering rule lacks an expected security flavor | VALID deny | `EXPORT_SECURITY_MISMATCH` |
| any netgroup / hostname / wildcard-host rule present in the export, whether or not an IP rule also covers the network (such a rule may apply to any host of the network and contradict the IP rule) | UNKNOWN | `EXPORT_RULE_UNSUPPORTED` |
| covering rules disagree on rw/ro, or `writable: null` | UNKNOWN | `EXPORT_RULES_CONFLICT` |
| nfsd `running: false` | VALID deny | `NFS_SERVICE_STOPPED` |
| nfsd `running: null` | UNKNOWN | `NFS_SERVICE_UNKNOWN` |
| required protocol missing | VALID deny | `NFS_PROTOCOL_MISSING` |
| share missing from a `COMPLETE` snapshot | VALID deny | `SHARE_ABSENT` |
| share missing from a `PARTIAL` snapshot | UNKNOWN | `SHARE_UNRESOLVED` |
| share or referenced resource not `SUCCESS` | UNKNOWN | `SHARE_COLLECTION_ERROR` / `DEPENDENCY_ERROR` |
| reference does not resolve | UNKNOWN | `GRAPH_UNRESOLVED` |
| reference resolves to a record that contradicts the share: wrong `kind`, export `export_path` ≠ share path, filesystem `mountpoint` not containing the path or not the most specific managed filesystem that does, DATA array `volume_path` ≠ filesystem `source_device`, LOG / REALTIME array ≠ `logdev=` / `rtdev=`, `INTERNAL` log with a `logdev=` or LOG ref | UNKNOWN | `GRAPH_INCONSISTENT` (`diagnostics.graph_inconsistent` lists every contradiction) |
| a `SUCCESS` share, filesystem, export, service or array without `observed_at` or `evidence_age_ms` | UNKNOWN | `EVIDENCE_AGE_MISSING` |
| filesystem without `uuid` or `incarnation` | UNKNOWN (no capacity domain) | `FILESYSTEM_IDENTITY_MISSING` |
| array `edition` ≠ `Classic` or `version` outside `4.4.x` (Opus, 4.3, a future major, `unknown`) | UNKNOWN | `XIRAID_VERSION_UNSUPPORTED` |
| array `raid_level` not one of 0 / 1 / 5 / 6 / 7 / 10 / 50 / 60 / 70 / n+m (either spelling) | UNKNOWN | `RAID_LEVEL_UNSUPPORTED` |
| binding without `expected_target_incarnation` (xinas bindings must pin one; `validate-config` rejects the file, the policy guards the runtime path) | UNKNOWN | `INCARNATION_UNPINNED` |
| `controller_id` ≠ binding | VALID deny | `IDENTITY_MISMATCH` |
| share `incarnation` ≠ `expected_target_incarnation` | VALID deny (rebind) | `INCARNATION_MISMATCH` |
| share `export_path` ≠ endpoint | VALID deny | `EXPORT_PATH_MISMATCH` |
| another share or export lies between the bound share and `ds_path` | VALID deny | `DS_PATH_UNDER_NESTED_SHARE` |
| snapshot `FAILED` | UNKNOWN | `SOURCE_FAILED` |
| required check not in `capabilities`/`EVALUATED` | UNKNOWN | `CAPABILITY_MISSING` |
| oldest evidence + request duration > `source_max_age_ms` | UNKNOWN | `SOURCE_STALE` |

## Coverage rows

The eight required checks are `EVALUATED`; `topology.log` and
`topology.realtime` are optional rows (`EVALUATED`, or `NOT_APPLICABLE`
with `INTERNAL_LOG` / `NO_REALTIME_DEVICE`); `filesystem.integrity`,
`network.path` and `network.performance` are `NOT_IMPLEMENTED` /
`OUT_OF_MVP` (XMOD-05).

## Identity and resources

`datastore_id` = the instance's `expected_controller_id`; `target_id` = the
bound share id; `target_incarnation` = the share's `incarnation`
(`<share id>:<fsid>` in the xiNAS prototype); `capacity_domain_id` =
`<controller>/<filesystem uuid>/<filesystem incarnation>` — two shares on
one filesystem share one domain (LAT-10); `shared_resource_ids` =
`<controller>/array:<name>` for the DATA/LOG/RT arrays.

## Runtime reason codes

`RECOVERY_HOLD_DOWN` (VALID deny during the hold-down), `EVIDENCE_EXPIRED`
(TTL reached 0), `NO_ASSESSMENT` (start-up), `SOURCE_UNAVAILABLE` /
`SOURCE_TIMEOUT` / `COLLECT_IN_FLIGHT` (transport, or a helper that is
still inside the previous collect+evaluate; the last VALID decisions are
retained until their own expiry), `SOURCE_REPLAY` (a snapshot whose
`source_generation` is not newer than the last accepted one within the
same `server_epoch` is ignored: the sequence does not advance and the
hold-down sees no new cycle), `SOURCE_AUTH_FAILED` / `SOURCE_TLS_FAILED` /
`SOURCE_SCHEMA_INVALID` / `SOURCE_NOT_READY` / `SOURCE_STALE` /
`SNAPSHOT_TOO_LARGE` / `MODULE_ERROR` (revoke immediately, alert),
`WORKER_STUCK` (restart budget exhausted), `INVALID_MULTIPLIER` /
`INVALID_QUALITY` / `EVIDENCE_INCOMPLETE` / `ZERO_MULTIPLIER` (a module
result that violated CON-06 was downgraded). The full catalogue with one
line each is `lattice_ds_connector/contract.py` (`ALL_REASON_CODES`) and
is the bounded label set for `connector_poll_errors_total`.
