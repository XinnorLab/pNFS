# Lattice placement modes (`rr` / `fill` / `smart`) — design

**Status:** design for review, 2026-09-23. Implements the requirements in
`lattice_placement_modes_mvp_requirements.md` (MODE-01…10, CLI-01…05,
acceptance §7) on top of the connector MVP requirements (LAT-01…25).
**Baseline:** PEAK-AIO/pnfs-lattice `main` @ `6b4dcde` (verified in code, see
§2); XinnorLab/pNFS `main` @ `6ccd87e` (connector after the 2026-09-23 audit,
WRR kernel `modules/wrr/wrr.c`).
**Where the MDS code lives:** the fork `XinnorLab/pnfs-lattice`, branch
`xinnor/placement-modes`, cut from `6b4dcde` (created 2026-09-23). This repo
(XinnorLab/pNFS) keeps the connector, the CLI helper, the contract manifest,
the exported patch series and the stand scripts.

## 1. What the operator gets

One key, `placement_mode = rr | fill | smart`, selects how a **new** backing
object picks its data server. Nothing else about a file changes: stripe /
mirror geometry, stripe unit, inline policy and device info are the same in
all three modes (MODE-03).

| Mode | Candidate set | Weight | Connector |
|---|---|---|---|
| `rr` | registered DS that are `DS_ONLINE`, not admin-excluded, and pass the native NFS/transport/io-limit filters | none — cyclic order over the candidate list, one position per DS (MODE-07) | not required, not consulted |
| `fill` | the `rr` set **and** a fresh, valid capacity observation for the DS's capacity domain with `available > placement_min_free_bytes` | `domain_weight / N`, `domain_weight = max(1, floor(100 × available / total))` | not required, not consulted |
| `smart` | the `fill` set **and** a fresh `VALID` / `allowed` / `multiplier_ppm > 0` connector assessment | `domain_weight / N × multiplier_ppm / 1 000 000` | required; UNKNOWN, stale or missing = not a candidate |

`fill` and `smart` are **weighted random** selection (the roulette kernel),
not a strict rotation; every operator-facing text says "weighted
distribution of new files". `capacity` stays a legacy `placement_policy`
value (strict max) and is not one of the three modes. Switching modes never
touches existing layouts, never migrates data and never recalls anything
(MODE-10). Leaving `smart` for `rr`/`fill` re-enables DS the connector
denies; the CLI says so before it writes.

## 2. Verified baseline facts (upstream `6b4dcde`)

Each shaped the design; all were read in the source, not inferred.

| # | Fact | Consequence |
|---|---|---|
| B1 | `placement_select_ex2` has a single-DS fast path (`ds_count == 1`) and delegates `PLACEMENT_RR` and unknown policies to `placement_select2`; both skip the weight path entirely | the candidate gate must run *before* any selector and every selector must consume the gated list |
| B2 | `fill_online_with_free` maps `weight == 0` to "unset" and a zero free-byte value to `1`; `ds_capacity_derive_auto_weight` floors at `1` | a full or unknown DS keeps a positive weight today; the gate excludes it before weights exist |
| B3 | `mds_wrr_weighted_pick` returns `0` on an all-zero vector and the caller walks forward to the next free slot; the `>64` DS path uses `taken_heap` without masking | an all-denied set can still place; the kernel needs an explicit "no candidate" result and the same masking on both paths |
| B4 | `probe_one` records `total`/`used` and (when `proportional`) `auto_weight`; nothing records *when* the last successful `statvfs` happened; a failed probe keeps the previous values silently; the cluster reload restamps peer observations as "now" | fill/smart need their own observation record with a monotonic timestamp and `f_fsid`; peer-reloaded values are not fresh evidence |
| B5 | four call sites create backing objects: LAYOUTGET new-file path (`compound_layout.c` ≈1976–2032, dispatcher-aware), a second LAYOUTGET path (≈2217, legacy `placement_select`), `promote_inline_to_ds` (`compound_data_io.c` ≈1868, legacy), and the prealloc pop path (`ds_prealloc` — the community stub still selects a DS, SRC-06) | one admission function called from all four; the legacy `placement_select*` entry points are removed from those sites |
| B6 | `placement_policy_enabled` also gates the default stripe/mirror geometry in the LAYOUTGET and promotion paths | an explicit mode always takes the dispatcher branch, so `rr` keeps geometry (MODE-03) |
| B7 | `workload_profile` presets (`config.c` `g_profiles`) may set `placement_policy`; explicit keys override profile values; the parser is `key = value`, `#`/`;` comments, `[section]` lines skipped, last key wins | conflict detection must look at the profile's placement fields as well as explicit keys; the CLI can edit the file line-by-line |
| B8 | `mds-admin` talks to the daemon over the cluster transport (`--mds-port`, default 9800; the lab daemons listen on `grpc_port` 50051) and already has `config show` | the live source for `show` is `config show` on the right port; no new RPC in the MVP |
| B9 | no JSON parser in the tree | the connector client vendors `jsmn` (MIT, single header) |
| B10 | `include/wrr.h` is the only kernel contract; the community stub returns `0` for both kernels | the build must prove the real kernel is linked (`mds_wrr_kernel_id()`), not just that `ENABLE_WRR` was passed |

## 3. Architecture

```text
mds.conf ──► config.c: placement_mode + thresholds + domain map
                │  validation (conflicts, ranges, build flags) → startup failure
                ▼
        struct placement_cfg (effective mode, generation = sha256 of the managed keys)
                │
   ┌────────────┼──────────────────────────────┐
   │            │                               │
ds_cache   ds_capacity.c                  ds_connector.c (smart only)
(DS_ONLINE, (statvfs → capacity observation:  background client: UDS poll 1 s,
 admin state) avail/total/f_fsid/mono_ts)   deadline 500 ms, schema+epoch check,
   │            │                               immutable assessment cache + TTL
   └────────────┴──────────────┬────────────────┘
                               ▼
              placement_admit()  ← the ONE entry for new backing objects
              1. candidate gate (mode-specific, no bypass)
              2. weights (fill/smart) or cyclic order (rr)
              3. kernel: rr walk | weighted pick over positive weights
              4. shrink/strict, distinct DS per layout, reasons + metrics
                               │
        LAYOUTGET create · LAYOUTGET fallback · inline promotion · prealloc pop
```

`placement_admit()` lives in `src/fsal_obj/placement_gate.c` with the pure
selection logic separated from I/O so unit tests drive it with synthetic
DS lists, capacity views and assessment views. `placement.c` keeps the
upstream functions for the legacy path (`placement_mode` absent) and for
the resilver/tier helpers, which are out of scope.

## 4. Configuration contract

All names, defaults and ranges live in one manifest,
`docs/placement-modes/contract-manifest.json` in this repo, consumed by the
MDS tests and the CLI helper (both read the same file in CI).

| Key | Default | Range / values | Applies to |
|---|---|---|---|
| `placement_mode` | absent = legacy behaviour | `rr` \| `fill` \| `smart` | all |
| `ds_capacity_poll_ms` | 60000 (existing) | fill/smart: `> 0` | fill, smart |
| `placement_capacity_max_age_ms` | 120000 | `> ds_capacity_poll_ms`, ≤ 86400000 | fill, smart |
| `placement_min_free_bytes` | 0 | uint64; a domain is a candidate only when `available > value` | fill, smart |
| `ds_capacity_domain.<ds_id>` | unset = the DS is its own domain | non-empty string ≤ 128 bytes | fill (required for shared-FS aliases), smart (must agree with the connector) |
| `placement_domain_weight.<domain>` | unset | uint32 ≥ 1; only with `placement_allow_manual_base_weights = true` | smart |
| `placement_allow_manual_base_weights` | false | bool | smart |
| `placement_stripe_shrink` | `allow` | `allow` \| `strict` | all (LAT-13) |
| `ds_connector_enabled` | derived | must be `true` iff mode is `smart`; an explicit contradiction is an error | smart |
| `ds_connector_socket` | `/run/lattice-ds-connector/connector.sock` | absolute path | smart |
| `ds_connector_poll_ms` | 1000 | 200..10000 | smart |
| `ds_connector_request_deadline_ms` | 500 | 50..`poll_ms` | smart |
| `ds_connector_expected_contract_major` | 1 | uint | smart |
| `ds_connector_max_ds` | 256 | 1..256 | smart |

Validation (`config.c`, fatal at startup, every error printed with its key):

- **Conflicts (MODE-02).** With `placement_mode` present, any of
  `placement_policy`, `placement_policy_enabled`,
  `placement_capacity_weighting` in the file is `PLACEMENT_MODE_CONFLICT`.
  A `workload_profile` whose preset sets a placement policy is the same
  error, naming the profile (the requirement asks for an error, not a
  silent override). `ds_weight.<id>` with `fill` is a conflict;
  `placement_domain_weight.*` without the advanced flag, or with `rr`/`fill`,
  is a conflict.
- **Prerequisites.** `fill`/`smart`: `ds_capacity_poll_ms > 0`;
  `placement_capacity_max_age_ms > ds_capacity_poll_ms`. `smart`:
  `default_mirror_count == 1` (LAT-13), binary built with
  `ENABLE_DS_PREALLOC=OFF` (compile-time `#error`-style refusal when both
  are on, LAT-18) and `ENABLE_WRR=ON` with the real kernel
  (`mds_wrr_kernel_id() != 0`, checked at startup); socket path present.
  A missing socket at runtime is **readiness**, never a mode change.
- **Aliases.** In `fill`, two DS whose back-mounts report the same
  `(host, f_fsid)` without an identical `ds_capacity_domain` entry is
  `SHARED_FS_ALIAS_UNMAPPED` (startup error once the first probe cycle has
  seen both; before that, both are UNKNOWN and excluded). In `smart`, the
  domain comes from the connector's `capacity_domain_id`; an operator map
  that disagrees is `DOMAIN_MAP_MISMATCH` for those DS (UNKNOWN, excluded,
  logged), not a startup failure — the connector may be updated first.
- **Legacy.** Without `placement_mode` the parser, the dispatcher and the
  log lines are byte-for-byte the upstream behaviour; the regression suite
  runs in that configuration (acceptance §7.1).

`placement_config_generation` is the SHA-256 of the canonical managed keys
(mode, thresholds, domain map, connector keys, geometry). It is logged at
startup, exported by `config show` and compared by `verify`.

## 5. Candidate gate and mode dispatch

```c
enum placement_mode { PM_LEGACY, PM_RR, PM_FILL, PM_SMART };

struct placement_candidate {
    uint32_t     idx;          /* index into the caller's ds_list */
    uint32_t     ds_id;
    uint64_t     weight;       /* fixed-point, > 0 for every candidate */
    const char  *domain;       /* NULL in rr */
};

/* Filters ds_list (already ONLINE/admin/profile/io-limit filtered by the
 * caller) by mode; fills out[] and per-reason rejection counts.
 * Pure: takes explicit capacity and assessment views + `now`. */
uint32_t placement_candidates(const struct placement_ctx *ctx,
                              const struct mds_ds_info *ds_list, uint32_t n,
                              struct placement_candidate *out,
                              struct placement_reject_counts *why);

/* The one entry point for a new backing object. */
enum mds_status placement_admit(const struct placement_ctx *ctx,
                                const struct mds_ds_info *ds_list, uint32_t n,
                                uint32_t *stripe_count, uint32_t mirror_count,
                                uint64_t rr_key,
                                struct mds_ds_map_entry *entries,
                                struct placement_reason *reason);
```

- **Order of checks** for every DS: native (caller) → capacity (fill,
  smart) → assessment (smart). The first failing check is the DS's reason;
  counts per reason go to `pnfs_mds_placement_rejections_total{reason}`.
- **rr:** candidates in registry order, cyclic start from the existing
  atomic counter (`rr_key` = counter or fileid for the prealloc rotation),
  `mirror_count` consecutive distinct candidates per stripe — the upstream
  RR algorithm over the gated list. Shrink to `candidates / mirror_count`
  stripes when `placement_stripe_shrink = allow`; `strict` refuses.
- **fill / smart:** per stripe, the weighted kernel picks among not-yet-taken
  candidates with positive weight; mirrors take the next distinct
  candidates. No walk-forward into a zero-weight or taken slot: a stripe
  with no remaining positive candidate ends the layout (shrink) or refuses
  (strict). Zero candidates → `MDS_ERR_NOSPC` with reason
  `NO_ELIGIBLE_DS`; callers keep returning `NFS4ERR_NOSPC` (LAT-20).
- **Fixed-point weights.** `weight = domain_weight × ppm × SCALE / N`,
  `SCALE = 65536`, `ppm = 1 000 000` in `fill`. Bounds: max
  `100 × 10⁶ × 65536 = 6.6·10¹²` per DS, `× 256` DS `= 1.7·10¹⁵ < 2⁶²`;
  min `1 × 1 × 65536 / 256 = 256 > 0`. So with `N ≤ 256` (`MDS_MAX_DS_NODES`)
  no positive rational weight quantizes to zero; the bound is asserted in
  a unit test and documented in the manifest (LAT-11, MODE-08).
- **Call-site inventory (LAT-14):** LAYOUTGET create path, LAYOUTGET
  fallback path, `promote_inline_to_ds`, prealloc pop/batch (stub and
  module). Each site: (1) native filter as today, (2) `placement_admit`,
  (3) no other selector. `grep -n "placement_select" src/` in CI must list
  only `placement.c`, `placement_gate.c` and the out-of-scope helpers
  (`placement_select_replacement`, `placement_select_for_tier`).
- **Race boundary (LAT-16/17):** admission reads one atomic snapshot of
  the capacity and assessment views (a pointer swap published by the
  background threads; readers hold a refcount for the call). No network,
  no lock across the syscall. A layout that cannot be completed is not
  persisted; partial DS files go to the existing GC.

## 6. Capacity gate

`ds_capacity.c` gains a per-DS **observation record** in the DS cache:

```c
struct ds_capacity_obs {
    uint64_t total_bytes, avail_bytes;   /* f_blocks·frsize, f_bavail·frsize */
    uint64_t fsid;                       /* statvfs f_fsid, alias proof */
    uint64_t observed_mono_ms;           /* 0 = never observed */
    uint32_t consecutive_failures;
};
```

- Only a **successful local** `statvfs` updates the record; a failure
  increments `consecutive_failures` and leaves the last values with their
  old timestamp (so they age out). Peer observations from the catalogue
  reload keep feeding the legacy `total/used` fields for `df` and the
  legacy policies, but never the observation record (B4).
- **Freshness:** `now − observed_mono_ms ≤ placement_capacity_max_age_ms`,
  else `CAPACITY_STALE` (or `CAPACITY_UNKNOWN` when never observed).
- **Hard gate:** `total > 0` and `avail > placement_min_free_bytes`, else
  `CAPACITY_FULL`. No substitution by 1, by raw free bytes or by a stale
  success.
- **Domain:** `ds_capacity_domain.<id>` when set, else `smart`'s
  connector-published `capacity_domain_id`, else the DS itself. One
  canonical observation per domain: the fresh observation of the alias
  with the lowest `ds_id`; when two fresh aliases disagree on `avail` by
  more than 1 % the smaller value is used and a rate-limited WARN names
  both (LAT-10). `N` = all registered DS in the domain, offline or denied
  included; the share is `1/N` and is not redistributed (MODE-07).
- `rr` never consults the record. The existing `proportional` auto-weight
  path is untouched for legacy configurations.

## 7. `smart`: the connector client

`src/mds/ds_connector.c` (new, behind `ENABLE_DS_CONNECTOR`, default ON in
our build; the module is a no-op when the mode is not `smart`):

- **Poll loop:** every `ds_connector_poll_ms` it `GET`s
  `/v1/assessments` over the Unix socket with
  `ds_connector_request_deadline_ms` as connect+read deadline, parses the
  batch with `jsmn` and validates: `contract_version` major matches,
  `runtime_epoch`/`sequence` advance within an epoch (a stale or replayed
  batch is dropped, counted), every record's `ds_id` is registered and
  ≤ `ds_connector_max_ds`, `quality`, `allowed`, `multiplier_ppm`
  (0..1 000 000), `remaining_ttl_ms`, `capacity_domain_id`,
  `binding_generation`. A record that fails validation excludes that DS
  (UNKNOWN); an envelope failure excludes every DS of that batch (LAT-06).
- **Cache:** an immutable array indexed by `ds_id`, published by pointer
  swap; each entry carries `allowed`, `ppm`, `quality`, `domain`,
  `reason[0..3]`, `expires_mono_ms = receive_mono + remaining_ttl_ms`. The
  MDS clock decides expiry (LAT-04). At startup every DS is UNKNOWN until
  the first valid batch (MODE-10). No DS state, weight or recall is ever
  changed by the connector (LAT-03).
- **Readiness:** `smart` reports `ready = socket reachable ∧ last batch
  valid ∧ every registered DS has a non-expired record`. Not ready means
  those DS are excluded; it never falls back to `rr`/`fill`.
- **Candidate rule:** `quality == VALID ∧ allowed ∧ ppm > 0 ∧ not expired`.
  Reasons: `ASSESSMENT_UNKNOWN`, `ASSESSMENT_STALE`, `CONNECTOR_DENIED`,
  `ZERO_MULTIPLIER`, `NO_BINDING`.
- **Domain identity** in `smart` is the connector's; a missing or
  contradictory domain (two records, one domain, different controllers)
  is UNKNOWN for those DS (MODE-07).

## 8. WRR kernel changes (`modules/wrr/wrr.c`, mirrored into the fork)

- `mds_wrr_kernel_id()` returns a non-zero build id (stub returns 0) so the
  MDS can prove the real kernel at startup; the CI job asserts it via
  `nm` and a runtime log line.
- New entry `mds_wrr_weighted_pick2(const uint64_t *w, uint32_t n,
  uint32_t *out)` returns `-1` when `n == 0` or every weight is zero
  instead of `0`; the gate uses it. The old symbols stay for the legacy
  path. Sum saturation and the 62-bit sampler remain; the gate's bound
  (§5) guarantees the sum is below `2⁶²`, and the kernel refuses (rather
  than loops) when a caller violates it.
- The `>64` DS path masks taken slots exactly like the stack path (one
  code path over a heap or stack buffer).

## 9. Observability

- Startup log: `placement_mode=<mode> generation=<sha256[:12]> kernel=<id>
  shrink=<allow|strict> connector=<off|socket>`.
- `mds-admin config show` gains `placement_mode`,
  `placement_mode_effective`, `placement_config_generation`,
  `placement_smart_ready`, per-DS rows `ds<id>: domain, capacity_age_ms,
  avail, assessment_age_ms, quality, allowed, ppm, weight_source, reason`.
- Metrics (bounded labels): `pnfs_mds_placement_mode` (gauge with
  `mode` label), `pnfs_mds_placement_rejections_total{reason}`,
  `pnfs_mds_placement_eligible_ds`, `pnfs_mds_capacity_age_ms{ds}`,
  `pnfs_mds_assessment_age_ms{ds}`, `pnfs_mds_connector_poll_errors_total`,
  `pnfs_mds_connector_batches_dropped_total{reason}`.
- Structured reasons in the LAYOUTGET/promotion error log separate health
  deny from real `ENOSPC` (LAT-20).

## 10. CLI helper `lattice-placement` (this repo, `tools/lattice-placement/`)

Python 3.9 stdlib, same style as the connector CLI. Commands:

- `mode show --mds host[,host]` — for each MDS: desired mode (config file
  over SSH or local), effective mode and generation (`mds-admin config
  show --mds-port <grpc_port>`), build flags (`kernel=`, `connector=` from
  the startup line), connector readiness, eligible/denied DS with
  capacity and assessment ages; WARN when the MDS differ. It never infers
  "smart is active" from the file alone (CLI-01).
- `mode validate <mode> [--config-dir] [--connector-socket]` — read-only
  preflight: parses the INI with the same rules as `config.c`
  (the manifest carries them), range checks, conflict checks, domain map
  consistency; for `smart` additionally `lattice-ds-connector preflight`
  over the socket (bindings cover every registered DS, no
  UNKNOWN/stale, contract and profile digests). Prints `READY` or
  `NOT_READY` with reasons; exit code accordingly (CLI-02).
- `mode set <mode> [--apply]` — dry-run diff by default. `--apply` rewrites
  only the local config file: line-preserving parser (comments and unknown
  keys kept), legacy placement keys removed, managed keys written, backup
  `mds.conf.<ts>.bak`, atomic rename, audit line
  (`who, when, old→new, digests`) appended to
  `/var/lib/lattice-placement/audit.log`, re-validation of the result. It
  prints that running daemons still use the old mode and does not restart
  anything (CLI-03). Leaving `smart` prints the health-veto warning.
- `mode verify --mds host[,host]` — every MDS reports the same effective
  mode and generation and, for `smart`, `ready`; otherwise exit 1 (CLI-04
  step 5).

The supported switch procedure (validate → drain new creates → identical
config on every MDS → controlled restart → verify → resume) is documented
in `docs/placement-modes/operations.md`; no online switch (CLI-05).

## 11. Connector preflight (this repo)

`lattice-ds-connector preflight [--socket] [--expect-ds 0,1,2]` reads
`/healthz` and `/v1/assessments` and reports: instance readiness, bindings
per DS id, quality/TTL per DS, contract version, profile/config digests,
domain consistency (two aliases, one domain, same controller). Read-only;
the connector still stores no placement mode. The CLI helper calls it.

## 12. Packaging and CI

- Fork branch `xinnor/placement-modes` carries the MDS changes as reviewable
  commits; `scripts/export-patches.sh` in this repo regenerates
  `mds/patches/6b4dcde/*.patch` with `git format-patch`, and
  `mds/manifest.json` records upstream SHA, fork SHA, patch digests, the
  connector profile digest and the required CMake flags
  (`ENABLE_WRR=ON`, `ENABLE_DS_CONNECTOR=ON`, `ENABLE_DS_PREALLOC=OFF`).
- CI (GitHub Actions on the fork): build with those flags, `ctest`,
  `nm` proof of the real kernel, the placement unit tests, the fairness
  test, and the call-site grep of §5. This repo's CI runs the connector
  tests, the CLI tests and the manifest/patch consistency check.
- Three example configs under `docs/placement-modes/examples/`
  (`mds.conf.rr`, `.fill`, `.smart`).

## 13. Testing and acceptance

| Level | What | Requirement |
|---|---|---|
| unit (cmocka, fork) | config parsing: absent key = legacy; each mode; every conflict and range error; profile conflict; alias map | §7.1 |
| unit | `placement_candidates` / `placement_admit` on synthetic views: rr cyclic order; fill excludes full/stale/unknown; smart excludes deny/UNKNOWN/expired/ppm 0; single DS, 64/65/256 DS, multi-stripe, shrink vs strict, mirrors distinct, no zero weight reaches the kernel | §7.3, §7.4 |
| unit | fairness with a seeded PRNG: equal fill → ≈ even; 80 %/20 % free → ≈ 4:1 over 100 000 draws within ±2 %; degraded 250 000 ppm vs healthy at equal capacity → ≈ 1:4; alias share: two DS on one domain vs one DS on another → domain totals equal | §7.2, §7.5 |
| unit | connector client: schema, epoch/sequence replay, TTL expiry on the MDS clock, envelope failure excludes the batch, no fallback when the socket is gone | §7.3 |
| integration (fork tests) | the four call sites go through the gate: a denied DS never receives a backing object via CREATE, LAYOUTGET, promotion, prealloc pop | §7.3 |
| stand (node223/node225, box + node 71) | `rr`: 40 files ≈ 20:20 in registry order; `fill` after filling one DS: skew follows free fraction; `smart` with the connector denying one DS: 0 files there, hold-down and recovery visible; existing files untouched; `show`/`verify` output | §7.2–7.6 |
| perf (stand) | ≥ 3 runs of the create benchmark, `smart` vs legacy: ≤ 5 % create throughput loss, ≤ 10 % p99 placement latency growth | LAT-25 |

Until the gate tests pass on the built MDS, `smart` is documented as
`NOT_READY` (§7.7).

## 14. Out of scope (deferred, recorded in `docs/TODO.md` of this repo)

Live cluster-wide switch (CLI-05); `mirror_count > 1` in `smart`;
`ENABLE_DS_PREALLOC=ON` with `smart` (LAT-18); per-filesystem/profile mode
override (`UNSUPPORTED_PROFILE` today); rebalance/resilver driven by
health; peer-observation freshness for metadata-only MDS.

## 15. Decisions taken in this design

1. MDS changes live in the fork (`XinnorLab/pnfs-lattice`,
   `xinnor/placement-modes`), exported as a patch series here — approved
   2026-09-23.
2. A conflicting `workload_profile` placement policy is a validation error,
   as the requirement states, not a silent override.
3. `fill` proves shared-FS aliases with `(host, f_fsid)` from the
   back-mounts; the operator map is mandatory only when that proof fires.
4. Fixed-point weights with `SCALE = 65536` (bounds in §5).
5. `show` uses `mds-admin config show` over the cluster transport port as
   the live source; the metrics endpoint is the secondary source.
6. Delivery order: A (MDS core, `rr`/`fill`, kernel, gate at all call
   sites) → B (`smart`, connector client, preflight) → C (CLI, examples,
   manifest, stand and perf acceptance). Each stage ends green on the fork
   CI and, for A and B, with a stand trial. Stand trials restart
   `pnfs-mds` on node223/node225 inside a maintenance window and are
   announced before they run.
