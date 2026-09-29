# `smart`: verdict retention and the neutral multiplier — design

Status: approved in chat by Sergey on 2026-09-29 ("Да, делай файл состояния, пиши спеку и план").
Supersedes, for `placement_mode = smart`, the fail-closed rule of the placement-modes
design (`2026-09-23-placement-modes-design.md` §5 candidate rule, §7 "Readiness"/candidate
rule, §13 acceptance rows) and of the requirements package
(`lattice_placement_modes_mvp_requirements.md` §1 "неизвестный/просроченный assessment
означает запрет новой allocation" and §7.3 "UNKNOWN / expired TTL … ни разу не получает
новый backing object").

## 1. What changes for the operator

The connector steers placement; it no longer decides whether a data store is usable.

- A data store the connector has never reported on is placed like in `fill`: its
  multiplier is 1 (1 000 000 ppm).
- Once the connector has reported a verdict, that verdict stays in force while no new
  verdict arrives: **20 minutes** for a critical verdict (the connector denies new
  allocations), **10 minutes** for any other verdict (normal or degraded). After that the
  data store is back to multiplier 1.
- Losing data never makes a data store unavailable: a source the connector cannot read, a
  connector that is down or restarting, a record the MDS rejects, an MDS without its first
  batch — all are "no new data". Only a verdict the connector actually observed can lower a
  weight or deny, and only for its hold time.
- The MDS's own checks do not change: the data server's `ONLINE` state and the capacity
  gate (`statvfs` of the back-mount, `min_free`, freshness, the alias grades) still exclude
  a data store on their own. A dead data store still drops out — through them.

Before this change the same outages refused every new file (stand 2026-09-24: `xinas-agent`
stopped → `ASSESSMENT_UNKNOWN` in 13 s → ENOSPC; connector stopped → `ASSESSMENT_STALE` in
15 s → ENOSPC; an MDS without a connector → `MODE_NOT_READY` → ENOSPC): the connector was a
single point of failure for new allocations.

## 2. Terms

| Term | Meaning |
|---|---|
| **verdict** | the placement part of a `quality = VALID` assessment: `allowed`, `multiplier_ppm`, `reason_codes`, `capacity_domain_id`, with the binding it belongs to and the time it was observed |
| **critical verdict** | `allowed = false` (this includes `ZERO_MULTIPLIER`, which the connector normalises to a deny) |
| **other verdict** | `allowed = true`, any `multiplier_ppm` in 1..1 000 000 (normal = 1 000 000, degraded = e.g. 250 000) |
| **observed at** | the source's observation time of the evidence the verdict was computed from: the record's `observed_at`, equivalently `fetched − evidence_age_ms` |
| **hold** | how long a verdict stays in force without a new one: `critical_hold_ms` (default 1 200 000) for a critical verdict, `verdict_hold_ms` (default 600 000) for any other; counted from *observed at* |
| **verdict in force** | the newest verdict for a binding whose hold has not run out |
| **no new data** | anything that is not a newer `VALID` record for the same binding: an `UNKNOWN` record (any cause), a collection error of any kind, no batch, a batch the MDS drops, a record the MDS rejects (binding mismatch, shape) |
| **neutral** | multiplier 1 000 000 ppm and the operator's capacity domain (`ds_capacity_domain.<id>` or `ds:<id>`), i.e. the `fill` weight |

## 3. Rules

1. **Never reported → neutral.** A registered data store without a verdict in force is
   neutral. This covers: no binding for it on the connector, the connector never answered,
   the MDS started and has no batch yet (`MODE_NOT_READY` is no longer produced), the hold
   ran out.
2. **A newer VALID record replaces the verdict at once**, in both directions. A worse verdict
   (deny, lower multiplier) takes effect on the next MDS poll; a better one is subject to the
   connector's recovery hold-down (rule 6).
3. **No new data never replaces a verdict.** The verdict in force keeps counting down its
   hold from *observed at*; when the hold runs out the data store becomes neutral.
4. **The hold is chosen by the verdict it protects:** `critical_hold_ms` for a critical
   verdict, `verdict_hold_ms` otherwise. A retained verdict keeps its original
   *observed at*; re-publishing it never extends the hold.
5. **An explicit rebind clears the verdict.** A strictly higher `binding_generation` (the
   operator re-bound the data store) drops the verdict of the old binding: the data store is
   neutral until the first VALID record of the new binding. A connector restart (new
   `runtime_epoch`) and a configuration reload that keeps the binding do **not** clear it.
6. **Recovery hold-down only follows a deny.** When the verdict in force is critical and a
   fresh VALID record allows, the connector keeps publishing the deny (reason
   `RECOVERY_HOLD_DOWN` first) until its hold-down completes (`recovery_hold_down_ms`,
   `recovery_distinct_cycles`, unchanged). That deny's *observed at* is the last observation
   that was itself critical — the hold-down never extends the critical hold. After a start,
   an `UNKNOWN` period or a neutral period the first fresh verdict is published at once:
   the connector no longer turns "I just started" into a deny.
7. **Neutral does not bypass the MDS's gates.** Admin/heartbeat state, the capacity gate and
   the alias grades apply to a neutral data store exactly as in `fill`. `DOMAIN_MAP_MISMATCH`
   (the operator's `ds_capacity_domain.<id>` contradicts the connector's domain of a verdict
   in force) is unchanged: it excludes the data store — it is an operator configuration
   error, not a connector failure.

## 4. Connector (`connectors/lattice-ds-connector`)

### 4.1 Profile

Two new profile fields, part of the profile digest (`Profile.placement_fields`):

| Field | Default | Range | Meaning |
|---|---|---|---|
| `critical_hold_ms` | 1 200 000 | `source_max_age_ms` .. 3 600 000 | hold of a critical verdict |
| `verdict_hold_ms` | 600 000 | `source_max_age_ms` .. 3 600 000 | hold of any other verdict |

`source_max_age_ms` keeps its meaning: the oldest evidence the policy accepts as fresh when
it computes a verdict. It no longer sets the published TTL. A profile digest change means
the MDS pins in `ds_connector_expected_profiles` must be updated with the new digests
(runbook step).

### 4.2 The verdict store

One store per `Runtime` (not per `InstanceRuntime`, so a configuration reload that recreates
an instance keeps it). Key: `(instance id, ds_id, binding_generation, target_id,
expected_target_incarnation)`. Value: the verdict (`allowed`, `multiplier_ppm`,
`reason_codes`, `capacity_domain_id`, `shared_resource_ids`, `datastore_id`,
`target_incarnation`), `observed_at` (UTC, from the record), `critical` (bool), and — for a
critical verdict under hold-down — `critical_observed_at` (the last critical observation).

- A cycle's post-normalisation, post-hold-down `VALID` record for a binding **replaces** the
  entry (rule 2), with `observed_at` = the record's observation time. When the published
  record is a hold-down deny, the entry stays critical and keeps `critical_observed_at`.
- A cycle's `UNKNOWN` record, a collection error of any kind (retryable or not, including
  `SOURCE_AUTH_FAILED`, `SOURCE_SCHEMA_INVALID`, `SOURCE_FAILED`, `WORKER_STUCK`) leaves the
  entry untouched (rule 3). Errors are still logged, counted and alerted as today; they no
  longer revoke anything.
- An entry expires when `now_utc − observed_at ≥ hold(entry)`; expired entries are dropped.
- A binding removed from the configuration, or whose generation/target/incarnation changed,
  loses its entry (rule 5).

### 4.3 What the batch carries

For each binding, in this order:

1. The cycle produced a `VALID` record → publish it; `remaining_ttl_ms = hold − age`, where
   `age = evidence_age_ms + (now − fetched)`, clamped to 0..3 600 000.
2. Otherwise the store has an entry in force → publish the **retained** verdict:
   `quality = VALID`, the stored `allowed`/`multiplier_ppm`/`capacity_domain_id`/identity,
   `observed_at` = the stored one, `evidence_age_ms` = now − observed_at,
   `remaining_ttl_ms` = hold − that age, `reason_codes = ["VERDICT_RETAINED", <why no new
   data: the UNKNOWN record's first reason or the collection error code>, <stored reasons>…]`
   (bounded to `MAX_REASON_CODES`).
3. Otherwise → the `UNKNOWN` record as today (null `target_incarnation` allowed).

`render()` recomputes ages and TTLs at read time, as now; a retained verdict whose TTL
reaches 0 is replaced by the `UNKNOWN` record (reason `EVIDENCE_EXPIRED` first).

A collection error of any kind publishes a new snapshot (sequence + 1) built only from the
store: each binding carries its retained verdict (cause = the error code) or the `UNKNOWN`
record; `snapshot_status` stays `FAILED` because the source snapshot did fail. A `VALID`
record inside a `FAILED` snapshot is therefore always a retained verdict and always carries
`VERDICT_RETAINED`.

### 4.4 The state file

- Path: `runtime.state_path`, default `/var/lib/lattice-ds-connector/verdicts.json`; `null`
  disables persistence (tests, fixture setups). The unit gets `StateDirectory=lattice-ds-connector`
  (`StateDirectoryMode=0750`) and `ReadWritePaths` for it; the file is `0600`, owner
  `lattice-ds-connector`.
- Content (JSON): `{"version": 1, "written_at": <UTC>, "runtime_epoch": …, "verdicts": [
  {"instance": …, "ds_id": …, "binding_generation": …, "target_id": …,
  "expected_target_incarnation": …, "profile_id": …, "allowed": …, "multiplier_ppm": …,
  "reason_codes": […], "capacity_domain_id": …, "shared_resource_ids": […], "datastore_id": …,
  "target_incarnation": …, "observed_at": …, "critical": …, "critical_observed_at": …}]}`.
  No credentials, no endpoints beyond the binding identity.
- Written after every cycle that changes the store (a new verdict, a new `observed_at`, an
  expiry), at most once per `collect_interval_ms`: temp file in the same directory, `fsync`,
  `rename`, `fsync` of the directory. A write failure is a `WARN` plus
  `connector_state_write_errors_total`; the connector keeps running on its in-memory store.
- Loaded once at start, before the first publication. An entry is restored only when its key
  matches a configured binding exactly, its `profile_id` is the binding's profile id, and its
  hold has not run out by the wall clock; `observed_at` in the future (the clock stepped
  back) counts as age 0 and the remaining hold is capped at the full hold. A restored entry
  is published as a retained verdict with `RESTORED_FROM_STATE` after `VERDICT_RETAINED`. An
  unreadable, unparsable or wrong-version file is a `WARN`, is renamed to
  `verdicts.json.corrupt-<ts>`, and the connector starts with an empty store — never a
  refusal to start.

### 4.5 Contract

- `contract_version` becomes `"1.1"` (the MDS accepts major 1).
- `remaining_ttl_ms`: maximum 3 600 000 in `contracts/connector-batch.schema.json` and
  `contract.MAX_REMAINING_TTL_MS` (the MDS already accepts ≤ 3 600 000, `DC_TTL_MAX_MS`). Its
  meaning becomes "how long this verdict stays in force without a new one".
- New reason codes: `VERDICT_RETAINED` ("the source gave no new verdict; the last observed one
  is repeated until its hold runs out"), `RESTORED_FROM_STATE` ("restored from the state file
  after a connector restart").
- `preflight` reports, per data store, `fresh`/`retained`/`restored` and the hold left; a
  retained verdict is not a preflight failure.

## 5. MDS (fork `XinnorLab/pnfs-lattice`, `xinnor/placement-modes`)

### 5.1 The MDS verdict store (`ds_connector.c`)

`struct ds_connector_state` gains `struct ds_connector_verdict verdicts[MDS_MAX_DS_NODES]`:
`bool live; bool allowed; uint32_t ppm; uint32_t binding_generation; uint64_t
received_mono_ms, expires_mono_ms; bool retained; char domain[PM_DOMAIN_ID_MAX]; char
reasons[PA_REASONS_MAX][PA_REASON_LEN]; uint32_t reason_count`.

- An accepted `VALID` record replaces the entry: `expires = receive + remaining_ttl_ms`,
  `retained` = its `reason_codes` contain `VERDICT_RETAINED`. In an instance snapshot whose
  `snapshot_status` is `FAILED` only a `VALID` record carrying `VERDICT_RETAINED` counts;
  any other `VALID` record there is treated as `UNKNOWN` (today's rule, `valid && !failed`,
  narrowed to fresh records).
- An accepted `UNKNOWN` record, a rejected record (binding mismatch, shape) and a dropped
  batch do not touch the entry (rule 3).
- A rebind (strictly higher `binding_generation`) clears the entry (rule 5); the
  `runtime_epoch` reset keeps entries (only sequence lines and pins reset, as today).
- The published `placement_assessment_view` is built from the store: a row is `present`
  when the entry is live and unexpired, carrying `allowed`, `multiplier_ppm`,
  `expires_mono_ms`, `domain`, `reasons` and a new `retained` flag. When the connector is
  unreachable no new view is built; the rows of the last view expire on their own
  `expires_mono_ms` — the MDS holds a verdict exactly as long as the connector said, never
  longer.

### 5.2 The gate (`placement_gate.c`)

In `smart`, per data store after the capacity gate:

- no row, row not present, or `now ≥ expires_mono_ms` → **neutral**: `ppm = 1 000 000`,
  domain = the operator map / `ds:<id>`, counted in `neutral_ds`;
- present and `!allowed` → excluded, `CONNECTOR_DENIED`; present and `ppm == 0` →
  `ZERO_MULTIPLIER` (as today);
- present and allowed → weight with its `ppm` and the connector domain (the
  `DOMAIN_MAP_MISMATCH` check applies only to a live row), counted in `retained_ds` when the
  row is `retained`.
- `ctx->assess == NULL` (no batch since start) → every data store neutral; the
  `MODE_NOT_READY` refusal is removed from `candidates_weighted` and `placement_admit`.
- `placement_ds_admitted` / `placement_gate_admit_create` follow the same rule.
- The reason enum keeps `NO_BINDING`, `ASSESSMENT_UNKNOWN`, `ASSESSMENT_STALE` and
  `MODE_NOT_READY` (metric label stability); `smart` no longer produces them.

### 5.3 Readiness, `config show`, metrics

- `placement_readiness` adds `retained_ds=… neutral_ds=…`; `coverage` counts data stores with
  a live verdict (fresh or retained). `mode_active`, `connector_config_valid`,
  `connector_reachable`, `last_batch_valid` keep their definitions.
- `placement_ds.<id>` adds `verdict=fresh|retained|none` and `hold_left_ms=<n>|none`; for a
  neutral data store `quality=NONE allowed=- ppm=1000000 reason=NONE`.
- Metrics: gauges `pnfs_mds_placement_neutral_ds`, `pnfs_mds_placement_retained_ds` (at the
  last decision); counter `pnfs_mds_connector_verdicts_expired_total` (a verdict ran out
  without a new one).

## 6. Helper (`tools/lattice-placement`)

- `mode verify`: in `smart`, `coverage=none` and `connector_reachable=0` become **warnings**
  (`STEERING_OFF:<host>`, `CONNECTOR_UNREACHABLE:<host>`) — the cluster places, it just does
  not steer. Errors stay: differing effective mode / generation / build / connector digests /
  profile maps, `DESIRED_NE_EFFECTIVE`, `connector_config_valid=0`, an unreadable MDS.
  `--require-full-coverage` still turns partial or no coverage into exit 1, and now also a
  data store held only by a retained verdict (`--require-fresh` is not added: YAGNI).
- `mode validate smart`: a connector that is not ready is a **warning**, not an error.
- `mode show`: the per-DS line shows `verdict=… hold_left=…`; the readiness line shows
  `retained=… neutral=…`.

## 7. Documentation to amend (spec-first, in the same change set)

- `docs/superpowers/specs/2026-09-23-placement-modes-design.md`: §5 candidate rule, §7
  readiness and candidate rule, §10 `verify`, §13 acceptance rows — pointing here.
- Fork `docs/placement-modes.md`: the smart candidate rule, readiness, `config show` rows,
  metrics.
- `connectors/lattice-ds-connector/docs/profile-xinas-mvp.md` (hold fields, hold-down rule),
  `troubleshooting.md` (`VERDICT_RETAINED`, `RESTORED_FROM_STATE`, the state file),
  `README.md`, `docs/compatibility-manifest.json`.
- `docs/placement-modes/contract-manifest.json`: contract 1.1, `ttl_max_ms`, the new reason
  codes, the new rows/metrics, `verify_exit`.
- `docs/placement-modes/operations.md`: what "neutral" means, the alerts to set
  (`pnfs_mds_connector_reachable == 0`, `pnfs_mds_placement_neutral_ds > 0` while
  `coverage` was full), the profile-pin update after the profile digest change.
- `docs/TODO.md`: the entries this closes (hold-down after start) and what stays open.

## 8. Rollout and compatibility

- **MDS first.** The new MDS with the current connector (contract 1.0, TTL 20 s, `UNKNOWN` on
  failure) is safe: verdicts are held for the connector's 20 s and the data store is
  neutral afterwards instead of refused.
- **Connector second.** The new connector with an old MDS (Stage C) is accepted (TTL ≤ 1 h),
  holds denies for their hold, but that MDS still refuses on `UNKNOWN` — do not run that
  pairing on purpose.
- The profile digest changes with the two new fields: update
  `ds_connector_expected_profiles` on every MDS (`lattice-placement mode set --set …`) in the
  same window.

## 9. Testing and acceptance

| Level | What |
|---|---|
| connector unit | store replace/keep/expire per rule; hold chosen by verdict class; retained rendering (reason order, ages, TTL) and expiry to `UNKNOWN`; every collection-error class keeps verdicts; rebind and changed incarnation clear; reload keeps; hold-down only after a deny and never extends the critical clock; start publishes a fresh allow at once; state file round trip, foreign/expired/future entries, corrupt file renamed, write failure tolerated, write rate bounded |
| MDS unit | `UNKNOWN`/rejected records do not overwrite; expiry to neutral on the MDS clock; no view → every data store neutral with `fill` weights; deny held until its TTL; rebind clears, epoch reset keeps; `config show` rows and readiness counts; `admit_create` admits neutral and refuses a live deny; manifest constants |
| helper unit | `verify` warnings vs errors above; `show` columns; fixtures updated |
| stand (lab, shortened holds: `critical_hold_ms = 180000`, `verdict_hold_ms = 90000` in the lab profile) | (1) `smart`, DS 1 unbound → neutral: 40 files ≈ 20 : 20 via MDS 2; (2) DS 0 critical by adding a client network its export does not grant to the binding's `expected_client_networks` (`EXPORT_ACCESS_MISSING`, box untouched) → 0 files on DS 0; (3) `systemctl stop xinas-agent` → DS 0 `verdict=retained` and still 0 files on DS 0 for 180 s, then neutral and files land on DS 0 again; (4) connector restart during the hold → `RESTORED_FROM_STATE`, still denied; (5) MDS restart during the hold → the connector re-publishes, still denied; (6) connector stopped → the MDS holds the deny until its TTL, then neutral, no ENOSPC anywhere; (7) restore the binding and legacy; one `pm-bench.sh` run per mode (gate cost unchanged) |

## 10. Out of scope

MDS-side persistence of verdicts (the connector's state file plus the connector re-publishing
covers single restarts of either side and a double restart); per-reason hold times beyond the
two classes; a live change of hold times without a profile digest change; `DOMAIN_MAP_MISMATCH`
semantics; `fill` and `rr` (unchanged).

## 11. Decisions taken

1. Unknown means neutral, known-bad means deny for its hold — Sergey, 2026-09-29.
2. Holds: 20 min critical, 10 min otherwise, from the observation time, in the connector
   profile — Sergey, 2026-09-29.
3. The connector persists its verdicts in a state file — Sergey, 2026-09-29.
4. The hold logic lives in the connector (policy owner); the MDS keeps a verdict exactly as
   long as the last record said and turns "nothing in force" into neutral.
5. Recovery hold-down applies only to leaving a deny.
