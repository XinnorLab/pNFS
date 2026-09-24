# Placement modes — Stage B (`smart`: connector client, binding check, readiness, preflight) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `placement_mode = smart` real: the MDS polls the per-MDS `lattice-ds-connector` over its Unix socket, validates every batch and every assessment's binding, keeps an immutable assessment view with TTL on the MDS clock, and the gate admits a DS only with a fresh `VALID`/`allowed`/`ppm > 0` assessment — never falling back to `rr`/`fill`. The connector gains a read-only `preflight`.

**Architecture:** `src/mds/ds_connector.{c,h}` = pure batch parser/validator (jsmn) + per-instance/per-DS state + a background UDS poll thread that publishes `struct placement_assessment_view` into the gate singleton; `placement_gate.c` gains the smart candidate rule, connector-sourced domains, manual base weights and the four readiness facts; `config.c` parses the `ds_connector_*` keys; `main.c` starts/stops the client in smart. Stage A's create boundary and selection sites need no change (they consume the view through `placement_candidates`).

**Tech Stack:** C11 (gcc 11, `-Werror=pedantic`), CMake option `ENABLE_DS_CONNECTOR` (ON in our builds), vendored `deps/jsmn/jsmn.h` (MIT, zserge/jsmn @ 25647e6, `JSMN_STATIC` + `JSMN_PARENT_LINKS` + `JSMN_STRICT`), POSIX sockets/poll/pthread, upstream `ASSERT_*/RUN_TEST` tests. Python 3.9 stdlib for the connector `preflight`.

**Spec:** `docs/superpowers/specs/2026-09-23-placement-modes-design.md` §4 (connector keys), §5 (smart weights), §7 (client, binding, readiness), §9, §11, §13 rows "connector client", "readiness/verify". Requirements LAT-03..06, LAT-22..24, MODE-08..10, review findings 2 and 4.

## Global Constraints

- Fork `XinnorLab/pnfs-lattice`, branch `xinnor/placement-modes` (base ced2043 + Stage A). Commits Conventional, English, `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Legacy, `rr` and `fill` behaviour stays exactly as shipped by Stage A (the Stage A suites keep passing unchanged).
- Contract constants (from `connectors/lattice-ds-connector/contracts/connector-batch.schema.json`): `contract_version` = `"1.0"`; assessment `scope` = `"NEW_ALLOCATION"`, `access_scope_id` = `"cluster-default"`, `quality` ∈ {`VALID`,`UNKNOWN`}, `multiplier_ppm` 0..1000000, `endpoint.protocol` = `"NFS"`, `endpoint.transport` ∈ {TCP,RDMA}, `endpoint.port` 1..65535; instance `snapshot_status` ∈ {COMPLETE,PARTIAL,FAILED}; batch ≤ 4 MiB.
- Endpoint rule: `endpoint.server == registry.host`, `endpoint.port == registry.tcp_port` when both non-zero, and `registry.export_path` equals `endpoint.export_path` **or lies under it** (the lab registers `192.168.64.51:/mnt/data/pnfs-ds` inside the share `/mnt/data`).
- Hot path never touches the socket: placement reads the published view only (LAT-04). The poll thread is the only writer.
- No selector/creation bypass is introduced: the CI proofs of Stage A stay green; `grep -n "socket(" src/fsal_obj/` finds nothing.
- Build/test loop: `mds/scripts/pm-run.sh` (now configures with `-DENABLE_DS_CONNECTOR=ON`); CI workflow, manifest and docs list the flag.
- Stand (Task B7) restarts `pnfs-mds` on both MDS and stops `xinas-agent` / the connector on purpose; announced before it runs.

---

## File structure

| File | Responsibility |
|---|---|
| `deps/jsmn/jsmn.h` (new, vendored) | JSON tokenizer |
| `include/ds_connector.h`, `src/mds/ds_connector.c` (new) | batch parse + validate (pure), per-instance sequence/epoch, per-DS binding pins, digest pins, assessment view build, UDS HTTP client, poll thread, readiness facts, metrics |
| `include/placement_gate.h`, `src/fsal_obj/placement_gate.c` | `struct placement_assessment_view` + smart candidate rule, connector domains, manual base weights, `placement_gate_publish_assessments`, readiness in `placement_gate_ds_status`/new `placement_gate_readiness` |
| `include/placement_modes.h`, `include/pnfs_mds.h`, `src/common/config.c`, `src/common/placement_config.c`, `docs/config-keys.md` | `ds_connector_*` keys, defaults, validation, generation |
| `src/mds/main.c` | start/stop the client in smart; startup line |
| `src/cluster/cluster_transport.c` | `config show`: readiness keys + assessment columns |
| `src/common/mds_metrics.c`, `include/mds_metrics.h` | connector counters/gauges |
| `CMakeLists.txt`, `src/mds/CMakeLists.txt`, `tests/CMakeLists.txt`, `.github/workflows/placement-modes.yml` | option, sources, tests, CI flag |
| `tests/unit/test_ds_connector.c` (new), `test_placement_gate.c`, `test_placement_config.c`, `test_cluster_transport.c` (extended) | tests |
| `docs/placement-modes.md`, `docs/examples/mds.conf.smart` | operator docs |
| XinnorLab/pNFS: `connectors/lattice-ds-connector/lattice_ds_connector/cli.py` (+`preflight.py`), tests, README; `docs/placement-modes/contract-manifest.json`, `mds/manifest.json`, `mds/scripts/pm-run.sh`, `docs/TODO.md`, stand report | connector preflight, manifests, loop, docs |

---

### Task B1: `ENABLE_DS_CONNECTOR`, jsmn, connector config keys

**Files:**
- Create: `deps/jsmn/jsmn.h` (from zserge/jsmn @ 25647e6, MIT header kept), `deps/jsmn/README` (source, commit, licence)
- Modify: `CMakeLists.txt` (option + `add_compile_definitions(ENABLE_DS_CONNECTOR=1)` + include dir `deps`), `include/placement_modes.h`, `include/pnfs_mds.h`, `src/common/config.c`, `src/common/placement_config.c`, `docs/config-keys.md`, `tests/unit/test_placement_config.c`, `mds/scripts/pm-run.sh` (pNFS), `.github/workflows/placement-modes.yml`

**Interfaces:**
- Produces (`placement_modes.h`):

```c
#define PM_KEY_CONN_ENABLED          "ds_connector_enabled"
#define PM_KEY_CONN_SOCKET           "ds_connector_socket"
#define PM_KEY_CONN_POLL_MS          "ds_connector_poll_ms"
#define PM_KEY_CONN_DEADLINE_MS      "ds_connector_request_deadline_ms"
#define PM_KEY_CONN_CONTRACT_MAJOR   "ds_connector_expected_contract_major"
#define PM_KEY_CONN_MAX_DS           "ds_connector_max_ds"
#define PM_KEY_CONN_ACCESS_SCOPE     "ds_connector_access_scope"
#define PM_KEY_CONN_PROFILE_DIGEST   "ds_connector_expected_profile_digest"
#define PM_KEY_CONN_CONFIG_DIGEST    "ds_connector_expected_config_digest"
#define PM_DEFAULT_CONN_SOCKET       "/run/lattice-ds-connector/connector.sock"
#define PM_DEFAULT_CONN_POLL_MS      1000u      /* 200..10000 */
#define PM_DEFAULT_CONN_DEADLINE_MS  500u       /* 50..poll */
#define PM_DEFAULT_CONN_CONTRACT_MAJOR 1u
#define PM_DEFAULT_CONN_MAX_DS       256u       /* 1..256 */
#define PM_DEFAULT_CONN_ACCESS_SCOPE "cluster-default"
#define PM_CONN_SCOPE                "NEW_ALLOCATION"
#define PM_DIGEST_MAX                128
```

- Produces (`struct mds_config`): `bool ds_connector_enabled, ds_connector_enabled_set; char ds_connector_socket[MDS_MAX_PATH]; uint32_t ds_connector_poll_ms, ds_connector_request_deadline_ms, ds_connector_expected_contract_major, ds_connector_max_ds; char ds_connector_access_scope[64]; char ds_connector_expected_profile_digest[PM_DIGEST_MAX]; char ds_connector_expected_config_digest[PM_DIGEST_MAX];`
- Validation (`placement_config_validate`): `smart` ⇒ `ds_connector_enabled` derived true; explicit `ds_connector_enabled = false` with smart → `PLACEMENT_MODE_CONFLICT`; explicit `= true` with rr/fill/legacy → `PLACEMENT_MODE_CONFLICT`; socket must be absolute and ≤ MDS_MAX_PATH; poll 200..10000; deadline 50..poll (`RANGE`); max_ds 1..256; `#ifndef ENABLE_DS_CONNECTOR` keeps `PLACEMENT_MODE_UNSUPPORTED_BUILD`. Generation appends `conn_socket=`, `conn_poll=`, `conn_deadline=`, `conn_major=`, `conn_max_ds=`, `conn_scope=`, `conn_profile=`, `conn_config=` lines when the mode is smart.

- [ ] **Step 1: Failing tests** (`test_placement_config.c`): `test_smart_parses_with_connector_defaults` (mode smart → OK, enabled derived true, socket default, poll 1000, deadline 500, major 1, max_ds 256, scope "cluster-default"); `test_smart_connector_conflicts` (smart + `ds_connector_enabled = false` → INVAL; `rr` + `ds_connector_enabled = true` → INVAL; smart + `default_mirror_count = 2` → INVAL; smart + `ds_capacity_poll_ms = 0` → INVAL); `test_connector_ranges` (`ds_connector_poll_ms = 100` / `20000`, deadline `2000` with poll 1000, `ds_connector_max_ds = 0`, relative socket path → INVAL; poll 5000 + deadline 4000 → OK); `test_generation_covers_connector_keys` (two smart configs differing only in the socket path → different generation; rr with a connector socket key → conflict); update `test_smart_is_unsupported_in_this_build` → renamed `test_smart_is_supported_in_this_build` asserting MDS_OK.
- [ ] **Step 2: Run** `mds/scripts/pm-run.sh test_placement_config` → BUILD_FAILED / FAIL.
- [ ] **Step 3: Implement** the keys in `config.c` (strict numbers via `parse_u64_strict`, presence flag for `ds_connector_enabled`), defaults (`socket`, poll, deadline, major, max_ds, scope), validation and generation in `placement_config.c`, CMake option (default OFF upstream-style, our loop/CI pass ON), jsmn vendoring, `docs/config-keys.md` rows.
- [ ] **Step 4: Run** `mds/scripts/pm-run.sh "test_placement_config|test_config"` → PASS.
- [ ] **Step 5: Commit** `feat(config): ds_connector_* keys for placement_mode = smart; vendor jsmn; ENABLE_DS_CONNECTOR option`.

### Task B2: Batch parser, binding pins, assessment view (pure)

**Files:**
- Create: `include/ds_connector.h`, `src/mds/ds_connector.c` (pure half), `tests/unit/test_ds_connector.c`
- Modify: `src/mds/CMakeLists.txt`, `tests/CMakeLists.txt`, `include/placement_gate.h` (the view type)

**Interfaces:**

```c
/* placement_gate.h */
#define PA_REASONS_MAX 4
struct placement_assessment_row {
    uint32_t ds_id;
    bool     present;          /* a record was accepted for this ds */
    bool     valid;            /* quality == VALID */
    bool     allowed;
    uint32_t multiplier_ppm;
    uint64_t expires_mono_ms;  /* receive time + remaining_ttl_ms */
    uint64_t received_mono_ms;
    char     domain[PM_DOMAIN_ID_MAX];   /* resources.capacity_domain_id or "" */
    char     reasons[PA_REASONS_MAX][32];
    uint32_t reason_count;
};
struct placement_assessment_view {
    uint32_t count;
    struct placement_assessment_row rows[MDS_MAX_DS_NODES];
    bool     batch_valid;      /* last envelope accepted */
    uint64_t batch_received_mono_ms;
    char     config_digest[PM_DIGEST_MAX];
    char     profile_digest[PM_DIGEST_MAX];
};

/* ds_connector.h */
struct ds_connector_registry_ds { uint32_t ds_id; char host[MDS_DS_HOST_MAX]; char export_path[MDS_DS_EXPORT_MAX]; uint16_t tcp_port; };
struct ds_connector_registry { uint32_t count; struct ds_connector_registry_ds ds[MDS_MAX_DS_NODES]; };
struct ds_connector_cfg { uint32_t contract_major; uint32_t max_ds; char access_scope[64]; char expected_profile_digest[PM_DIGEST_MAX]; char expected_config_digest[PM_DIGEST_MAX]; };
struct ds_connector_pin {           /* per ds_id, first accepted tuple */
    bool     pinned;
    char     instance[128]; uint32_t binding_generation;
    char     datastore_id[128]; char target_id[128]; char target_incarnation[128];
    char     profile_digest[PM_DIGEST_MAX]; char access_scope[64];
};
struct ds_connector_instance_seq { char id[128]; char epoch[128]; uint64_t sequence; bool used; };
struct ds_connector_state {
    struct ds_connector_cfg cfg;
    char     runtime_epoch[128];
    struct ds_connector_instance_seq inst[64];
    struct ds_connector_pin pins[MDS_MAX_DS_NODES];
    uint64_t last_generated_at_ms;   /* parsed ISO-8601 UTC, 0 = none */
};
enum ds_connector_drop { DC_OK = 0, DC_JSON, DC_SCHEMA, DC_CONTRACT_MAJOR, DC_REPLAY, DC_OLD_GENERATED_AT, DC_CONFIG_DIGEST, DC_TOO_LARGE, DC_COUNT };
struct ds_connector_report { enum ds_connector_drop drop; uint32_t accepted, rejected_binding, rejected_shape, unknown_ds; char detail[160]; };

/* Parses and validates one batch. On DC_OK fills `out` (every registered DS
 * gets a row; present=false for those without an accepted record) and
 * advances the state (sequence lines, pins). Any other drop leaves the
 * state untouched and `out` empty (batch_valid=false). */
enum ds_connector_drop ds_connector_apply_batch(struct ds_connector_state *st,
                                                const char *text, size_t len,
                                                const struct ds_connector_registry *reg,
                                                uint64_t now_mono_ms,
                                                struct placement_assessment_view *out,
                                                struct ds_connector_report *rep);
const char *ds_connector_drop_name(enum ds_connector_drop d);
```

Rules (spec §7, review finding 2):
1. Envelope: JSON object; `contract_version` string whose major (before the first `.`) equals `cfg.contract_major` else `DC_CONTRACT_MAJOR`; `runtime_epoch` — different from `st->runtime_epoch` ⇒ reset every instance line and every pin (log INFO), then continue; `generated_at` parsed (`YYYY-MM-DDTHH:MM:SS[.fff]Z`) must be ≥ `last_generated_at_ms` else `DC_OLD_GENERATED_AT`; `config_digest` equals `cfg.expected_config_digest` when set else `DC_CONFIG_DIGEST`; `instances` array ≤ 64.
2. Instance: `connector_instance_id`, `epoch`, `sequence` (uint); same epoch ⇒ sequence must be strictly greater than the stored one else `DC_REPLAY` (whole batch); new epoch ⇒ line reset. `snapshot_status` ∈ enum. Records under an instance with `snapshot_status = FAILED` are all treated as `quality = UNKNOWN`.
3. Assessment (per record; a failure rejects only that record, counted in `rep->rejected_binding` or `rejected_shape`): `ds_id` < max_ds and in the registry (else `unknown_ds`, ignored); `scope == "NEW_ALLOCATION"`, `access_scope_id == cfg.access_scope`; endpoint rule (Global Constraints); `profile.digest == cfg.expected_profile_digest` when set, and identical across every record of the batch; `quality` enum; `placement.allowed` bool; `multiplier_ppm` 0..1e6; `remaining_ttl_ms` uint ≤ 3600000; `resources.capacity_domain_id` string or null; `reason_codes` array of strings (first 4 kept, 31 chars each). Pin: first accepted tuple per ds_id `(instance, binding_generation, datastore_id, target_id, target_incarnation, profile.digest, access_scope_id)`; later record must equal the tuple, or carry a strictly higher `binding_generation` (re-pin, row `present` but `valid=false` with reason `REBOUND` for this batch); lower generation or same generation with any other field changed ⇒ `BINDING_MISMATCH` (record rejected, row present=false).
4. Duplicate `ds_id` inside one batch ⇒ both rejected (`rejected_shape`).
5. View rows: `expires_mono_ms = now + remaining_ttl_ms`, `received_mono_ms = now`, `valid = quality==VALID && snapshot != FAILED`, `allowed`, `multiplier_ppm`, `domain`.

- [ ] **Step 1: Failing tests** (`test_ds_connector.c`; batches as C string literals built by a helper `mk_batch(const char *epoch, unsigned seq, const char *records)`): accepts a healthy batch (row present/valid/allowed/ppm/domain/expiry); rejects contract major 2; replay (same epoch, seq ≤ last) drops the batch and leaves the previous pins; new instance epoch resets the line; new runtime_epoch resets pins (a changed tuple is then accepted as a fresh pin); older `generated_at` dropped; config digest pin mismatch dropped, match accepted; profile digest mismatch rejects the record; endpoint server / export_path (both exact and "under") / port rules; scope / access scope constants; binding mismatch (same generation, different target_id) rejects the record; higher generation re-pins; lower generation rejected; unknown ds ignored and counted; duplicate ds rejected; FAILED snapshot ⇒ rows invalid; malformed JSON ⇒ DC_JSON; 4 MiB + 1 ⇒ DC_TOO_LARGE; `remaining_ttl_ms` past bound ⇒ shape reject.
- [ ] **Step 2: Run** → BUILD_FAILED.
- [ ] **Step 3: Implement** with jsmn (`JSMN_PARENT_LINKS` navigation helpers: `tok_get(obj, "key")`, `tok_str_eq`, `tok_u64`, `tok_bool`, `tok_str_copy`), a 64 K-token budget (heap), `iso8601_to_ms` (strict), and the rules above. Pure: no sockets, no globals except the `st` passed in.
- [ ] **Step 4: Run** `mds/scripts/pm-run.sh test_ds_connector` → PASS.
- [ ] **Step 5: Commit** `feat(ds_connector): batch parser with envelope, sequence, digest and per-DS binding validation (pure)`.

### Task B3: UDS client, poll thread, publish, readiness facts

**Files:**
- Modify: `include/ds_connector.h`, `src/mds/ds_connector.c` (I/O half), `include/placement_gate.h`, `src/fsal_obj/placement_gate.c` (`placement_gate_publish_assessments`, `placement_gate_readiness`), `include/mds_metrics.h`, `src/common/mds_metrics.c`, `tests/unit/test_ds_connector.c`

**Interfaces:**

```c
/* ds_connector.h */
struct ds_connector_facts {
    bool     mode_active;             /* gate mode == smart */
    bool     config_valid;            /* fixed at start */
    bool     reachable;               /* a successful poll within 3 x poll_ms */
    bool     last_batch_valid;
    uint64_t last_success_mono_ms;    /* 0 = never */
    uint32_t registered_ds, covered_ds, eligible_ds;   /* covered = fresh valid row */
    enum ds_connector_drop last_drop;
    char     last_error[160];
    char     config_digest[PM_DIGEST_MAX];
    char     profile_digest[PM_DIGEST_MAX];
};
/* One HTTP GET over the Unix socket; body malloc'd on 200; MDS_ERR_IO on
 * connect/timeout/protocol errors, MDS_ERR_AGAIN on 503, MDS_ERR_INVAL on
 * other statuses. Deadline covers connect + full read. */
enum mds_status ds_connector_http_get(const char *socket_path, const char *path,
                                      uint32_t deadline_ms, char **body, size_t *len,
                                      int *http_status);
int  ds_connector_start(const struct mds_config *cfg, struct ds_cache *cache);   /* smart only; 0 ok */
void ds_connector_stop(void);
void ds_connector_facts(struct ds_connector_facts *out);
/* Test hook: run one poll cycle synchronously (no thread). */
enum mds_status ds_connector_poll_once(void);

/* placement_gate.h */
void placement_gate_publish_assessments(const struct placement_assessment_view *view);  /* copies, refcounted like the capacity view */
struct placement_readiness { bool mode_active, connector_config_valid, connector_reachable, last_batch_valid; uint32_t registered_ds, covered_ds, eligible_ds; char coverage[8]; /* full|partial|none|n/a */ };
void placement_gate_readiness(struct placement_readiness *out);
```

Poll thread: pipe + `poll()` like `ds_io_limits`; every `poll_ms`: GET, `ds_connector_apply_batch` against the registry snapshot taken from the DS cache (`ds_cache_capacity_view` rows → host/export/port via `ds_cache_get`), publish the view on `DC_OK`; on any failure keep the previous view (its rows age out by TTL — no refresh) and record `last_error`/`last_drop`; metrics `connector_poll_errors_total{code}`, `connector_batches_dropped_total{reason}`, `connector_last_success_age_ms` gauge, `connector_covered_ds` gauge.

- [ ] **Step 1: Failing tests**: an in-test Unix-socket HTTP server thread (`socket/bind/listen/accept`, replies with a canned status + body, closes) — `test_http_get_200_body`, `test_http_get_503_is_again`, `test_http_get_timeout` (server accepts and sleeps 2 s, deadline 200 ms → MDS_ERR_IO within 400 ms), `test_http_get_no_socket` (ENOENT → MDS_ERR_IO), `test_http_get_oversize` (Content-Length 5 MiB → MDS_ERR_IO); `test_poll_once_publishes_and_facts` (start with a cache holding DS 0/1, server serving a batch for ds 0 → `placement_gate_readiness` covered_ds 1, coverage partial; then server returns garbage → facts.last_batch_valid false, previous rows still present until TTL); `test_no_fallback_when_socket_gone` (unlink socket → reachable false after 3 × poll, rows expire, eligible 0).
- [ ] **Step 2: Run** → BUILD_FAILED.
- [ ] **Step 3: Implement**: HTTP/1.1 minimal client (`GET %s HTTP/1.1\r\nHost: connector\r\nConnection: close\r\n\r\n`, `poll()`-bounded reads into a growing buffer ≤ 4 MiB + headers, parse status line, `Content-Length`, body after `\r\n\r\n`); thread; publish; facts under a mutex; gate: refcounted assessment view (same pattern as the capacity view), `placement_gate_readiness` computed from both views.
- [ ] **Step 4: Run** `mds/scripts/pm-run.sh "test_ds_connector|test_placement_gate"` → PASS.
- [ ] **Step 5: Commit** `feat(ds_connector): Unix-socket HTTP client, poll thread, published assessment view, readiness facts, metrics`.

### Task B4: `smart` in the gate — candidate rule, weights, domains, config show

**Files:**
- Modify: `src/fsal_obj/placement_gate.c`, `include/placement_gate.h`, `src/cluster/cluster_transport.c`, `tests/unit/test_placement_gate.c`, `tests/unit/test_cluster_transport.c`

**Rules:**
- In smart, `candidates_weighted` requires `ctx->assess != NULL` (else every DS `PR_MODE_NOT_READY`); per listed DS after the capacity gate: row absent → `PR_NO_BINDING`; `!valid` → `PR_ASSESSMENT_UNKNOWN`; `now ≥ expires` → `PR_ASSESSMENT_STALE`; `!allowed` → `PR_CONNECTOR_DENIED`; `ppm == 0` → `PR_ZERO_MULTIPLIER`. Reason order after the capacity checks (a full domain is CAPACITY_FULL first).
- Domain in smart: the row's `domain` when non-empty; a declared `ds_capacity_domain.<id>` that differs → `PR_DOMAIN_MAP_MISMATCH` (new reason, add to the enum/names/manifest); empty connector domain ⇒ the DS is its own domain.
- Weight: `placement_weight(domain_weight, ppm, N)` where `domain_weight` = manual `placement_domain_weight.<domain>` if `placement_allow_manual_base_weights` and an entry exists, else the derived fill level. Manual weights reach the ctx via `ctx->domain_weights` (id/weight arrays copied from the config at init).
- `placement_gate_ds_status` gains `assessment_age_ms` (UINT64_MAX none), `quality`, `allowed`, `ppm`; `config show` prints them plus `placement_readiness = mode_active=… connector_config_valid=… connector_reachable=… last_batch_valid=… coverage=… covered_ds=… eligible_ds=…` and `placement_connector_config_digest`, `placement_connector_profile_digest`.

- [ ] **Step 1: Failing tests** (`test_placement_gate.c`, synthetic assessment views via a helper `add_assess(id, valid, allowed, ppm, ttl, domain)`): smart without view → MODE_NOT_READY; healthy row → candidate with `weight == placement_weight(dw, ppm, N)`; denied / UNKNOWN / expired / ppm 0 / no row → the five reasons; degraded 250000 ppm vs healthy at equal capacity → ≈ 1:4 over 100 000 draws (seed); connector domain groups two DS (N=2) and a contradicting `ds_capacity_domain` → `DOMAIN_MAP_MISMATCH`; manual base weight 300 with the flag → weight(300, ppm, N), without the flag ignored; `test_cluster_transport.c`: config show prints the readiness key and assessment columns.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** `"test_placement_gate|test_cluster_transport|test_placement_create_boundary"` → PASS. **Step 5: Commit** `feat(placement): smart candidate rule, connector domains, manual base weights, readiness in config show`.

### Task B5: `main.c` wiring and the startup line

**Files:** `src/mds/main.c`, `src/fsal_obj/placement_gate.c` (init: smart requires `ENABLE_DS_CONNECTOR`, cache, kernel), `tests/unit/test_placement_gate.c` (init rules)

- Start `ds_connector_start(&cfg, ds_cache)` right after `placement_gate_init` when `cfg.placement_mode == PM_SMART`; failure is fatal (`exit_code = EXIT_FAILURE; goto cleanup`). Stop before `placement_gate_destroy`. Startup line gains `connector=<socket> poll=<ms> deadline=<ms>`.
- [ ] Tests: `placement_gate_init` with smart and no cache → -1; smart with cache → 0 and `placement_gate_readiness().mode_active == true`, `coverage == "none"` before any batch.
- [ ] Run `"test_placement_gate"`, then the full suite. Commit `feat(mds): start the connector client in placement_mode = smart`.

### Task B6: Connector `preflight`, docs, manifests, CI flag

**Files (pNFS):** `connectors/lattice-ds-connector/lattice_ds_connector/preflight.py` (new), `cli.py`, `tests/test_preflight.py` (new), `README.md`, `docs/compatibility-manifest.json`; `docs/placement-modes/contract-manifest.json` (stage B keys → implemented, `DOMAIN_MAP_MISMATCH`, endpoint rule), `mds/manifest.json` (flag), `mds/scripts/pm-run.sh`, `docs/TODO.md`. **Fork:** `docs/placement-modes.md` (smart section, readiness, reasons), `docs/examples/mds.conf.smart`, `.github/workflows/placement-modes.yml` (`-DENABLE_DS_CONNECTOR=ON` in both jobs).

`lattice-ds-connector preflight [--socket PATH] [--expect-ds 0,1] [--json]` (read-only): GET `/healthz` and `/v1/assessments`; report per DS id: bound (a record exists), quality, allowed, ppm, remaining_ttl_ms, capacity_domain_id, datastore_id, binding_generation; checks: contract major 1, every `--expect-ds` id bound, no UNKNOWN, no `remaining_ttl_ms == 0`, one profile digest across records, domain consistency (records with one `capacity_domain_id` share one `datastore_id`); prints `READY` / `NOT_READY <reasons>` and exits 0/1; `--json` dumps the facts (`runtime_epoch`, `config_digest`, `profile_digest`, per-DS rows). It never changes anything and never mentions `placement_mode`.

- [ ] Tests (pytest, using the existing UDS server fixture in `tests/test_server.py` style): READY on the healthy fixture; NOT_READY when `--expect-ds` names an unbound id, when a record is UNKNOWN, when two records share a domain with different datastore ids; `--json` shape.
- [ ] Docs + manifests as listed; run `pytest tests -q` (connector) and the full fork suite; commit both repos; export patches.

### Task B7: Stand trial for `smart` (announced; restarts both MDS, stops xinas-agent and the connector on purpose)

Pre-condition: Sergey's go-ahead in chat. The connector runs on node225 (MDS 2) only, so the trial client mounts **MDS 2** and writes under `shard2`; MDS 1 (no connector) is expected to report `coverage=none` and refuse new placements while in smart — that is the per-MDS rule, shown, not hidden. DS 1 (node 71) has no binding → `NO_BINDING`.

1. Deploy `smart` (`ds_connector_socket = /run/lattice-ds-connector/connector.sock`, poll 1000, deadline 500) on both MDS; `config show` on MDS 2: `placement_readiness … coverage=partial covered_ds=1`, `placement_ds.0 … quality=VALID allowed=1 ppm=1000000`, `placement_ds.1 … reason=NO_BINDING`; MDS 1: `connector_reachable=0`.
2. 20 files via MDS 2 → **20 : 0** (all on DS 0).
3. `systemctl stop xinas-agent` on the box → within ~16 s the connector reports `UNKNOWN`/`SOURCE_STALE` → MDS 2 `placement_ds.0 … reason=ASSESSMENT_UNKNOWN`, 10 files → every CREATE/LAYOUTGET `NFS4ERR_NOSPC` (client sees ENOSPC), `rejections_total{reason="ASSESSMENT_UNKNOWN"}` grows, **no file lands anywhere**; `systemctl start xinas-agent` → hold-down → `VALID` → 10 files → 10 : 0.
4. `systemctl stop lattice-ds-connector` on node225 → `connector_reachable=0` after 3 s, rows expire after their TTL (≤ 20 s) → refusals continue (no fallback); start it → recovery.
5. `pm-deploy.sh legacy` → 100 files ≈ 55 : 45 via MDS 1 (regression).
6. Report `docs/placement-modes/stand-2026-09-24.md`; the stand stays on legacy.

## Self-review

- Spec coverage: §4 connector keys (B1), §5 smart weights + manual base weights (B4), §7 poll/validation/binding/cache/readiness/candidate rule/domain identity (B2–B4), §9 startup line + config show + metrics (B3–B5), §11 preflight (B6), §13 connector-client and readiness rows (B2–B4 tests), review findings 2/4 (readiness split, binding tuple). Deferred to Stage C: CLI `verify` semantics over the readiness facts, perf row.
- Placeholders: none; the batch rules are enumerated, the HTTP framing is literal, the facts struct is defined.
- Type consistency: `placement_assessment_view`/`_row` used by B2 (producer), B3 (publish), B4 (consumer); `ds_connector_facts` → `placement_readiness` mapping in B3/B4; `ds_connector_apply_batch` signature used in B2 tests and B3 poll.
