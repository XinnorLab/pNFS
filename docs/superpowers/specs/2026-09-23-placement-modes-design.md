# Lattice placement modes (`rr` / `fill` / `smart`) — design

**Status:** design for review, 2026-09-23, revision 2 (review findings 1–5
of the same day folded in: admission at the create-if-absent boundary,
full binding check per assessment, bounded manual weights, split readiness,
explicit alias map). Implements the requirements in
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
| B5 | four sites *select* a DS for a new file: LAYOUTGET new-file path (`compound_layout.c` ≈1976–2032, dispatcher-aware), a second LAYOUTGET path (≈2217, legacy `placement_select`), `promote_inline_to_ds` (`compound_data_io.c` ≈1868, legacy), and the prealloc pop/peek/batch path (`ds_prealloc_stub.c` — the community stub selects with `placement_select_ex` / `placement_select_rr_at2`) | one selection function called from all four; the legacy `placement_select*` entry points are removed from those sites |
| B5a | backing objects are *created* elsewhere, after selection: every `mds_proxy_ensure_ds_file*` in `proxy_io.c` opens with `O_WRONLY \| O_CREAT` and is idempotent create-if-absent; it is called by the `ds_prepare` worker (`ds_prepare.c` ≈241), by `layout_refresh_wide_stripe_fhs` on LAYOUTGET (`compound_layout.c` ≈1107, "re-creates the file if absent"), by the promotion write path, by the proxy READ/WRITE ensure calls (`compound_data_io.c` ≈2094, ≈2262), and by prealloc pop/batch/ensure | a grep for selectors does not find these; the gate must also sit at the create boundary — a lookup that never creates, and a create that needs admission (§5a) |
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
| `placement_domain_weight.<domain>` | unset | 1..10000; only with `placement_allow_manual_base_weights = true` (the range keeps the weight bound of §5; larger values are a validation error) | smart |
| `placement_allow_manual_base_weights` | false | bool | smart |
| `placement_stripe_shrink` | `allow` | `allow` \| `strict` | all (LAT-13) |
| `ds_connector_enabled` | derived | must be `true` iff mode is `smart`; an explicit contradiction is an error | smart |
| `ds_connector_socket` | `/run/lattice-ds-connector/connector.sock` | absolute path | smart |
| `ds_connector_poll_ms` | 1000 | 200..10000 | smart |
| `ds_connector_request_deadline_ms` | 500 | 50..`poll_ms` | smart |
| `ds_connector_expected_contract_major` | 1 | uint | smart |
| `ds_connector_max_ds` | 256 | 1..256 | smart |
| `ds_connector_expected_profiles` | unset | `<id>=<digest>[,...]`, at most 8, id `[A-Za-z0-9._-]{1,63}` | smart |

`ds_connector_expected_profile_digest` is a config error (removed
2026-09-25): replaced by `ds_connector_expected_profiles`, a map from
profile id to digest (`2026-09-25-per-profile-digest-design.md`).

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
  `SCALE = 65536`, `ppm = 1 000 000` in `fill`. `domain_weight` is the
  derived value (1..100) or, in `smart` with the advanced flag, the
  manual override (1..10000, §4). Bounds with the override at its
  maximum: `10⁴ × 10⁶ × 65536 = 6.6·10¹⁴` per DS, `× 256` DS
  `= 1.7·10¹⁷ < 2⁶²`; min `1 × 1 × 65536 / 256 = 256 > 0`. The product is
  computed in `unsigned __int128` and asserted `< 2⁶²` before the kernel
  sees it, so a future range change cannot silently overflow (review
  finding 3). With `N ≤ 256` (`MDS_MAX_DS_NODES`) no positive rational
  weight quantizes to zero; both bounds are asserted in a unit test with
  the extreme values and documented in the manifest (LAT-11, MODE-08).
- **Selection-site inventory (LAT-14):** LAYOUTGET create path, LAYOUTGET
  fallback path, `promote_inline_to_ds`, prealloc pop/peek/batch (stub and
  module). Each site: (1) native filter as today, (2) `placement_admit`,
  (3) no other selector. `grep -n "placement_select" src/` in CI must list
  only `placement.c`, `placement_gate.c` and the out-of-scope helpers
  (`placement_select_replacement`, `placement_select_for_tier`). This
  grep proves selection only; creation is proven by §5a.
- **Race boundary (LAT-16/17):** admission reads one atomic snapshot of
  the capacity and assessment views (a pointer swap published by the
  background threads; readers hold a refcount for the call). No network,
  no lock across the syscall. A layout that cannot be completed is not
  persisted; partial DS files go to the existing GC.

### 5a. Admission at the create-if-absent boundary (LAT-15/16, review finding 1)

Selection is necessary but not sufficient: a backing object is created
later, by `mds_proxy_ensure_ds_file`, `mds_proxy_ensure_ds_file_fh` and
`mds_proxy_ensure_ds_file_fh_batch` (B5a), from paths that never ran a
selector — the `ds_prepare` worker, the LAYOUTGET filehandle refresh, the
proxy READ/WRITE ensure calls, prealloc. The design therefore splits the
proxy helpers:

```c
/* Opens an EXISTING DS file and returns its handle; never creates.
 * MDS_ERR_NOTFOUND when the object is absent. Not gated: access to an
 * existing object is never blocked by placement (LAT-15). */
enum mds_status mds_proxy_lookup_ds_file_fh(ctx, ds_id, fileid, s, m, fh, &len);

/* Creates the DS file (O_CREAT) and returns its handle. Requires an
 * admission token minted by the gate for (ds_id, purpose, generations)
 * immediately before the call; refuses without one. */
enum mds_status mds_proxy_create_ds_file_fh(ctx, ds_id, fileid, s, m,
                                             const struct placement_token *tok,
                                             fh, &len);

/* The former ensure_* helpers become: lookup; if absent →
 * placement_gate_admit_create(gate, ds_id, PURPOSE_*, &tok) → create. */
```

`placement_gate_admit_create()` consults the same snapshot the selector
uses. Purposes: `NEW_OBJECT` (the object of a just-selected layout: the
DS was a candidate moments ago; the re-check catches a deny published in
between — LAT-16) and `RECREATE_MISSING` (an existing stripe map whose
object vanished). Rules per mode:

| Mode | `NEW_OBJECT` | `RECREATE_MISSING` |
|---|---|---|
| legacy, `rr` | admitted when the DS is `DS_ONLINE` and not admin-excluded (today's behaviour, now explicit) | same |
| `fill` | admitted when the DS's domain passes the capacity gate | same — a full or unknown domain does not get a new object even for an old file |
| `smart` | admitted when the DS passes the capacity and assessment gates | same — a denied/UNKNOWN DS does not get a new object; the caller sees `MDS_ERR_NOSPC` with reason `CONNECTOR_DENIED` / `ASSESSMENT_UNKNOWN`, which the LAYOUTGET refresh turns into the existing `NFS4ERR_DELAY` (entry not ready) and the proxy READ/WRITE path into `NFS4ERR_DELAY` as well, never into a create elsewhere |

The token is a small struct `{ds_id, purpose, snapshot_generation,
mono_ms}` valid for one call and one DS; the create helper checks that
`tok->ds_id == ds_id` and that the token is younger than
`placement_token_max_age_ms` (default 2000). A token cannot be reused
for another DS or stripe. Every proxy create site passes through this
pair; the CI grep for `O_CREAT` in `proxy_io.c` must find only
`mds_proxy_create_ds_file*`, and a unit test drives each former ensure
caller with a denied DS and asserts no file is created (§13).

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
- **Aliases are declared, not discovered (review finding 5).** In `fill`
  the operator map is the authority: two exports of one filesystem must
  carry the same `ds_capacity_domain`, on every MDS. The `(host, f_fsid)`
  probe is a check on that declaration, in three grades: (a) two DS in one
  declared domain whose fresh observations report different `f_fsid` on
  the same host string → `DOMAIN_MAP_CONTRADICTION`, both excluded; (b)
  two DS with the same host string and the same `f_fsid` but no shared
  declared domain → proven alias, `SHARED_FS_ALIAS_UNMAPPED`, startup
  error once observed (both UNKNOWN before that); (c) equal `f_fsid`
  behind *different* host strings (one xiNAS registered under two names
  or addresses) cannot be proven from NFS → `ALIAS_SUSPECTED`, a
  rate-limited WARN and a `verify`/`validate` finding, never silent and
  never an automatic merge. `smart` takes the domain from the connector
  (which knows the filesystem UUID) and treats an operator map that
  disagrees as `DOMAIN_MAP_MISMATCH` (§4).
- `rr` never consults the record. The existing `proportional` auto-weight
  path is untouched for legacy configurations.

## 7. `smart`: the connector client

`src/mds/ds_connector.c` (new, behind `ENABLE_DS_CONNECTOR`, default ON in
our build; the module is a no-op when the mode is not `smart`):

- **Poll loop:** every `ds_connector_poll_ms` it `GET`s
  `/v1/assessments` over the Unix socket with
  `ds_connector_request_deadline_ms` as connect+read deadline, parses the
  batch with `jsmn` and validates it in two layers (LAT-06, review
  finding 2):
  1. **Envelope.** `contract_version` major ==
     `ds_connector_expected_contract_major`; `runtime_epoch` — a change
     resets every instance's sequence line and every DS to UNKNOWN; per
     `connector_instance_id`, `epoch` + `sequence` must advance (a
     replayed or lower sequence within the same epoch drops the whole
     batch, `pnfs_mds_connector_batches_dropped_total{reason=replay}`);
     `generated_at` not older than the last accepted batch;
     `config_digest` == `ds_connector_expected_config_digest` when that key
     is set (LAT-22; `verify` compares the digests the MDS report even
     when it is not set, LAT-24). An envelope failure excludes every DS
     of that batch.
  2. **Binding, per assessment** (all fields are required by the batch
     contract, `contracts/connector-batch.schema.json`): `ds_id`
     registered and ≤ `ds_connector_max_ds`; `scope == "ds"` and
     `access_scope_id == ds_connector_access_scope` (default `global`);
     `endpoint.server` equals the registry's `host`; without
     `endpoint.ds_path`, `endpoint.export_path` equals the registry's
     `export_path`; with it, `ds_path` equals the registry's
     `export_path` and `export_path` is `ds_path` or a component-wise
     ancestor of it, never `/` unless `ds_path` is `/` (trailing `/` ignored;
     `2026-09-26-endpoint-ds-path-design.md`) and `endpoint.port` equals
     `tcp_port` when the registry has one;
     when `ds_connector_expected_profiles` pins `profile.id`, its
     `profile.digest` must equal the pin for that id, and the id must be
     pinned at all — otherwise only that record is rejected
     (`rejected_binding`); one profile id with two digests, or more than
     8 distinct ids, in one batch drops the whole batch
     (`PROFILE_INCONSISTENT` / `PROFILE_LIMIT`, see
     `2026-09-25-per-profile-digest-design.md` §3.3–§3.4); `quality`,
     `placement.allowed`, `placement.multiplier_ppm` (0..1 000 000),
     `remaining_ttl_ms`, `resources.capacity_domain_id` well-typed. The
     MDS pins, per `ds_id`, the tuple
     `(connector_instance_id, binding_generation, datastore_id,
     target_id, target_incarnation, access_scope_id)` of
     the first accepted record (the profile is not part of the pin: a
     connector profile reload is not a rebind). A later record must repeat the tuple
     exactly, **or** carry a strictly higher `binding_generation` — that
     re-pins the tuple and resets that DS to UNKNOWN until its fresh
     record is accepted (the operator rebound the DS). A lower generation,
     or the same generation with any other field changed (a DS id
     re-assigned to another share, a share recreated without a rebind),
     is `BINDING_MISMATCH`: the record is rejected, the DS is UNKNOWN,
     the mismatch is counted and logged with both tuples. A record that
     fails any check excludes only that DS.

     **Unobserved incarnation (stand finding 2026-09-24).** The batch
     schema allows `target_incarnation: null` on a `quality = UNKNOWN`
     record and forbids it on a `VALID` one: when the connector cannot
     read its source at all (`SOURCE_UNAVAILABLE`, `SOURCE_FAILED`) it
     knows the binding it was configured with but not the share's
     incarnation. Such a record is compared on the rest of the tuple
     (instance, generation, datastore_id, target_id, access scope) and
     the incarnation is *not* compared; the record is accepted as UNKNOWN
     with the connector's reason codes (`ASSESSMENT_UNKNOWN` in the
     gate), the pin is left as it was, and no pin is created from it — the
     first VALID record pins. A higher generation on an unobserved record
     clears the pin (rebind) without pinning. A VALID record with a null
     incarnation is a shape error. Before this rule the MDS read the null
     as a changed incarnation and reported `BINDING_MISMATCH` — the same
     refusal, but it sent the operator to rebind a DS whose only problem
     was a stopped source.
- **Cache:** an immutable array indexed by `ds_id`, published by pointer
  swap; each entry carries `allowed`, `ppm`, `quality`, `domain`,
  `reason[0..3]`, `expires_mono_ms = receive_mono + remaining_ttl_ms`. The
  MDS clock decides expiry (LAT-04). At startup every DS is UNKNOWN until
  the first valid batch (MODE-10). No DS state, weight or recall is ever
  changed by the connector (LAT-03).
- **Readiness is four facts, not one flag (review finding 4).**
  `mode_active` (the effective mode is `smart`), `connector_config_valid`
  (build flags, socket path, thresholds — fixed at startup),
  `connector_reachable` (a successful poll within `3 × poll_ms`) with
  `last_batch_valid`, and `coverage ∈ {full, partial, none}` with
  `eligible_ds_count` and the list of UNKNOWN/denied/stale DS. A DS
  without a fresh valid record is excluded from placement; the mode
  keeps placing on the eligible remainder and never falls back to
  `rr`/`fill`. `coverage = none` with `mode_active` is the operational
  alarm ("smart is placing nothing"); `partial` is a degraded-but-correct
  state and is reported as such by `show` and `verify` (§10).
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

**Stage C additions (2026-09-24).** `config show` carries one build row in
every mode, `placement_build = wrr=<0|1> connector=<0|1> prealloc=<0|1>`
(the kernel is real, the connector client is compiled in, the enterprise
prealloc module is compiled in) — `show`/`verify` compare it across MDS
instead of reading journals. One latency histogram,
`pnfs_mds_placement_admit_seconds` (the upstream 12-bucket layout, 100 µs
… +Inf, with `_sum`/`_count`), is observed once per
`placement_select_gated` call in **every** mode — legacy included, so the
LAT-25 row compares like with like — and once per
`placement_gate_admit_create`.

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
- `mode verify --mds host[,host]` — exit 1 when the MDS differ in
  effective mode or generation, when a `smart` MDS reports
  `connector_config_valid = false`, `connector_reachable = false` or
  `coverage = none`, or when the connector `config_digest`/profile digests
  the MDS report differ between MDS. `coverage = partial` is printed as a
  warning with the affected DS and their reasons and exits 0 — one
  UNKNOWN DS is a degraded DS, not a failed switch; `--require-full-coverage`
  turns it into exit 1 for operators who want that gate (CLI-04 step 5).

The supported switch procedure (validate → drain new creates → identical
config on every MDS → controlled restart → verify → resume) is documented
in `docs/placement-modes/operations.md`; no online switch (CLI-05).

**Precisions fixed in Stage C (2026-09-24):**

- *Live source.* `show`/`verify` run `mds-admin config show --json`
  against each host (`--mds-admin` path, `--mds-port` default 50051,
  `--env KEY=VALUE` passthrough for `LD_LIBRARY_PATH`) and, secondarily,
  scrape `/metrics` (`--metrics-port` 9090, `--no-metrics`). The desired
  mode comes from `--config <path>` (a local file) or `--ssh <user>` (`ssh
  <user>@<host> cat <path>`); when neither is given it prints `?` — it is
  never inferred from the effective mode. Exit 2 when an MDS cannot be
  read.
- *Profile rule in `validate`.* Every shipped `workload_profile` other than
  `default` (`hpc`, `ai_training`, `genomics`, `media`) sets a placement
  policy, so any of them with `placement_mode` is
  `PLACEMENT_MODE_CONFLICT`; the manifest lists them
  (`profiles_with_placement_policy`).
- *Managed block in `set`.* The keys the helper owns live between
  `# lattice-placement managed block` and `# end lattice-placement managed
  block`; everything else in the file is preserved byte for byte
  (line-preserving document, `config.c` grammar: `#`/`;` comments,
  `[section]` lines skipped, first `=` splits, last key wins). `set
  <rr|fill|smart>` removes the legacy keys (`placement_policy`,
  `placement_policy_enabled`, `placement_capacity_weighting`,
  `ds_weight.<id>`) and writes `placement_mode` plus `--set key=value`
  pairs (manifest keys applying to that mode only). `set legacy` removes
  the block and every `placement_*` / `ds_capacity_domain.*` /
  `ds_connector_*` key and writes `placement_policy_enabled = true`,
  `placement_policy = <--legacy-policy, default wrr>` and `ds_weight.<id>
  = <w>` from `--ds-weight id=w`. The audit line is JSON: `ts, user,
  sudo_user, host, file, old_mode, new_mode, old_sha256, new_sha256,
  backup`. If the rewritten file fails validation the backup is restored
  and the exit code is 1. Leaving `smart` prints the health-veto warning;
  entering it prints that no DS is admitted before the first fresh VALID
  assessment.
- *`verify` comparison set.* Effective mode, `placement_config_generation`
  and `placement_build` must be identical across MDS; a known desired
  mode that differs from the effective one is an error (the daemon was
  not restarted); in `smart` every MDS must report
  `connector_config_valid=1`, `connector_reachable=1` and a coverage other
  than `none`, and the connector `config_digest` and the profile map
  (`placement_connector_profiles`) must be identical across MDS
  (`CONNECTOR_PROFILES_MISMATCH`). `coverage=partial` is a warning (exit 0) that
  lists the non-eligible DS with their reasons; `--require-full-coverage`
  makes it exit 1.

## 11. Connector preflight (this repo)

`lattice-ds-connector preflight [--socket] [--expect-ds 0,1,2]
[--expect-profiles id=digest,...]` reads `/healthz` and `/v1/assessments`
and reports: instance readiness, bindings per DS id, quality/TTL per DS,
contract version, config digest, one digest per profile id
(`PROFILE_INCONSISTENT:<id>` when a profile id carries two digests), and
with `--expect-profiles` the pins as well
(`PROFILE_NOT_PINNED:<id>` / `PROFILE_PIN_MISMATCH:<id>`), domain
consistency (two aliases, one domain, same controller). Read-only;
the connector still stores no placement mode. The CLI helper calls it,
passing the pins automatically in `mode validate`.

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
| unit | connector client: schema, epoch/sequence replay, TTL expiry on the MDS clock, envelope failure excludes the batch, no fallback when the socket is gone; binding: endpoint mismatch, re-assigned ds_id with the same generation, lower generation, higher generation re-pins and resets to UNKNOWN, profile pins by id (one id with two digests drops the batch, more than 8 ids drops the batch), config digest pin | §7.3, LAT-06 |
| unit | create boundary: with a denied (smart) or full/stale (fill) DS, each former ensure caller — ds_prepare job, LAYOUTGET refresh, promotion write, proxy READ/WRITE ensure, prealloc pop/batch/ensure — creates no file and returns the mapped status; lookup of an existing object still succeeds; a token for DS A is refused for DS B and after its max age | LAT-15/16, review finding 1 |
| unit | weight bounds with the manual override at 10000 and N = 1 and 256: sum < 2⁶², min > 0; a value of 10001 is a config error | LAT-11, review finding 3 |
| unit | readiness/verify: partial coverage = warning + exit 0, none = exit 1, `--require-full-coverage` | review finding 4 |
| integration (fork tests) | the four call sites go through the gate: a denied DS never receives a backing object via CREATE, LAYOUTGET, promotion, prealloc pop | §7.3 |
| stand (node223/node225, box + node 71) | `rr`: 40 files ≈ 20:20 in registry order; `fill` after filling one DS: skew follows free fraction; `smart` with the connector denying one DS: 0 files there, hold-down and recovery visible; existing files untouched; `show`/`verify` output | §7.2–7.6 |
| perf (stand) | ≥ 3 runs of the create benchmark, `smart` vs legacy: ≤ 5 % create throughput loss, ≤ 10 % p99 placement latency growth | LAT-25 |

**Status (2026-09-24):** the gate tests pass on the built MDS and every stand row above has been run — `rr`/`fill` (`stand-2026-09-23.md`), `smart` with the agent and the connector stopped (`stand-2026-09-24.md`), the two-MDS switch through the helper with `show`/`verify` and the LAT-25 row (`stand-2026-09-24-stage-c.md`: no throughput loss, +1.7 µs per gate call, p99 inside the first bucket). `smart` is no longer documented as `NOT_READY`; what stays open is in `docs/TODO.md`.

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
3. `fill` aliases are declared with `ds_capacity_domain`; the
   `(host, f_fsid)` probe validates the declaration (contradiction and
   proven-unmapped alias are errors, a suspected alias behind two host
   strings is a diagnostic).
4. Fixed-point weights with `SCALE = 65536` (bounds in §5).
5. `show` uses `mds-admin config show` over the cluster transport port as
   the live source; the metrics endpoint is the secondary source.
6. Delivery order: A (MDS core, `rr`/`fill`, kernel, selection gate at
   all selection sites and the create-boundary split in `proxy_io.c`) →
   B (`smart`, connector client with the binding check, preflight) → C
   (CLI, examples, manifest, stand and perf acceptance).
   Landed: A at fork `ced2043` (review wave 1) + stand 2026-09-23; B at `ccfbcaa` → `96d02b8` (review wave 2) → `3a76e6c` (stand finding) → `1bbd41d`; C at `2f1b52c` (build row + histogram) with the helper, CI, runbook and stand in this repo (`c3ff56e` … `0c92b87`).
7. Review findings 1–5 (2026-09-23) are folded in: §5a, §7 (binding),
   §4/§5 (override range and 128-bit check), §7/§10 (readiness split),
   §6 (declared aliases). Each stage ends green on the fork
   CI and, for A and B, with a stand trial. Stand trials restart
   `pnfs-mds` on node223/node225 inside a maintenance window and are
   announced before they run.
8. Profiles are pinned per id (`ds_connector_expected_profiles`), not one
   digest per batch — see `2026-09-25-per-profile-digest-design.md`.
