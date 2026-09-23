# Placement modes — Stage A (`rr` / `fill`, gate, kernel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land `placement_mode = rr | fill` in the MDS fork with the candidate gate at every selection site, the admission check at the create-if-absent boundary, the capacity observation record, the hardened WRR kernel, metrics/config-show, docs and CI — leaving `smart` parsed but refused until Stage B.

**Architecture:** One new module `placement_gate.{h,c}` (pure selection + a process singleton holding the effective mode and an atomically published capacity view) sits in front of the upstream selectors; `proxy_io.c` splits lookup from create and asks the gate before any `O_CREAT`; `ds_capacity.c` records `avail`/`fsid`/monotonic time per successful `statvfs`; `config.c` parses and validates the new keys and computes the config generation. Legacy behaviour (no `placement_mode`) stays byte-for-byte.

**Tech Stack:** C11 (gcc 11), CMake 3.31, upstream's own `ASSERT_*/RUN_TEST` unit-test style (no cmocka), OpenSSL SHA-256, Linux only. Builds and tests run on node225 (`/home/lattice/pnfs-lattice-pm`, Release, `-DENABLE_WRR=ON -DENABLE_DS_PREALLOC=OFF -DENABLE_RONDB=ON -DENABLE_TESTS=ON -DENABLE_EBPF=OFF`).

**Spec:** `docs/superpowers/specs/2026-09-23-placement-modes-design.md` (revision 2, commit `aa60a1d`) — §4 config, §5 gate, §5a create boundary, §6 capacity, §8 kernel, §9 observability, §12 packaging, §13 tests. Stage A = everything except §7 (`smart`, connector client), §10 (CLI), §11 (preflight).

## Global Constraints

- Fork `XinnorLab/pnfs-lattice`, branch `xinnor/placement-modes`, base `6b4dcde`. Commits are Conventional Commits, English, `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer. Never rewrite the base.
- Without `placement_mode` the daemon behaves exactly as upstream: no new log lines at INFO, same selectors, same config defaults (`placement_policy_enabled=false`, `ds_capacity_poll_ms=60000`).
- `placement_mode = smart` parses but fails validation with `PLACEMENT_MODE_UNSUPPORTED_BUILD` until Stage B adds `ENABLE_DS_CONNECTOR`.
- Weights: `weight = domain_weight × ppm × 65536 / N`, `ppm = 1000000` in `fill`, computed in `unsigned __int128`, asserted `< 2^62`; manual override range 1..10000; `N ≤ 256`.
- No selector runs on an un-gated list: after Task 6, `grep -n "placement_select" src/` lists only `placement.c`, `placement_gate.c`, `placement_select_replacement`, `placement_select_for_tier`. After Task 7, `grep -n O_CREAT src/mds/proxy_io.c` lists only `mds_proxy_create_ds_file*` and the fd-cache flag constant.
- The lab MDS (`pnfs-mds` on node223/node225) is **not** restarted by any task except Task 10, which is announced to Sergey first.
- Build/test loop: `mds/scripts/pm-run.sh` in XinnorLab/pNFS (Task 0) syncs the local working tree to node225 and runs the named test targets; every "Run" step below uses it.

---

## File structure

Fork (`~/Documents/GitHub/pnfs-lattice`):

| File | Responsibility |
|---|---|
| `include/placement_modes.h` (new) | `enum placement_mode`, key names/defaults/ranges as macros (mirror of the contract manifest), reason enum + names |
| `src/common/config.c`, `include/pnfs_mds.h`, `docs/config-keys.md` | parse + validate the new keys, conflict rules, `placement_config_generation` |
| `src/common/placement_config.c` (new) | `placement_config_validate(cfg, err, cap)` and `placement_config_generation(cfg, out)` — pure functions over `struct mds_config`, unit-tested without the parser |
| `include/placement_gate.h`, `src/fsal_obj/placement_gate.c` (new) | views, candidates, `placement_admit`, fixed-point weights, singleton, create-admission tokens, metrics counters |
| `src/modules/wrr/wrr.c` (vendored), `wrr_stub.c`, `include/wrr.h` | real kernel, `mds_wrr_kernel_id`, `mds_wrr_weighted_pick2`, test seed hook |
| `src/mds/ds_cache.c`, `include/ds_cache.h` | `struct ds_capacity_obs` per entry, `ds_cache_set_capacity_obs`, `ds_cache_note_capacity_failure`, `ds_cache_capacity_view` |
| `src/mds/ds_capacity.c` | probe records the observation (avail, fsid, mono ms) and publishes the gate's capacity view after each sweep |
| `src/mds/proxy_io.c`, `include/proxy_io.h` | `mds_proxy_lookup_ds_file_fh`, `mds_proxy_create_ds_file[_fh]` (token), ensure wrappers |
| `src/mds/compound_layout.c`, `src/mds/compound_data_io.c`, `src/modules/ds_prealloc/ds_prealloc_stub.c` | selection sites → `placement_admit` |
| `src/mds/main.c` | gate init/destroy, startup line |
| `src/common/mds_metrics.c`, `include/mds_metrics.h` | rejection counters, eligible gauge, mode gauge |
| `src/cluster/cluster_transport.c` | `render_cfg_placement` gains mode/generation/per-DS rows |
| `tests/unit/test_wrr_kernel.c`, `test_placement_config.c`, `test_placement_gate.c`, `test_placement_create_boundary.c` (new); `test_ds_capacity.c`, `test_compound.c` (extended); `tests/CMakeLists.txt` | tests |
| `docs/placement-modes.md`, `docs/examples/mds.conf.{rr,fill}` (new) | operator docs |
| `.github/workflows/placement-modes.yml` (new) | build with the required flags, ctest, grep proofs, kernel proof |

XinnorLab/pNFS: `mds/scripts/pm-run.sh`, `mds/manifest.json`, `docs/placement-modes/contract-manifest.json`, `scripts/export-patches.sh`, `mds/patches/6b4dcde/*.patch` (Task 9).

---

### Task 0: Build loop on node225 and baseline

**Files:**
- Create: `~/Documents/GitHub/pNFS/mds/scripts/pm-run.sh`
- Create on node225: `/home/lattice/pnfs-lattice-pm` (clone of the fork), `build/`

**Interfaces:**
- Produces: `pm-run.sh [--no-sync] <ctest-regex|all|build>` — syncs the local fork working tree (tracked-modified + untracked files) over `ssh xinas-box → ssh root@192.168.65.225`, applies it onto the node225 clone at the same base commit, builds, runs the tests matching the regex, prints `PM_RESULT <n passed>/<n total>`.

- [ ] **Step 1: Write the sync/build script**

```bash
#!/bin/bash
# pm-run.sh -- build the placement-modes fork on node225 and run tests.
# Usage: pm-run.sh [--no-sync] <ctest-regex|all|build>
set -u
FORK=${FORK:-$HOME/Documents/GitHub/pnfs-lattice}
REMOTE=/home/lattice/pnfs-lattice-pm
SYNC=1; [ "${1:-}" = "--no-sync" ] && { SYNC=0; shift; }
WHAT=${1:-all}
BASE=$(git -C "$FORK" rev-parse HEAD)
TAR=/tmp/pm-sync.tgz
if [ $SYNC = 1 ]; then
  ( cd "$FORK" && git ls-files -m -o --exclude-standard -z | tar --null -T - -czf "$TAR" ) || exit 1
  for i in 1 2 3; do scp -q -o ConnectTimeout=25 -o BatchMode=yes "$TAR" xinas-box:/root/pm-sync.tgz && break; sleep 8; done
fi
ssh -o ConnectTimeout=25 -o BatchMode=yes xinas-box "scp -q -o BatchMode=yes /root/pm-sync.tgz root@192.168.65.225:/root/pm-sync.tgz && ssh -o BatchMode=yes root@192.168.65.225 'set -e
cd $REMOTE
if [ \"\$(git rev-parse HEAD)\" != \"$BASE\" ]; then git fetch -q origin && git checkout -q -f $BASE; fi
git checkout -q -- . && git clean -qfd -e build
[ $SYNC = 1 ] && tar xzf /root/pm-sync.tgz -C $REMOTE
[ -f build/CMakeCache.txt ] || cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_RONDB=ON -DRonDB_ROOT=/opt/rondb -DENABLE_EBPF=OFF -DENABLE_TESTS=ON -DENABLE_WRR=ON -DENABLE_DS_PREALLOC=OFF >/dev/null
cmake --build build -j32 2>&1 | grep -E \"error|warning: |Error\" | head -40 || true
cmake --build build -j32 >/dev/null || { echo PM_RESULT BUILD_FAILED; exit 1; }
case \"$WHAT\" in build) echo PM_RESULT BUILD_OK;; all) ctest --test-dir build -j16 2>&1 | tail -6;; *) ctest --test-dir build -R \"$WHAT\" --output-on-failure 2>&1 | tail -40;; esac'"
```

- [ ] **Step 2: Create the node225 clone and run the baseline**

Run (via `ssh xinas-box`, then `ssh root@192.168.65.225`): `git clone -q https://github.com/XinnorLab/pnfs-lattice.git /home/lattice/pnfs-lattice-pm && cd /home/lattice/pnfs-lattice-pm && git checkout -q xinnor/placement-modes && cp /home/lattice/pnfs-lattice/src/modules/wrr/wrr.c src/modules/wrr/wrr.c`
Then locally: `mds/scripts/pm-run.sh --no-sync all`
Expected: `PM_RESULT` line absent (all mode prints ctest tail) with `100% tests passed` (64 tests at 6b4dcde).

- [ ] **Step 3: Commit the script in XinnorLab/pNFS**

```bash
cd ~/Documents/GitHub/pNFS && git add mds/scripts/pm-run.sh && git commit -m "chore(mds): node225 build-and-test loop for the placement-modes fork"
```

---

### Task 1: Vendor the WRR kernel; kernel id, pick2, test seed

**Files:**
- Create: `src/modules/wrr/wrr.c` (copy of `pNFS/modules/wrr/wrr.c`), `tests/unit/test_wrr_kernel.c`
- Modify: `include/wrr.h`, `src/modules/wrr/wrr_stub.c`, `tests/CMakeLists.txt`

**Interfaces:**
- Produces: `uint32_t mds_wrr_kernel_id(void)` (0 = stub, `0x58494e01` = XinnorLab kernel v1); `int mds_wrr_weighted_pick2(const uint64_t *w, uint32_t n, uint32_t *out)` returns 0 and sets `*out` on a positive pick, -1 when `n == 0`, `w == NULL`, every weight is zero, or the saturating sum reached `2^62`; `void mds_wrr_test_seed(uint32_t seed)` reseeds the calling thread's PRNG (tests only).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_wrr_kernel.c` (same ASSERT/RUN_TEST macros as `test_config.c`):

```c
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "wrr.h"
/* ... ASSERT_EQ / ASSERT_TRUE / RUN_TEST as in test_config.c ... */
static void test_kernel_id_is_nonzero(void)
{
    ASSERT_EQ(mds_wrr_kernel_id(), 0x58494e01u);
}
static void test_pick2_refuses_empty_and_all_zero(void)
{
    uint64_t z[3] = {0, 0, 0};
    uint32_t out = 99;
    ASSERT_EQ(mds_wrr_weighted_pick2(NULL, 3, &out), -1);
    ASSERT_EQ(mds_wrr_weighted_pick2(z, 0, &out), -1);
    ASSERT_EQ(mds_wrr_weighted_pick2(z, 3, &out), -1);
    ASSERT_EQ(out, 99u);
}
static void test_pick2_never_returns_a_zero_slot(void)
{
    uint64_t w[4] = {0, 5, 0, 5};
    uint32_t out;
    mds_wrr_test_seed(7);
    for (int i = 0; i < 2000; i++) {
        ASSERT_EQ(mds_wrr_weighted_pick2(w, 4, &out), 0);
        ASSERT_TRUE(out == 1 || out == 3);
    }
}
static void test_pick2_distribution_4_to_1(void)
{
    uint64_t w[2] = {80, 20};
    uint32_t out, hits0 = 0;
    mds_wrr_test_seed(12345);
    for (int i = 0; i < 100000; i++) {
        ASSERT_EQ(mds_wrr_weighted_pick2(w, 2, &out), 0);
        if (out == 0) hits0++;
    }
    ASSERT_TRUE(hits0 > 78000 && hits0 < 82000);   /* 80% +- 2% */
}
static void test_pick2_refuses_sum_at_2_pow_62(void)
{
    uint64_t w[2] = {UINT64_C(1) << 61, UINT64_C(1) << 61};
    uint32_t out;
    ASSERT_EQ(mds_wrr_weighted_pick2(w, 2, &out), -1);
}
static void test_seed_is_deterministic(void)
{
    uint64_t w[3] = {1, 2, 3};
    uint32_t a[50], b[50];
    mds_wrr_test_seed(99);
    for (int i = 0; i < 50; i++) (void)mds_wrr_weighted_pick2(w, 3, &a[i]);
    mds_wrr_test_seed(99);
    for (int i = 0; i < 50; i++) (void)mds_wrr_weighted_pick2(w, 3, &b[i]);
    ASSERT_EQ(memcmp(a, b, sizeof(a)), 0);
}
int main(void)
{
    RUN_TEST(test_kernel_id_is_nonzero);
    RUN_TEST(test_pick2_refuses_empty_and_all_zero);
    RUN_TEST(test_pick2_never_returns_a_zero_slot);
    RUN_TEST(test_pick2_distribution_4_to_1);
    RUN_TEST(test_pick2_refuses_sum_at_2_pow_62);
    RUN_TEST(test_seed_is_deterministic);
    printf("%d/%d passed\n", tests_passed, tests_run);
    return tests_passed == tests_run ? 0 : 1;
}
```

Register in `tests/CMakeLists.txt`: `add_executable(test_wrr_kernel unit/test_wrr_kernel.c)` next to `test_placement_policy`, and add `test_wrr_kernel` to the `foreach(test_target …)` list.

- [ ] **Step 2: Run it to verify it fails**

Run: `mds/scripts/pm-run.sh test_wrr_kernel`
Expected: `PM_RESULT BUILD_FAILED` (undefined `mds_wrr_kernel_id`).

- [ ] **Step 3: Implement**

`include/wrr.h` — append:

```c
/* Build identity: 0 in the community stub, non-zero in a real kernel.
 * The MDS refuses fill/smart placement modes when this is 0. */
uint32_t mds_wrr_kernel_id(void);
#define MDS_WRR_KERNEL_XINNOR_V1 0x58494e01u

/* Weighted pick that refuses instead of returning slot 0:
 *   0  -> *out is a slot with weight > 0
 *  -1  -> n == 0, w == NULL, every weight is zero, or the sum of the
 *         weights reaches 2^62 (the sampler's range). */
int mds_wrr_weighted_pick2(const uint64_t *w, uint32_t n, uint32_t *out);

/* Tests only: reseed the calling thread's PRNG. */
void mds_wrr_test_seed(uint32_t seed);
```

`src/modules/wrr/wrr.c` — copy the pNFS file, then: make the seed variables file-scope `static __thread` (move `seed`/`seeded` out of `wrr_rand`), add

```c
void mds_wrr_test_seed(uint32_t s) { seed = s; seeded = 1; }
uint32_t mds_wrr_kernel_id(void) { return MDS_WRR_KERNEL_XINNOR_V1; }

int mds_wrr_weighted_pick2(const uint64_t *w, uint32_t n, uint32_t *out)
{
    uint64_t total = 0, r;
    uint32_t i;
    if (w == NULL || n == 0 || out == NULL) return -1;
    for (i = 0; i < n; i++) {
        if (w[i] > (UINT64_C(1) << 62) - 1 - total) return -1;
        total += w[i];
    }
    if (total == 0) return -1;
    r = wrr_rand_below(total);
    for (i = 0; i < n; i++) {
        if (r < w[i]) { *out = i; return 0; }
        r -= w[i];
    }
    return -1; /* unreachable: r < total */
}
```

`src/modules/wrr/wrr_stub.c` — add `uint32_t mds_wrr_kernel_id(void) { return 0; }`, `int mds_wrr_weighted_pick2(const uint64_t *w, uint32_t n, uint32_t *out) { (void)w; (void)n; (void)out; return -1; }`, `void mds_wrr_test_seed(uint32_t s) { (void)s; }`.

- [ ] **Step 4: Run to verify it passes**

Run: `mds/scripts/pm-run.sh "test_wrr_kernel|test_placement_policy"`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/Documents/GitHub/pnfs-lattice && git add include/wrr.h src/modules/wrr/wrr.c src/modules/wrr/wrr_stub.c tests/unit/test_wrr_kernel.c tests/CMakeLists.txt && git commit -m "feat(wrr): vendor the XinnorLab kernel; kernel id, refusing pick2 and a test seed hook"
```

---

### Task 2: Configuration — keys, validation, generation

**Files:**
- Create: `include/placement_modes.h`, `src/common/placement_config.c`, `tests/unit/test_placement_config.c`
- Modify: `include/pnfs_mds.h` (struct `mds_config`, after `ds_capacity_poll_ms`), `src/common/config.c` (defaults ≈300, parse loop ≈430–1290, tail before `return MDS_OK` ≈1515), `src/common/CMakeLists.txt`, `docs/config-keys.md`, `tests/CMakeLists.txt`

**Interfaces:**
- Produces (`include/placement_modes.h`):

```c
enum placement_mode { PM_LEGACY = 0, PM_RR = 1, PM_FILL = 2, PM_SMART = 3 };
enum placement_shrink { PM_SHRINK_ALLOW = 0, PM_SHRINK_STRICT = 1 };
#define PM_KEY_MODE                 "placement_mode"
#define PM_KEY_CAP_MAX_AGE_MS       "placement_capacity_max_age_ms"
#define PM_KEY_MIN_FREE_BYTES       "placement_min_free_bytes"
#define PM_KEY_DOMAIN_PREFIX        "ds_capacity_domain."
#define PM_KEY_DOMAIN_WEIGHT_PREFIX "placement_domain_weight."
#define PM_KEY_ALLOW_MANUAL         "placement_allow_manual_base_weights"
#define PM_KEY_SHRINK               "placement_stripe_shrink"
#define PM_DEFAULT_CAP_MAX_AGE_MS   120000u
#define PM_CAP_MAX_AGE_MS_MAX       86400000u
#define PM_DOMAIN_ID_MAX            128
#define PM_DOMAIN_WEIGHT_MIN        1u
#define PM_DOMAIN_WEIGHT_MAX        10000u
#define PM_WEIGHT_SCALE             65536u
#define PM_MAX_DOMAINS              MDS_MAX_DS_NODES
const char *placement_mode_name(enum placement_mode m);   /* "legacy"|"rr"|"fill"|"smart" */
```

- Produces (`struct mds_config` additions):

```c
    enum placement_mode   placement_mode;          /* PM_LEGACY when the key is absent */
    bool                  placement_mode_set;      /* key present */
    bool                  placement_policy_set;    /* legacy key present (conflict check) */
    bool                  placement_policy_enabled_set;
    bool                  placement_capacity_weighting_set;
    bool                  ds_weight_set;           /* any ds_weight.<id> present */
    uint32_t              placement_capacity_max_age_ms;
    uint64_t              placement_min_free_bytes;
    char                  ds_capacity_domain[MDS_MAX_DS_NODES][PM_DOMAIN_ID_MAX]; /* "" = own domain */
    char                  placement_domain_weight_id[PM_MAX_DOMAINS][PM_DOMAIN_ID_MAX];
    uint32_t              placement_domain_weight[PM_MAX_DOMAINS];
    uint32_t              placement_domain_weight_count;
    bool                  placement_allow_manual_base_weights;
    enum placement_shrink placement_stripe_shrink;
    char                  placement_config_generation[65]; /* hex sha256, "" in legacy */
```

- Produces (`src/common/placement_config.c`): `enum mds_status placement_config_validate(const struct mds_config *cfg, char *err, size_t cap)` (MDS_OK or MDS_ERR_INVAL with `err` = `"<CODE>: <detail>"`; codes `PLACEMENT_MODE_CONFLICT`, `PLACEMENT_MODE_UNSUPPORTED_BUILD`, `RANGE`, `DOMAIN_WEIGHT_FORBIDDEN`, `MIRROR_COUNT_UNSUPPORTED`), `void placement_config_generation(const struct mds_config *cfg, char out[65])` (SHA-256 of the canonical text `mode=<name>\nmax_age=<n>\nmin_free=<n>\nshrink=<name>\nallow_manual=<0|1>\ndomain.<id>=<domain>\n… (only set ids, ascending)\ndomain_weight.<domain>=<w>\n… (sorted)\nstripe=<default_stripe_count>\nmirror=<default_mirror_count>\n`), and `enum placement_mode placement_config_effective_mode(const struct mds_config *cfg)`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_placement_config.c`, uses `write_tmp_ini` + `mds_config_load` exactly like `test_config.c`, plus direct calls to the pure functions)

```c
static void test_absent_key_is_legacy(void)
{
    struct mds_config cfg; char path[128];
    ASSERT_EQ(write_tmp_ini("placement_policy_enabled = true\nplacement_policy = wrr\n", path), 0);
    ASSERT_EQ(mds_config_load(path, &cfg), MDS_OK);
    ASSERT_EQ(cfg.placement_mode, PM_LEGACY);
    ASSERT_EQ(placement_config_effective_mode(&cfg), PM_LEGACY);
    ASSERT_EQ(cfg.placement_policy_enabled, true);
    ASSERT_EQ(cfg.placement_policy, PLACEMENT_WEIGHTED_RR);
    ASSERT_EQ(cfg.placement_config_generation[0], '\0');
}
static void test_each_mode_parses_with_defaults(void)
{
    const char *modes[] = {"rr", "fill"};
    for (int i = 0; i < 2; i++) {
        struct mds_config cfg; char path[128], ini[128];
        snprintf(ini, sizeof(ini), "placement_mode = %s\n", modes[i]);
        ASSERT_EQ(write_tmp_ini(ini, path), 0);
        ASSERT_EQ(mds_config_load(path, &cfg), MDS_OK);
        ASSERT_EQ(cfg.placement_mode, i == 0 ? PM_RR : PM_FILL);
        ASSERT_EQ(cfg.placement_capacity_max_age_ms, 120000u);
        ASSERT_EQ(cfg.placement_min_free_bytes, 0u);
        ASSERT_EQ(cfg.placement_stripe_shrink, PM_SHRINK_ALLOW);
        ASSERT_EQ(strlen(cfg.placement_config_generation), 64u);
    }
}
static void test_smart_is_unsupported_in_this_build(void)
{
    struct mds_config cfg; char path[128];
    ASSERT_EQ(write_tmp_ini("placement_mode = smart\n", path), 0);
    ASSERT_EQ(mds_config_load(path, &cfg), MDS_ERR_INVAL);
}
static void test_legacy_keys_conflict_with_mode(void)
{
    const char *bad[] = {
        "placement_mode = rr\nplacement_policy = rr\n",
        "placement_mode = rr\nplacement_policy_enabled = true\n",
        "placement_mode = fill\nplacement_capacity_weighting = proportional\n",
        "placement_mode = fill\nds_weight.0 = 3\n",
        "placement_mode = fill\nworkload_profile = hpc\n",
        "placement_mode = fill\nplacement_domain_weight.d1 = 5\n",
        "placement_mode = rr\nplacement_domain_weight.d1 = 5\nplacement_allow_manual_base_weights = true\n",
    };
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        struct mds_config cfg; char path[128];
        ASSERT_EQ(write_tmp_ini(bad[i], path), 0);
        ASSERT_EQ(mds_config_load(path, &cfg), MDS_ERR_INVAL);
    }
}
static void test_ranges(void)
{
    const char *bad[] = {
        "placement_mode = fill\nds_capacity_poll_ms = 0\n",
        "placement_mode = fill\nds_capacity_poll_ms = 60000\nplacement_capacity_max_age_ms = 60000\n",
        "placement_mode = fill\nplacement_capacity_max_age_ms = 86400001\n",
        "placement_mode = fill\nplacement_stripe_shrink = maybe\n",
        "placement_mode = fill\nds_capacity_domain.3 = \n",
        "placement_mode = rr\nplacement_mode = sideways\n",
    };
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        struct mds_config cfg; char path[128];
        ASSERT_EQ(write_tmp_ini(bad[i], path), 0);
        ASSERT_EQ(mds_config_load(path, &cfg), MDS_ERR_INVAL);
    }
    struct mds_config cfg; char path[128];
    ASSERT_EQ(write_tmp_ini("placement_mode = fill\nds_capacity_poll_ms = 30000\nplacement_capacity_max_age_ms = 90000\nplacement_min_free_bytes = 1073741824\nplacement_stripe_shrink = strict\nds_capacity_domain.0 = xi/fs-1\nds_capacity_domain.1 = xi/fs-1\n", path), 0);
    ASSERT_EQ(mds_config_load(path, &cfg), MDS_OK);
    ASSERT_EQ(cfg.placement_min_free_bytes, 1073741824ull);
    ASSERT_EQ(cfg.placement_stripe_shrink, PM_SHRINK_STRICT);
    ASSERT_EQ(strcmp(cfg.ds_capacity_domain[0], "xi/fs-1"), 0);
    ASSERT_EQ(strcmp(cfg.ds_capacity_domain[1], "xi/fs-1"), 0);
    ASSERT_EQ(cfg.ds_capacity_domain[2][0], '\0');
}
static void test_generation_is_stable_and_sensitive(void)
{
    struct mds_config a, b; char path[128];
    ASSERT_EQ(write_tmp_ini("placement_mode = fill\nds_capacity_domain.1 = d\nds_capacity_domain.0 = d\n", path), 0);
    ASSERT_EQ(mds_config_load(path, &a), MDS_OK);
    ASSERT_EQ(write_tmp_ini("ds_capacity_domain.0 = d\nplacement_mode = fill\nds_capacity_domain.1 = d\n# comment\n", path), 0);
    ASSERT_EQ(mds_config_load(path, &b), MDS_OK);
    ASSERT_EQ(strcmp(a.placement_config_generation, b.placement_config_generation), 0);
    ASSERT_EQ(write_tmp_ini("placement_mode = fill\nds_capacity_domain.0 = d\nds_capacity_domain.1 = e\n", path), 0);
    ASSERT_EQ(mds_config_load(path, &b), MDS_OK);
    ASSERT_TRUE(strcmp(a.placement_config_generation, b.placement_config_generation) != 0);
}
static void test_rr_keeps_geometry(void)
{
    struct mds_config cfg; char path[128];
    ASSERT_EQ(write_tmp_ini("placement_mode = rr\ndefault_stripe_count = 4\n", path), 0);
    ASSERT_EQ(mds_config_load(path, &cfg), MDS_OK);
    ASSERT_EQ(cfg.default_stripe_count, 4u);
    ASSERT_EQ(cfg.placement_policy_enabled, true);   /* explicit mode takes the dispatcher branch */
}
```

Register `test_placement_config` in `tests/CMakeLists.txt` (executable + foreach list).

- [ ] **Step 2: Run to verify it fails**

Run: `mds/scripts/pm-run.sh test_placement_config`
Expected: `PM_RESULT BUILD_FAILED` (unknown members).

- [ ] **Step 3: Implement**

`config.c` defaults (next to `cfg->ds_capacity_poll_ms = 60000;`): `cfg->placement_mode = PM_LEGACY; cfg->placement_capacity_max_age_ms = PM_DEFAULT_CAP_MAX_AGE_MS; cfg->placement_stripe_shrink = PM_SHRINK_ALLOW;` (the char arrays are zero from `memset`).

Parse branches (in the second-pass `else if` chain; every `ERROR:` path does `(void)fclose(fp); return MDS_ERR_INVAL;` like `placement_policy`):

```c
        } else if (strcmp(key, PM_KEY_MODE) == 0) {
            if (strcmp(val, "rr") == 0)         cfg->placement_mode = PM_RR;
            else if (strcmp(val, "fill") == 0)  cfg->placement_mode = PM_FILL;
            else if (strcmp(val, "smart") == 0) cfg->placement_mode = PM_SMART;
            else { fprintf(stderr, "ERROR: invalid placement_mode '%s' (expected rr|fill|smart)\n", val); (void)fclose(fp); return MDS_ERR_INVAL; }
            cfg->placement_mode_set = true;
        } else if (strcmp(key, PM_KEY_CAP_MAX_AGE_MS) == 0) {
            unsigned long v = strtoul(val, NULL, 10);
            if (v == 0 || v > PM_CAP_MAX_AGE_MS_MAX) { fprintf(stderr, "ERROR: %s=%lu out of range (1..%u)\n", key, v, PM_CAP_MAX_AGE_MS_MAX); (void)fclose(fp); return MDS_ERR_INVAL; }
            cfg->placement_capacity_max_age_ms = (uint32_t)v;
        } else if (strcmp(key, PM_KEY_MIN_FREE_BYTES) == 0) {
            cfg->placement_min_free_bytes = strtoull(val, NULL, 10);
        } else if (strncmp(key, PM_KEY_DOMAIN_PREFIX, strlen(PM_KEY_DOMAIN_PREFIX)) == 0) {
            unsigned long id = strtoul(key + strlen(PM_KEY_DOMAIN_PREFIX), NULL, 10);
            if (id >= MDS_MAX_DS_NODES || val[0] == '\0' || strlen(val) >= PM_DOMAIN_ID_MAX) { fprintf(stderr, "ERROR: %s: bad ds id or empty/too long domain\n", key); (void)fclose(fp); return MDS_ERR_INVAL; }
            (void)snprintf(cfg->ds_capacity_domain[id], PM_DOMAIN_ID_MAX, "%s", val);
        } else if (strncmp(key, PM_KEY_DOMAIN_WEIGHT_PREFIX, strlen(PM_KEY_DOMAIN_WEIGHT_PREFIX)) == 0) {
            const char *dom = key + strlen(PM_KEY_DOMAIN_WEIGHT_PREFIX);
            unsigned long w = strtoul(val, NULL, 10);
            if (dom[0] == '\0' || strlen(dom) >= PM_DOMAIN_ID_MAX || w < PM_DOMAIN_WEIGHT_MIN || w > PM_DOMAIN_WEIGHT_MAX || cfg->placement_domain_weight_count >= PM_MAX_DOMAINS) { fprintf(stderr, "ERROR: %s=%lu out of range (%u..%u)\n", key, w, PM_DOMAIN_WEIGHT_MIN, PM_DOMAIN_WEIGHT_MAX); (void)fclose(fp); return MDS_ERR_INVAL; }
            uint32_t n = cfg->placement_domain_weight_count++;
            (void)snprintf(cfg->placement_domain_weight_id[n], PM_DOMAIN_ID_MAX, "%s", dom);
            cfg->placement_domain_weight[n] = (uint32_t)w;
        } else if (strcmp(key, PM_KEY_ALLOW_MANUAL) == 0) {
            cfg->placement_allow_manual_base_weights = (strcmp(val, "true") == 0 || strcmp(val, "1") == 0);
        } else if (strcmp(key, PM_KEY_SHRINK) == 0) {
            if (strcmp(val, "allow") == 0) cfg->placement_stripe_shrink = PM_SHRINK_ALLOW;
            else if (strcmp(val, "strict") == 0) cfg->placement_stripe_shrink = PM_SHRINK_STRICT;
            else { fprintf(stderr, "ERROR: invalid %s '%s' (expected allow|strict)\n", key, val); (void)fclose(fp); return MDS_ERR_INVAL; }
```

Mark presence in the existing branches: `cfg->placement_policy_set = true;` in `placement_policy`, `cfg->placement_policy_enabled_set = true;` in `placement_policy_enabled`, `cfg->placement_capacity_weighting_set = true;` in `placement_capacity_weighting`, `cfg->ds_weight_set = true;` in `ds_weight.`.

At the end of `mds_config_load` (before the prealloc auto-size block):

```c
    if (cfg->placement_mode_set) {
        char err[256];
        if (placement_config_validate(cfg, err, sizeof(err)) != MDS_OK) {
            (void)fprintf(stderr, "ERROR: %s\n", err);
            return MDS_ERR_INVAL;
        }
        /* An explicit mode always takes the dispatcher branch so the
         * configured stripe/mirror geometry applies in rr too (MODE-03). */
        cfg->placement_policy_enabled = true;
        cfg->placement_policy = (cfg->placement_mode == PM_RR) ? PLACEMENT_RR : PLACEMENT_WEIGHTED_RR;
        placement_config_generation(cfg, cfg->placement_config_generation);
    }
```

`src/common/placement_config.c`:

```c
enum mds_status placement_config_validate(const struct mds_config *cfg, char *err, size_t cap)
{
    if (!cfg->placement_mode_set) return MDS_OK;
    if (cfg->placement_policy_set || cfg->placement_policy_enabled_set || cfg->placement_capacity_weighting_set)
        return fail(err, cap, "PLACEMENT_MODE_CONFLICT: placement_policy/placement_policy_enabled/placement_capacity_weighting cannot be combined with placement_mode");
    if (cfg->workload_profile != MDS_PROFILE_DEFAULT && (cfg->tuning_set & MDS_CFG_SET_PLACEMENT_POLICY))
        return fail(err, cap, "PLACEMENT_MODE_CONFLICT: workload_profile sets a placement policy; use a profile without one or drop placement_mode");
    if (cfg->placement_mode == PM_FILL && cfg->ds_weight_set)
        return fail(err, cap, "PLACEMENT_MODE_CONFLICT: ds_weight.<id> is not allowed in fill (weights come from fill level)");
    if (cfg->placement_domain_weight_count > 0 && (cfg->placement_mode != PM_SMART || !cfg->placement_allow_manual_base_weights))
        return fail(err, cap, "DOMAIN_WEIGHT_FORBIDDEN: placement_domain_weight.* needs placement_mode = smart and placement_allow_manual_base_weights = true");
    if (cfg->placement_mode == PM_FILL || cfg->placement_mode == PM_SMART) {
        if (cfg->ds_capacity_poll_ms == 0) return fail(err, cap, "RANGE: ds_capacity_poll_ms must be > 0 in fill/smart");
        if (cfg->placement_capacity_max_age_ms <= cfg->ds_capacity_poll_ms) return fail(err, cap, "RANGE: placement_capacity_max_age_ms must exceed ds_capacity_poll_ms");
    }
    if (cfg->placement_mode == PM_SMART) {
        if (cfg->default_mirror_count > 1) return fail(err, cap, "MIRROR_COUNT_UNSUPPORTED: smart requires default_mirror_count = 1");
#ifndef ENABLE_DS_CONNECTOR
        return fail(err, cap, "PLACEMENT_MODE_UNSUPPORTED_BUILD: smart needs a binary built with ENABLE_DS_CONNECTOR=ON");
#endif
    }
    return MDS_OK;
}
```

(`fail()` snprintf's the message and returns `MDS_ERR_INVAL`; `placement_config_generation` builds the canonical text into a 16 KiB buffer in the order given under Interfaces, sorts the domain-weight pairs with `qsort` by id, then `SHA256()` from `<openssl/sha.h>` and hex-encodes.) Note the profile check must run on the *profile-applied* flag: `apply_profile` sets `MDS_CFG_SET_PLACEMENT_POLICY` in `tuning_set` for every non-default profile because their `sets` mask is `ALL_TUNING_BITS`; that is exactly the conflict the spec wants (§4).

Add `placement_config.c` to `src/common/CMakeLists.txt` sources; link OpenSSL is already required (`find_package(OpenSSL REQUIRED)`; add `OpenSSL::Crypto` to `pnfs_common` if not yet linked — `mds_tls.c` already uses it).

`docs/config-keys.md` — new subsection under "## Placement" listing every key from the Interfaces table with default and range, and the sentence "When `placement_mode` is present, the legacy placement keys are rejected (`PLACEMENT_MODE_CONFLICT`)".

- [ ] **Step 4: Run to verify it passes**

Run: `mds/scripts/pm-run.sh "test_placement_config|test_config"`
Expected: both PASS (legacy `test_config` unchanged).

- [ ] **Step 5: Commit**

```bash
git add include/placement_modes.h include/pnfs_mds.h src/common/config.c src/common/placement_config.c src/common/CMakeLists.txt docs/config-keys.md tests/unit/test_placement_config.c tests/CMakeLists.txt && git commit -m "feat(config): placement_mode rr|fill|smart with conflict rules, thresholds, domain map and config generation"
```

---

### Task 3: Capacity observation record

**Files:**
- Modify: `include/ds_cache.h`, `src/mds/ds_cache.c` (struct `ds_cache_entry`, after `last_observed_unix_sec`), `src/mds/ds_capacity.c` (`probe_one`), `tests/unit/test_ds_capacity.c`

**Interfaces:**
- Produces:

```c
struct ds_capacity_obs {
    uint64_t total_bytes;
    uint64_t avail_bytes;        /* f_bavail * f_frsize */
    uint64_t fsid;               /* statvfs f_fsid */
    uint64_t observed_mono_ms;   /* CLOCK_MONOTONIC ms of the last SUCCESSFUL local probe; 0 = never */
    uint32_t consecutive_failures;
};
int ds_cache_set_capacity_obs(struct ds_cache *c, uint32_t ds_id, const struct ds_capacity_obs *obs);   /* also resets consecutive_failures */
int ds_cache_note_capacity_failure(struct ds_cache *c, uint32_t ds_id);                                  /* ++consecutive_failures, keeps values/time */
enum mds_status ds_cache_get_capacity_obs(const struct ds_cache *c, uint32_t ds_id, struct ds_capacity_obs *out);
/* Snapshot of every present DS: ids[], obs[], host[] (from info.host), state[]; returns count. */
uint32_t ds_cache_capacity_view(const struct ds_cache *c, struct ds_capacity_view_row *rows, uint32_t cap);
struct ds_capacity_view_row { uint32_t ds_id; uint32_t state; char host[MDS_DS_HOST_MAX]; struct ds_capacity_obs obs; };
uint64_t ds_cache_mono_ms(void);   /* CLOCK_MONOTONIC in ms, shared helper */
```

- [ ] **Step 1: Write the failing tests** (append to `test_ds_capacity.c`; a present entry needs a catalogue — use `catalogue_memdb_open()` + `mds_cat_ds_put` as `test_compound.c`'s `seed_ds` does, then `ds_cache_create(cat, &c)`)

```c
static struct ds_cache *cache_with_ds(struct mds_catalogue **cat_out, uint32_t ds_id)
{
    struct mds_catalogue *cat = catalogue_memdb_open();
    struct mds_cat_txn *txn = NULL;
    struct mds_ds_info info;
    struct ds_cache *c = NULL;
    memset(&info, 0, sizeof(info));
    info.ds_id = ds_id; info.state = DS_ONLINE; info.port = 2049;
    snprintf(info.host, sizeof(info.host), "ds-host");
    (void)mds_cat_txn_begin(cat, MDS_CAT_TXN_WRITE, &txn);
    (void)mds_cat_ds_put(cat, txn, &info);
    (void)mds_cat_txn_commit(txn);
    (void)ds_cache_create(cat, &c);
    *cat_out = cat;
    return c;
}
static void test_obs_absent_until_probed(void)
{
    struct mds_catalogue *cat; struct ds_capacity_obs o;
    struct ds_cache *c = cache_with_ds(&cat, 0);
    ASSERT_TRUE(c != NULL);
    ASSERT_EQ(ds_cache_get_capacity_obs(c, 0, &o), MDS_OK);
    ASSERT_EQ(o.observed_mono_ms, 0u);
    ASSERT_EQ(ds_cache_get_capacity_obs(c, 5, &o), MDS_ERR_NOTFOUND);
    ds_cache_destroy(c); mds_catalogue_close(cat);
}
static void test_probe_records_avail_fsid_and_time(void)
{
    struct mds_catalogue *cat; struct ds_capacity_obs o; struct statvfs sv;
    struct ds_cache *c = cache_with_ds(&cat, 0);
    uint64_t before = ds_cache_mono_ms();
    ASSERT_EQ(ds_capacity_probe_once(c, "/tmp", CAP_WEIGHT_OFF), 1);     /* mount fmt has no %u -> "/tmp" */
    ASSERT_EQ(ds_cache_get_capacity_obs(c, 0, &o), MDS_OK);
    ASSERT_EQ(statvfs("/tmp", &sv), 0);
    ASSERT_EQ(o.total_bytes, (uint64_t)sv.f_blocks * sv.f_frsize);
    ASSERT_TRUE(o.avail_bytes <= o.total_bytes && o.avail_bytes > 0);
    ASSERT_EQ(o.fsid, (uint64_t)sv.f_fsid);
    ASSERT_TRUE(o.observed_mono_ms >= before && o.observed_mono_ms <= ds_cache_mono_ms());
    ASSERT_EQ(o.consecutive_failures, 0u);
    ds_cache_destroy(c); mds_catalogue_close(cat);
}
static void test_failed_probe_keeps_values_and_counts(void)
{
    struct mds_catalogue *cat; struct ds_capacity_obs o;
    struct ds_cache *c = cache_with_ds(&cat, 0);
    ASSERT_EQ(ds_capacity_probe_once(c, "/tmp", CAP_WEIGHT_OFF), 1);
    ASSERT_EQ(ds_cache_get_capacity_obs(c, 0, &o), MDS_OK);
    uint64_t t = o.observed_mono_ms;
    ASSERT_EQ(ds_capacity_probe_once(c, "/nonexistent-%u", CAP_WEIGHT_OFF), 0);
    ASSERT_EQ(ds_cache_get_capacity_obs(c, 0, &o), MDS_OK);
    ASSERT_EQ(o.observed_mono_ms, t);          /* not refreshed */
    ASSERT_EQ(o.consecutive_failures, 1u);
    ASSERT_TRUE(o.total_bytes > 0);            /* last values kept */
    ds_cache_destroy(c); mds_catalogue_close(cat);
}
static void test_remote_observation_does_not_touch_the_record(void)
{
    struct mds_catalogue *cat; struct ds_capacity_obs o; struct mds_ds_info remote;
    struct ds_cache *c = cache_with_ds(&cat, 0);
    memset(&remote, 0, sizeof(remote)); remote.ds_id = 0; remote.total_bytes = 500; remote.used_bytes = 100;
    ds_cache_apply_remote_observations(c, &remote, 1);
    ASSERT_EQ(ds_cache_get_capacity_obs(c, 0, &o), MDS_OK);
    ASSERT_EQ(o.observed_mono_ms, 0u);
    ds_cache_destroy(c); mds_catalogue_close(cat);
}
static void test_capacity_view_lists_present_ds(void)
{
    struct mds_catalogue *cat; struct ds_capacity_view_row rows[4];
    struct ds_cache *c = cache_with_ds(&cat, 3);
    ASSERT_EQ(ds_cache_capacity_view(c, rows, 4), 1u);
    ASSERT_EQ(rows[0].ds_id, 3u);
    ASSERT_EQ(rows[0].state, DS_ONLINE);
    ASSERT_EQ(strcmp(rows[0].host, "ds-host"), 0);
    ds_cache_destroy(c); mds_catalogue_close(cat);
}
```

Add `#include "mds_catalogue.h"` and `struct mds_catalogue *catalogue_memdb_open(void);` at the top of the test (the memdb lib is already linked for every unit test).

- [ ] **Step 2: Run to verify it fails**

Run: `mds/scripts/pm-run.sh test_ds_capacity` — Expected: `PM_RESULT BUILD_FAILED`.

- [ ] **Step 3: Implement**

`ds_cache.c`: add `struct ds_capacity_obs cap_obs;` to `ds_cache_entry`; `ds_cache_mono_ms()` with `clock_gettime(CLOCK_MONOTONIC)`; the three accessors under the rwlock (write for set/note, read for get/view); `ds_cache_capacity_view` copies `ds_id`, `info.state`, `info.host`, `cap_obs` for every `present` entry in id order. `ds_cache_apply_remote_observations` is untouched (it writes only `info.total/used` and `last_observed_unix_sec`).

`ds_capacity.c` `probe_one`: on `statvfs` failure or `f_frsize == 0 || f_blocks == 0` call `ds_cache_note_capacity_failure(cache, ds_id)` before `return 0`; on success, after `ds_cache_set_capacity`, fill `struct ds_capacity_obs obs = { total_bytes, avail_bytes, (uint64_t)sv.f_fsid, ds_cache_mono_ms(), 0 }` and `ds_cache_set_capacity_obs(cache, ds_id, &obs)`.

- [ ] **Step 4: Run to verify it passes**

Run: `mds/scripts/pm-run.sh "test_ds_capacity|test_compound"` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add include/ds_cache.h src/mds/ds_cache.c src/mds/ds_capacity.c tests/unit/test_ds_capacity.c && git commit -m "feat(ds_capacity): per-DS observation record (avail, fsid, monotonic time, failures) and a capacity view"
```

---

### Task 4: Placement gate — pure selection (`rr`, `fill`)

**Files:**
- Create: `include/placement_gate.h`, `src/fsal_obj/placement_gate.c`, `tests/unit/test_placement_gate.c`
- Modify: `src/fsal_obj/CMakeLists.txt` (add `placement_gate.c`), `tests/CMakeLists.txt`

**Interfaces:**
- Consumes: Task 1 `mds_wrr_weighted_pick2`, `mds_wrr_test_seed`; Task 2 `enum placement_mode`, `PM_WEIGHT_SCALE`, `PM_DOMAIN_ID_MAX`; Task 3 `struct ds_capacity_obs`, `struct ds_capacity_view_row`.
- Produces (`include/placement_gate.h`):

```c
enum placement_reason {
    PR_NONE = 0, PR_DS_OFFLINE, PR_CAPACITY_UNKNOWN, PR_CAPACITY_STALE, PR_CAPACITY_FULL,
    PR_DOMAIN_MAP_CONTRADICTION, PR_SHARED_FS_ALIAS_UNMAPPED, PR_ASSESSMENT_UNKNOWN,
    PR_ASSESSMENT_STALE, PR_CONNECTOR_DENIED, PR_ZERO_MULTIPLIER, PR_NO_BINDING,
    PR_NO_ELIGIBLE_DS, PR_INSUFFICIENT_ELIGIBLE_DS, PR_MODE_NOT_READY, PR_COUNT
};
const char *placement_reason_name(enum placement_reason r);   /* bounded label strings */

/* Immutable inputs for one decision; the singleton (Task 5) builds them,
 * tests build them by hand. */
struct placement_capacity_view {
    uint32_t count;
    struct ds_capacity_view_row rows[MDS_MAX_DS_NODES];
};
struct placement_ctx {
    enum placement_mode   mode;
    enum placement_shrink shrink;
    uint64_t              now_mono_ms;
    uint32_t              capacity_max_age_ms;
    uint64_t              min_free_bytes;
    const char          (*domain_of)[PM_DOMAIN_ID_MAX];   /* cfg->ds_capacity_domain or NULL */
    const struct placement_capacity_view *cap;             /* NULL in rr/legacy */
    const struct placement_assessment_view *assess;        /* Stage B; NULL in Stage A */
    _Atomic uint32_t     *rr_counter;                      /* shared rr cursor; NULL = use rr_key only */
};
struct placement_candidate {
    uint32_t idx;        /* index into the caller's ds_list */
    uint32_t ds_id;
    uint64_t weight;     /* > 0; 1 in rr */
    char     domain[PM_DOMAIN_ID_MAX];
};
struct placement_reject_counts { uint32_t by_reason[PR_COUNT]; };

uint32_t placement_candidates(const struct placement_ctx *ctx,
                              const struct mds_ds_info *ds_list, uint32_t n,
                              struct placement_candidate *out,          /* capacity n */
                              struct placement_reject_counts *why);      /* may be NULL */

/* The one entry point for a new backing object's DS selection.
 * stripe_count in/out (effective count on MDS_OK, as placement_select2).
 * MDS_ERR_NOSPC + *reason on refusal; MDS_ERR_INVAL on bad args. */
enum mds_status placement_admit(const struct placement_ctx *ctx,
                                const struct mds_ds_info *ds_list, uint32_t n,
                                uint32_t *stripe_count, uint32_t mirror_count,
                                uint64_t rr_key,
                                struct mds_ds_map_entry *entries,
                                enum placement_reason *reason);

/* Per-DS admission for the create boundary (Task 7). */
bool placement_ds_admitted(const struct placement_ctx *ctx,
                           const struct mds_ds_info *ds_list, uint32_t n,
                           uint32_t ds_id, enum placement_reason *reason);

/* Fixed-point weight; 0 when any input is 0. Computed in unsigned __int128,
 * asserted < 2^62 (returns 0 and sets *overflow when the assert would trip). */
uint64_t placement_weight(uint32_t domain_weight, uint32_t ppm, uint32_t n_aliases, bool *overflow);
```

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_placement_gate.c`; helpers `mk_ds(id, state, host)` as in `test_placement_policy.c`, `mk_view(...)` fills a `placement_capacity_view` row `{ds_id, DS_ONLINE, host, {total, avail, fsid, observed_mono_ms, 0}}`)

```c
static struct placement_capacity_view V;
static char DOM[MDS_MAX_DS_NODES][PM_DOMAIN_ID_MAX];
static _Atomic uint32_t RR;

static struct placement_ctx ctx_for(enum placement_mode m)
{
    struct placement_ctx c; memset(&c, 0, sizeof(c));
    c.mode = m; c.shrink = PM_SHRINK_ALLOW; c.now_mono_ms = 1000000;
    c.capacity_max_age_ms = 120000; c.min_free_bytes = 0;
    c.domain_of = DOM; c.cap = &V; c.rr_counter = &RR;
    return c;
}
static void add_row(uint32_t id, const char *host, uint64_t total, uint64_t avail, uint64_t fsid, uint64_t t)
{
    struct ds_capacity_view_row *r = &V.rows[V.count++];
    memset(r, 0, sizeof(*r)); r->ds_id = id; r->state = DS_ONLINE;
    snprintf(r->host, sizeof(r->host), "%s", host);
    r->obs.total_bytes = total; r->obs.avail_bytes = avail; r->obs.fsid = fsid; r->obs.observed_mono_ms = t;
}
static void reset(void) { memset(&V, 0, sizeof(V)); memset(DOM, 0, sizeof(DOM)); RR = 0; }

static void test_rr_is_cyclic_over_the_gated_list(void)
{
    reset();
    struct mds_ds_info ds[3]; mk_ds(&ds[0], 0, DS_ONLINE, "a"); mk_ds(&ds[1], 1, DS_OFFLINE, "b"); mk_ds(&ds[2], 2, DS_ONLINE, "c");
    struct placement_ctx c = ctx_for(PM_RR);
    uint32_t seq[6];
    for (int i = 0; i < 6; i++) {
        struct mds_ds_map_entry e; uint32_t sc = 1; enum placement_reason why;
        ASSERT_EQ(placement_admit(&c, ds, 3, &sc, 1, 0, &e, &why), MDS_OK);
        seq[i] = e.ds_id;
    }
    for (int i = 0; i < 6; i++) { ASSERT_TRUE(seq[i] == 0 || seq[i] == 2); ASSERT_TRUE(seq[i] != seq[(i + 1) % 6] || i == 5); }
    ASSERT_EQ(seq[0] + seq[1], 2u);   /* alternation: one 0, one 2 */
}
static void test_rr_ignores_capacity(void)
{
    reset();
    struct mds_ds_info ds[1]; mk_ds(&ds[0], 0, DS_ONLINE, "a");
    struct placement_ctx c = ctx_for(PM_RR); c.cap = NULL;   /* no capacity view at all */
    struct mds_ds_map_entry e; uint32_t sc = 1; enum placement_reason why;
    ASSERT_EQ(placement_admit(&c, ds, 1, &sc, 1, 0, &e, &why), MDS_OK);
}
static void test_fill_excludes_unknown_stale_and_full(void)
{
    reset();
    struct mds_ds_info ds[4]; for (uint32_t i = 0; i < 4; i++) mk_ds(&ds[i], i, DS_ONLINE, "h");
    add_row(0, "h", 1000, 500, 1, 1000000);            /* fresh, half free */
    add_row(1, "h", 1000, 500, 2, 1000000 - 130000);   /* stale */
    add_row(2, "h", 1000, 0, 3, 1000000);              /* full */
    /* ds 3: never observed */
    add_row(3, "h", 0, 0, 4, 0);
    struct placement_ctx c = ctx_for(PM_FILL);
    struct placement_candidate out[4]; struct placement_reject_counts why;
    ASSERT_EQ(placement_candidates(&c, ds, 4, out, &why), 1u);
    ASSERT_EQ(out[0].ds_id, 0u);
    ASSERT_EQ(why.by_reason[PR_CAPACITY_STALE], 1u);
    ASSERT_EQ(why.by_reason[PR_CAPACITY_FULL], 1u);
    ASSERT_EQ(why.by_reason[PR_CAPACITY_UNKNOWN], 1u);
}
static void test_fill_min_free_gate(void)
{
    reset();
    struct mds_ds_info ds[1]; mk_ds(&ds[0], 0, DS_ONLINE, "h");
    add_row(0, "h", 1000, 100, 1, 1000000);
    struct placement_ctx c = ctx_for(PM_FILL); c.min_free_bytes = 100;   /* avail must be > 100 */
    struct placement_candidate out[1];
    ASSERT_EQ(placement_candidates(&c, ds, 1, out, NULL), 0u);
    c.min_free_bytes = 99;
    ASSERT_EQ(placement_candidates(&c, ds, 1, out, NULL), 1u);
}
static void test_fill_weight_is_fill_level_over_aliases(void)
{
    reset();
    struct mds_ds_info ds[3]; for (uint32_t i = 0; i < 3; i++) mk_ds(&ds[i], i, DS_ONLINE, "xi");
    add_row(0, "xi", 1000, 800, 7, 1000000);   /* domain d: 80% free, aliases 0,1 */
    add_row(1, "xi", 1000, 800, 7, 1000000);
    add_row(2, "xi", 1000, 200, 8, 1000000);   /* own domain: 20% free */
    snprintf(DOM[0], PM_DOMAIN_ID_MAX, "d"); snprintf(DOM[1], PM_DOMAIN_ID_MAX, "d");
    struct placement_ctx c = ctx_for(PM_FILL);
    struct placement_candidate out[3];
    ASSERT_EQ(placement_candidates(&c, ds, 3, out, NULL), 3u);
    ASSERT_EQ(out[0].weight, placement_weight(80, 1000000, 2, NULL));
    ASSERT_EQ(out[1].weight, out[0].weight);
    ASSERT_EQ(out[2].weight, placement_weight(20, 1000000, 1, NULL));
    ASSERT_EQ(out[0].weight * 2, out[2].weight * 4);   /* domain totals 80 : 20 */
}
static void test_alias_share_counts_offline_members(void)
{
    reset();
    struct mds_ds_info ds[2]; mk_ds(&ds[0], 0, DS_ONLINE, "xi"); mk_ds(&ds[1], 1, DS_OFFLINE, "xi");
    add_row(0, "xi", 1000, 800, 7, 1000000); add_row(1, "xi", 1000, 800, 7, 1000000);
    snprintf(DOM[0], PM_DOMAIN_ID_MAX, "d"); snprintf(DOM[1], PM_DOMAIN_ID_MAX, "d");
    struct placement_ctx c = ctx_for(PM_FILL);
    struct placement_candidate out[2];
    ASSERT_EQ(placement_candidates(&c, ds, 2, out, NULL), 1u);
    ASSERT_EQ(out[0].weight, placement_weight(80, 1000000, 2, NULL));   /* still 1/2, not redistributed */
}
static void test_domain_canonical_observation_is_lowest_id_and_conservative(void)
{
    reset();
    struct mds_ds_info ds[2]; mk_ds(&ds[0], 0, DS_ONLINE, "xi"); mk_ds(&ds[1], 1, DS_ONLINE, "xi");
    add_row(0, "xi", 1000, 800, 7, 1000000); add_row(1, "xi", 1000, 400, 7, 1000000);   /* >1% apart */
    snprintf(DOM[0], PM_DOMAIN_ID_MAX, "d"); snprintf(DOM[1], PM_DOMAIN_ID_MAX, "d");
    struct placement_ctx c = ctx_for(PM_FILL);
    struct placement_candidate out[2];
    ASSERT_EQ(placement_candidates(&c, ds, 2, out, NULL), 2u);
    ASSERT_EQ(out[0].weight, placement_weight(40, 1000000, 2, NULL));   /* the smaller avail wins */
}
static void test_alias_grades(void)
{
    reset();
    struct mds_ds_info ds[2]; mk_ds(&ds[0], 0, DS_ONLINE, "xi"); mk_ds(&ds[1], 1, DS_ONLINE, "xi");
    add_row(0, "xi", 1000, 800, 7, 1000000); add_row(1, "xi", 1000, 800, 7, 1000000);
    struct placement_ctx c = ctx_for(PM_FILL);
    struct placement_candidate out[2]; struct placement_reject_counts why;
    /* (b) same host + same fsid, no map -> proven alias, both excluded */
    ASSERT_EQ(placement_candidates(&c, ds, 2, out, &why), 0u);
    ASSERT_EQ(why.by_reason[PR_SHARED_FS_ALIAS_UNMAPPED], 2u);
    /* (a) declared one domain but fsid differs on the same host -> contradiction */
    snprintf(DOM[0], PM_DOMAIN_ID_MAX, "d"); snprintf(DOM[1], PM_DOMAIN_ID_MAX, "d");
    V.rows[1].obs.fsid = 8;
    ASSERT_EQ(placement_candidates(&c, ds, 2, out, &why), 0u);
    ASSERT_EQ(why.by_reason[PR_DOMAIN_MAP_CONTRADICTION], 2u);
    /* (c) same fsid behind different host strings, no map -> suspected only, both stay candidates */
    memset(DOM, 0, sizeof(DOM)); V.rows[1].obs.fsid = 7; snprintf(V.rows[1].host, MDS_DS_HOST_MAX, "xi-alt"); snprintf(ds[1].host, MDS_DS_HOST_MAX, "xi-alt");
    ASSERT_EQ(placement_candidates(&c, ds, 2, out, &why), 2u);
}
static void test_weight_bounds(void)
{
    bool ovf = false;
    ASSERT_EQ(placement_weight(1, 1, 256, &ovf), 256u);
    ASSERT_EQ(ovf, false);
    ASSERT_EQ(placement_weight(0, 1000000, 1, &ovf), 0u);
    uint64_t max = placement_weight(10000, 1000000, 1, &ovf);
    ASSERT_EQ(ovf, false);
    ASSERT_TRUE((unsigned __int128)max * 256 < ((unsigned __int128)1 << 62));
    ASSERT_EQ(placement_weight(100000, 1000000, 1, &ovf), 0u);   /* out-of-range override -> overflow flag */
    ASSERT_EQ(ovf, true);
}
static void test_fill_multi_stripe_distinct_and_shrink_vs_strict(void)
{
    reset();
    struct mds_ds_info ds[3]; for (uint32_t i = 0; i < 3; i++) { mk_ds(&ds[i], i, DS_ONLINE, "h"); add_row(i, "h", 1000, 500, 10 + i, 1000000); }
    struct placement_ctx c = ctx_for(PM_FILL);
    struct mds_ds_map_entry e[4]; uint32_t sc = 4; enum placement_reason why;
    ASSERT_EQ(placement_admit(&c, ds, 3, &sc, 1, 0, e, &why), MDS_OK);
    ASSERT_EQ(sc, 3u);
    ASSERT_TRUE(e[0].ds_id != e[1].ds_id && e[1].ds_id != e[2].ds_id && e[0].ds_id != e[2].ds_id);
    c.shrink = PM_SHRINK_STRICT; sc = 4;
    ASSERT_EQ(placement_admit(&c, ds, 3, &sc, 1, 0, e, &why), MDS_ERR_NOSPC);
    ASSERT_EQ(why, PR_INSUFFICIENT_ELIGIBLE_DS);
    ASSERT_EQ(sc, 4u);   /* unchanged on error */
}
static void test_no_candidate_is_nospc_never_ds0(void)
{
    reset();
    struct mds_ds_info ds[2]; mk_ds(&ds[0], 0, DS_ONLINE, "h"); mk_ds(&ds[1], 1, DS_ONLINE, "h");
    add_row(0, "h", 1000, 0, 1, 1000000); add_row(1, "h", 1000, 0, 2, 1000000);
    struct placement_ctx c = ctx_for(PM_FILL);
    struct mds_ds_map_entry e; uint32_t sc = 1; enum placement_reason why;
    ASSERT_EQ(placement_admit(&c, ds, 2, &sc, 1, 0, &e, &why), MDS_ERR_NOSPC);
    ASSERT_EQ(why, PR_NO_ELIGIBLE_DS);
    ASSERT_EQ(e.ds_id, 0u);   /* entries zeroed, not "DS 0 chosen": caller checks status first */
}
static void test_fill_fairness_4_to_1(void)
{
    reset();
    struct mds_ds_info ds[2]; mk_ds(&ds[0], 0, DS_ONLINE, "a"); mk_ds(&ds[1], 1, DS_ONLINE, "b");
    add_row(0, "a", 1000, 800, 1, 1000000); add_row(1, "b", 1000, 200, 2, 1000000);
    struct placement_ctx c = ctx_for(PM_FILL);
    mds_wrr_test_seed(4242);
    uint32_t hits0 = 0;
    for (int i = 0; i < 100000; i++) {
        struct mds_ds_map_entry e; uint32_t sc = 1; enum placement_reason why;
        ASSERT_EQ(placement_admit(&c, ds, 2, &sc, 1, 0, &e, &why), MDS_OK);
        if (e.ds_id == 0) hits0++;
    }
    ASSERT_TRUE(hits0 > 78000 && hits0 < 82000);
}
static void test_fill_equal_fill_is_even(void)
{
    reset();
    struct mds_ds_info ds[2]; mk_ds(&ds[0], 0, DS_ONLINE, "a"); mk_ds(&ds[1], 1, DS_ONLINE, "b");
    add_row(0, "a", 40000, 39000, 1, 1000000); add_row(1, "b", 30000, 29250, 2, 1000000);  /* both 97.5% free */
    struct placement_ctx c = ctx_for(PM_FILL);
    mds_wrr_test_seed(99);
    uint32_t hits0 = 0;
    for (int i = 0; i < 100000; i++) { struct mds_ds_map_entry e; uint32_t sc = 1; enum placement_reason why; (void)placement_admit(&c, ds, 2, &sc, 1, 0, &e, &why); if (e.ds_id == 0) hits0++; }
    ASSERT_TRUE(hits0 > 48000 && hits0 < 52000);
}
static void test_sizes_64_65_256(void)
{
    static struct mds_ds_info ds[256];
    uint32_t sizes[3] = {64, 65, 256};
    for (int s = 0; s < 3; s++) {
        reset();
        for (uint32_t i = 0; i < sizes[s]; i++) { char h[16]; snprintf(h, sizeof(h), "h%u", i); mk_ds(&ds[i], i, DS_ONLINE, h); add_row(i, h, 1000, 500, 100 + i, 1000000); }
        struct placement_ctx c = ctx_for(PM_FILL);
        struct mds_ds_map_entry e[8]; uint32_t sc = 8; enum placement_reason why;
        ASSERT_EQ(placement_admit(&c, ds, sizes[s], &sc, 1, 0, e, &why), MDS_OK);
        ASSERT_EQ(sc, 8u);
        for (int i = 0; i < 8; i++) for (int j = i + 1; j < 8; j++) ASSERT_TRUE(e[i].ds_id != e[j].ds_id);
    }
}
static void test_ds_admitted_single(void)
{
    reset();
    struct mds_ds_info ds[2]; mk_ds(&ds[0], 0, DS_ONLINE, "a"); mk_ds(&ds[1], 1, DS_ONLINE, "b");
    add_row(0, "a", 1000, 800, 1, 1000000); add_row(1, "b", 1000, 0, 2, 1000000);
    struct placement_ctx c = ctx_for(PM_FILL); enum placement_reason why;
    ASSERT_EQ(placement_ds_admitted(&c, ds, 2, 0, &why), true);
    ASSERT_EQ(placement_ds_admitted(&c, ds, 2, 1, &why), false);
    ASSERT_EQ(why, PR_CAPACITY_FULL);
    ASSERT_EQ(placement_ds_admitted(&c, ds, 2, 9, &why), false);
    ASSERT_EQ(why, PR_DS_OFFLINE);
    c.mode = PM_RR;
    ASSERT_EQ(placement_ds_admitted(&c, ds, 2, 1, &why), true);
}
```

- [ ] **Step 2: Run to verify it fails** — `mds/scripts/pm-run.sh test_placement_gate` → `PM_RESULT BUILD_FAILED`.

- [ ] **Step 3: Implement `placement_gate.c`** (pure part; the singleton is Task 5)

Algorithm of `placement_candidates`:
1. `native`: keep `state == DS_ONLINE` (callers already filtered profile/io-limit); rr: every kept DS is a candidate with weight 1 and empty domain → return.
2. fill/smart: for each kept DS find its row in `ctx->cap` (by `ds_id`; missing row → `PR_CAPACITY_UNKNOWN`). Domain = `ctx->domain_of[ds_id]` if non-empty else the DS's own id as `"ds:<id>"`.
3. Alias checks over ALL rows of the view (not only kept DS): group by declared domain; inside one declared domain, two fresh rows with the same `host` and different `fsid` → all members `PR_DOMAIN_MAP_CONTRADICTION`. Across undeclared rows: two fresh rows with equal `host` and equal `fsid` and no shared declared domain → both `PR_SHARED_FS_ALIAS_UNMAPPED` (grade b). Equal `fsid` with different `host` and no shared domain → `placement_gate_note_alias_suspected(id_a, id_b)` (counter + rate-limited WARN; Task 5 wires it) and no exclusion (grade c).
4. Per domain, the canonical observation: among members with a fresh row (`now - observed_mono_ms <= capacity_max_age_ms`, `observed_mono_ms != 0`, `total > 0`), the lowest `ds_id`; if another fresh member's `avail` differs by more than 1 % of `total`, use the smallest `avail` (conservative). No fresh member → every member `PR_CAPACITY_UNKNOWN` (never observed) or `PR_CAPACITY_STALE` (observed but old).
5. Gate: `avail > min_free_bytes` else `PR_CAPACITY_FULL`. `domain_weight = max(1, floor(100 × avail / total))`.
6. `N` = number of DS in `ds_list` (all registered, any state) whose domain equals this domain.
7. `weight = placement_weight(domain_weight, ppm, N, &ovf)` with `ppm = 1000000` (fill) — smart multiplies by the assessment ppm in Stage B; `ovf` → treat as `PR_CAPACITY_UNKNOWN` and count it (cannot happen with the config ranges; the branch is defensive).

`placement_admit`: `n_c = placement_candidates(...)`; `0` → `PR_NO_ELIGIBLE_DS`, `MDS_ERR_NOSPC`, entries zeroed. `n_c < mirror_count` → `PR_INSUFFICIENT_ELIGIBLE_DS`. `sc × mirror_count > n_c`: strict → `PR_INSUFFICIENT_ELIGIBLE_DS`; allow → `sc = n_c / mirror_count` (min 1). rr: start = `rr_counter ? atomic_fetch_add(rr_counter, sc*mirror_count) : rr_key`, walk `candidates[(start + s*mc + m) % n_c]`. fill/smart: `taken[]` over candidates (stack ≤ 64 else `calloc`), per stripe build the masked weight vector (`taken ? 0 : weight`, one code path for any size), `mds_wrr_weighted_pick2` → head; mirrors = next untaken candidates after the head in index order; `-1` from the kernel with stripes still to place → strict: NOSPC `PR_INSUFFICIENT_ELIGIBLE_DS`; allow: stop and return the stripes placed so far (`sc = s`). Bump `g_branch_metrics.placement_degraded_total` when shrunk (keeps the upstream metric meaningful).

`placement_ds_admitted`: rr/legacy → the DS is in `ds_list` with `DS_ONLINE`; fill/smart → the DS is among `placement_candidates` (reason from the counts, `PR_DS_OFFLINE` when absent from the list).

`placement_weight`: `if (domain_weight == 0 || ppm == 0 || n == 0) return 0; unsigned __int128 w = (unsigned __int128)domain_weight * ppm * PM_WEIGHT_SCALE / n; if (w == 0 || (w * MDS_MAX_DS_NODES) >= ((unsigned __int128)1 << 62)) { if (overflow) *overflow = true; return 0; } return (uint64_t)w;`

- [ ] **Step 4: Run to verify it passes** — `mds/scripts/pm-run.sh "test_placement_gate|test_placement_policy|test_wrr_kernel"` → PASS.

- [ ] **Step 5: Commit**

```bash
git add include/placement_gate.h src/fsal_obj/placement_gate.c src/fsal_obj/CMakeLists.txt tests/unit/test_placement_gate.c tests/CMakeLists.txt && git commit -m "feat(placement): candidate gate and admit for rr/fill — capacity gate, declared aliases, fixed-point weights, refusing kernel"
```

---

### Task 5: Gate singleton, capacity view publication, startup wiring

**Files:**
- Modify: `include/placement_gate.h`, `src/fsal_obj/placement_gate.c`, `src/mds/ds_capacity.c` (end of each sweep), `src/mds/main.c` (after `ds_cache_apply_weights`, ≈1050; shutdown next to `ds_capacity_stop`), `include/mds_metrics.h`, `src/common/mds_metrics.c`
- Test: `tests/unit/test_placement_gate.c` (append)

**Interfaces:**
- Produces:

```c
int  placement_gate_init(const struct mds_config *cfg, struct ds_cache *cache);   /* 0 ok; -1 when fill/smart and mds_wrr_kernel_id()==0 or cache NULL */
void placement_gate_destroy(void);
enum placement_mode placement_gate_mode(void);                    /* PM_LEGACY when not initialised */
const char *placement_gate_generation(void);
void placement_gate_publish_capacity(void);                       /* rebuilds the view from the DS cache; called by the capacity thread after every sweep and by tests */
/* Fills a ctx bound to the current published views (refcounted snapshot; call release after use). */
void placement_gate_ctx(struct placement_ctx *out, uint64_t now_mono_ms);
void placement_gate_ctx_release(struct placement_ctx *ctx);
void placement_gate_note_alias_suspected(uint32_t a, uint32_t b);
/* Metrics (mds_metrics.h g_branch_metrics): */
_Atomic uint64_t placement_rejections_total[PR_COUNT];
_Atomic uint64_t placement_eligible_ds;        /* gauge: candidates at the last admit */
_Atomic uint64_t placement_alias_suspected_total;
```

The view is a heap `struct placement_capacity_view` with an atomic refcount; `publish` allocates a new one, fills it via `ds_cache_capacity_view`, swaps the `_Atomic` pointer and drops the old reference; `placement_gate_ctx` takes a reference under an acquire load + fetch_add (the classic pointer+refcount without a lock is racy on the drop; use a `pthread_rwlock` read-lock only around the pointer load + increment — microseconds, no I/O — and the writer takes the write lock only for the pointer swap).

- [ ] **Step 1: Write the failing tests** (append to `test_placement_gate.c`; uses `cache_with_ds`-style memdb setup as in Task 3)

```c
static void test_singleton_legacy_when_not_initialised(void)
{
    ASSERT_EQ(placement_gate_mode(), PM_LEGACY);
    struct placement_ctx c; placement_gate_ctx(&c, 5);
    ASSERT_EQ(c.mode, PM_LEGACY); ASSERT_TRUE(c.cap == NULL);
    placement_gate_ctx_release(&c);
}
static void test_singleton_publishes_capacity_from_cache(void)
{
    struct mds_config cfg; memset(&cfg, 0, sizeof(cfg));
    cfg.placement_mode = PM_FILL; cfg.placement_mode_set = true; cfg.placement_capacity_max_age_ms = 120000;
    snprintf(cfg.placement_config_generation, sizeof(cfg.placement_config_generation), "%064x", 1);
    struct mds_catalogue *cat; struct ds_cache *cache = cache_with_ds(&cat, 0);
    ASSERT_EQ(placement_gate_init(&cfg, cache), 0);
    ASSERT_EQ(placement_gate_mode(), PM_FILL);
    struct placement_ctx c; placement_gate_ctx(&c, ds_cache_mono_ms());
    ASSERT_TRUE(c.cap != NULL); ASSERT_EQ(c.cap->count, 1u); ASSERT_EQ(c.cap->rows[0].obs.observed_mono_ms, 0u);
    placement_gate_ctx_release(&c);
    ASSERT_EQ(ds_capacity_probe_once(cache, "/tmp", CAP_WEIGHT_OFF), 1);   /* probe_once now publishes */
    placement_gate_ctx(&c, ds_cache_mono_ms());
    ASSERT_TRUE(c.cap->rows[0].obs.observed_mono_ms != 0);
    placement_gate_ctx_release(&c);
    placement_gate_destroy(); ds_cache_destroy(cache); mds_catalogue_close(cat);
}
static void test_singleton_refuses_fill_on_stub_kernel(void)
{
    /* Only meaningful in a stub build; in the real build kernel id != 0 so init succeeds. */
    struct mds_config cfg; memset(&cfg, 0, sizeof(cfg));
    cfg.placement_mode = PM_FILL; cfg.placement_mode_set = true; cfg.placement_capacity_max_age_ms = 120000;
    struct mds_catalogue *cat; struct ds_cache *cache = cache_with_ds(&cat, 0);
    int rc = placement_gate_init(&cfg, cache);
    ASSERT_EQ(rc, mds_wrr_kernel_id() == 0 ? -1 : 0);
    placement_gate_destroy(); ds_cache_destroy(cache); mds_catalogue_close(cat);
}
```

- [ ] **Step 2: Run to verify it fails** — `mds/scripts/pm-run.sh test_placement_gate` → `PM_RESULT BUILD_FAILED`.

- [ ] **Step 3: Implement**

`placement_gate.c` singleton: `static struct { enum placement_mode mode; enum placement_shrink shrink; uint32_t max_age; uint64_t min_free; char domain_of[MDS_MAX_DS_NODES][PM_DOMAIN_ID_MAX]; char generation[65]; struct ds_cache *cache; pthread_rwlock_t lock; struct cap_view_ref *cur; _Atomic uint32_t rr_counter; bool initialised; } g;` `placement_gate_init` copies the config, refuses `-1` with an `MDS_LOG_ERROR(LOG_COMP_FSAL, "placement: fill/smart need the XinnorLab wrr kernel (kernel id 0)")` when the mode needs the kernel and `mds_wrr_kernel_id() == 0`, publishes an initial view, logs `MDS_LOG_INFO(LOG_COMP_FSAL, "placement_mode=%s generation=%.12s kernel=%08x shrink=%s max_age_ms=%u min_free=%llu", …)` — only when `cfg->placement_mode_set` (legacy stays silent).

`ds_capacity.c`: at the end of the per-DS loop in `capacity_thread` and at the end of `ds_capacity_probe_once`, call `placement_gate_publish_capacity()` (no-op when the gate is not initialised or `cache` differs).

`main.c`: after `ds_cache_apply_weights(...)`: `if (cfg.placement_mode_set) { if (placement_gate_init(&cfg, ds_cache) != 0) { MDS_LOG_ERROR(LOG_COMP_MDS, "placement gate init failed; refusing to start"); rc = 1; goto cleanup; } }` (follow the file's existing error-exit pattern — look at how `ds_capacity_start` failure is handled and mirror the fatal path used for catalogue open failure). Shutdown: `placement_gate_destroy();` right after `ds_capacity_stop(ds_cap);`.

Metrics: add the three fields to `struct mds_branch_metrics`; in `mds_metrics.c` next to the `placement_heap_fallback_total` block render `pnfs_mds_placement_mode{mode="<name>"} 1` (one series, name from `placement_gate_mode()`), `pnfs_mds_placement_rejections_total{reason="<name>"} <n>` for every `PR_*` with a non-zero or ever-used slot, `pnfs_mds_placement_eligible_ds <n>`, `pnfs_mds_placement_alias_suspected_total <n>`.

- [ ] **Step 4: Run to verify it passes** — `mds/scripts/pm-run.sh "test_placement_gate|test_ds_capacity|test_mds_metrics"` → PASS.

- [ ] **Step 5: Commit**

```bash
git add include/placement_gate.h src/fsal_obj/placement_gate.c src/mds/ds_capacity.c src/mds/main.c include/mds_metrics.h src/common/mds_metrics.c tests/unit/test_placement_gate.c && git commit -m "feat(placement): gate singleton with a published capacity view, startup wiring, metrics"
```

---

### Task 6: Selection sites → `placement_admit`

**Files:**
- Modify: `src/mds/compound_layout.c` (≈2010–2035 the dispatcher call; ≈2214–2224 the fallback `placement_select`), `src/mds/compound_data_io.c` (≈1868 in `promote_inline_to_ds`), `src/modules/ds_prealloc/ds_prealloc_stub.c` (`select_one_online`, `ds_prealloc_batch`)
- Test: `tests/unit/test_compound.c` (append), `tests/unit/test_placement_gate.c` (append a prealloc-stub test — the stub is linked into `pnfs_mds_core` when `ENABLE_DS_PREALLOC=OFF`)

**Interfaces:**
- Consumes: Task 5 `placement_gate_mode()`, `placement_gate_ctx()`, `placement_gate_ctx_release()`; Task 4 `placement_admit()`.
- Produces: a shared helper in `placement_gate.h`:

```c
/* Site helper: gated selection when a mode is set, upstream selector otherwise.
 * `legacy_policy_enabled`/`legacy_policy` reproduce the pre-feature call. */
enum mds_status placement_select_gated(bool legacy_policy_enabled,
                                       enum mds_placement_policy legacy_policy,
                                       const struct mds_ds_info *ds_list, uint32_t n,
                                       uint32_t *stripe_count, uint32_t mirror_count,
                                       uint32_t stripe_unit, uint64_t rr_key,
                                       struct mds_ds_map_entry *entries,
                                       enum placement_reason *reason);
```

Legacy branch: `legacy_policy_enabled ? placement_select_ex2(policy, …) : placement_select2(…)` (the two upstream calls, unchanged); when `rr_key != 0` and legacy, `placement_select_rr_at2` (the batch site). Mode branch: `placement_gate_ctx(&ctx, ds_cache_mono_ms()); st = placement_admit(&ctx, …, rr_key, …); placement_gate_ctx_release(&ctx);` plus `atomic_fetch_add(&g_branch_metrics.placement_rejections_total[*reason], 1)` on refusal and `MDS_LOG_WARN(LOG_COMP_FSAL, "placement refused: %s", placement_reason_name(*reason))` rate-limited (one line per reason per 10 s via a static timestamp array).

- [ ] **Step 1: Write the failing tests**

`test_compound.c` — after `test_layoutget`, a LAYOUTGET with `fill` and a full DS must return `NFS4ERR_NOSPC`, and with a fresh half-free DS must grant:

```c
static void test_layoutget_fill_mode_gates_full_ds(void)
{
    struct mds_catalogue *db; struct compound_data cd; struct nfs4_op ops[6]; struct nfs4_result res[6];
    char *path; uint32_t n; struct ds_cache *dsc = NULL;
    memset(res, 0, sizeof(res));
    db = open_test_db(&path);
    seed_patched_ready_ds(db, 1, "10.0.0.1:/export1");
    seed_ds_provision(db, 1);
    VERIFY(ds_cache_create(g_test_cat, &dsc) == 0);
    struct mds_config cfg; memset(&cfg, 0, sizeof(cfg));
    cfg.placement_mode = PM_FILL; cfg.placement_mode_set = true; cfg.placement_capacity_max_age_ms = 120000;
    VERIFY(placement_gate_init(&cfg, dsc) == 0);
    struct ds_capacity_obs full = { 1000, 0, 77, ds_cache_mono_ms(), 0 };
    VERIFY(ds_cache_set_capacity_obs(dsc, 1, &full) == 0);
    placement_gate_publish_capacity();

    compound_init(&cd); cd.cat = g_test_cat; cd.prealloc = g_prealloc; cd.ds_cache = dsc;
    cd.cfg_placement_policy_enabled = true; cd.cfg_placement_policy = PLACEMENT_WEIGHTED_RR;
    ops[0] = mk_sequence(); ops[1] = mk_putrootfh(); ops[2] = mk_create("gated", MDS_FTYPE_REG, 0644);
    n = compound_process(&cd, ops, res, 3); VERIFY(n == 3 && res[2].status == NFS4_OK);
    clear_inline_flag(db, res[2].fh_fileid);   /* the file's helper in this suite */
    ops[3] = mk_layoutget(LAYOUTIOMODE4_RW);
    n = compound_process(&cd, ops, res, 4);
    VERIFY(res[3].status == NFS4ERR_NOSPC);

    struct ds_capacity_obs half = { 1000, 500, 77, ds_cache_mono_ms(), 0 };
    VERIFY(ds_cache_set_capacity_obs(dsc, 1, &half) == 0);
    placement_gate_publish_capacity();
    n = compound_process(&cd, ops, res, 4);
    VERIFY(res[3].status == NFS4_OK || res[3].status == NFS4ERR_DELAY);   /* granted or FH-pending, never NOSPC */
    placement_gate_destroy(); ds_cache_destroy(dsc); close_test_db(db, path);
}
```

(`clear_inline_flag` and the CREATE→LAYOUTGET sequence exist in `test_layoutget`; copy their exact helper names from that test when writing this one.)

`test_placement_gate.c` — the prealloc stub through the gate:

```c
static void test_prealloc_stub_pop_is_gated(void)
{
    struct mds_catalogue *cat; struct ds_cache *cache = cache_with_ds(&cat, 0);
    /* second DS, ds_id 1, fresh and half free */
    { struct mds_cat_txn *txn = NULL; struct mds_ds_info info; memset(&info, 0, sizeof(info)); info.ds_id = 1; info.state = DS_ONLINE; info.port = 2049; snprintf(info.host, sizeof(info.host), "other"); (void)mds_cat_txn_begin(cat, MDS_CAT_TXN_WRITE, &txn); (void)mds_cat_ds_put(cat, txn, &info); (void)mds_cat_txn_commit(txn); ds_cache_invalidate(cache, cat); }
    struct mds_config cfg; memset(&cfg, 0, sizeof(cfg)); cfg.placement_mode = PM_FILL; cfg.placement_mode_set = true; cfg.placement_capacity_max_age_ms = 120000;
    ASSERT_EQ(placement_gate_init(&cfg, cache), 0);
    struct ds_capacity_obs full = { 1000, 0, 1, ds_cache_mono_ms(), 0 }, half = { 1000, 500, 2, ds_cache_mono_ms(), 0 };
    ASSERT_EQ(ds_cache_set_capacity_obs(cache, 0, &full), 0); ASSERT_EQ(ds_cache_set_capacity_obs(cache, 1, &half), 0);
    placement_gate_publish_capacity();
    struct ds_prealloc_ctx *pa = NULL;
    ASSERT_EQ(ds_prealloc_init_ex(cat, NULL, PLACEMENT_WEIGHTED_RR, 1, &pa), 0);
    ds_prealloc_set_ds_cache(pa, cache);
    for (int i = 0; i < 50; i++) { struct mds_ds_map_entry e; uint32_t su; uint64_t fid; ASSERT_EQ(ds_prealloc_pop(pa, &e, &su, &fid), 0); ASSERT_EQ(e.ds_id, 1u); }
    ASSERT_EQ(ds_cache_set_capacity_obs(cache, 1, &full), 0); placement_gate_publish_capacity();
    { struct mds_ds_map_entry e; uint32_t su; uint64_t fid; ASSERT_EQ(ds_prealloc_pop(pa, &e, &su, &fid), -1); }
    ds_prealloc_destroy(pa); placement_gate_destroy(); ds_cache_destroy(cache); mds_catalogue_close(cat);
}
```

- [ ] **Step 2: Run to verify it fails** — `mds/scripts/pm-run.sh "test_compound|test_placement_gate"` → build failure / NOSPC assertion fails.

- [ ] **Step 3: Implement**

`compound_layout.c` dispatcher site: replace the `if (cd->cfg_placement_policy_enabled) { st = placement_select_ex2(...) } else { st = placement_select2(...) }` block with `enum placement_reason why; st = placement_select_gated(cd->cfg_placement_policy_enabled, cd->cfg_placement_policy, ds_list, ds_count, &stripe_count, mirror_count, stripe_unit, 0, entries, &why);`. Fallback site (≈2217): `st = placement_select_gated(false, PLACEMENT_RR, ds_list, ds_count, &stripe_count, mirror_count, stripe_unit, 0, entries, &why);` (legacy branch reproduces `placement_select` = `placement_select2` by value; `stripe_count` there is a local `1`, pass its address). `promote_inline_to_ds`: same replacement of `placement_select`. `ds_prealloc_stub.c`: `select_one_online` → `uint32_t sc = 1; placement_select_gated(true, ctx->policy, ds_list, ds_count, &sc, 1, ctx->stripe_unit, 0, entry, &why)` (legacy: `placement_select_ex` = `_ex2` by value — the stub always ran the dispatcher); `ds_prealloc_batch` → `placement_select_gated(false, PLACEMENT_RR, …, &sc, mc, stripe_unit, fileid, entries, &why)` (legacy branch uses `placement_select_rr_at2` when `rr_key != 0`). Keep the `strict_unique_ds` check after the call.

The `ds_cache_overlay_weights` calls stay (harmless; the gate ignores `weight`).

- [ ] **Step 4: Run to verify it passes** — `mds/scripts/pm-run.sh "test_compound|test_placement_gate|test_placement_policy|test_inline_data|test_hpc_shared"` → PASS. Then the grep proof: `grep -n "placement_select" src/ -r | grep -v "placement.c\|placement_gate.c\|placement_select_replacement\|placement_select_for_tier\|placement_select_gated"` must print nothing.

- [ ] **Step 5: Commit**

```bash
git add src/mds/compound_layout.c src/mds/compound_data_io.c src/modules/ds_prealloc/ds_prealloc_stub.c include/placement_gate.h src/fsal_obj/placement_gate.c tests/unit/test_compound.c tests/unit/test_placement_gate.c && git commit -m "feat(placement): every selection site goes through placement_select_gated (LAYOUTGET x2, promotion, prealloc pop/batch)"
```

---

### Task 7: Create-if-absent boundary in `proxy_io.c`

**Files:**
- Modify: `include/proxy_io.h`, `src/mds/proxy_io.c` (`mds_proxy_ensure_ds_file` ≈913, `mds_proxy_ensure_ds_file_fh` ≈1108, the batch worker), `include/placement_gate.h`, `src/fsal_obj/placement_gate.c`
- Test: `tests/unit/test_placement_create_boundary.c` (new; fixture = `test_proxy_io.c`'s `make_ds_dir` + `mds_proxy_ctx_create` + `mds_proxy_mount_set`)

**Interfaces:**
- Produces:

```c
struct placement_token { uint32_t ds_id; uint32_t purpose; uint64_t minted_mono_ms; uint32_t snapshot_gen; };
enum placement_purpose { PP_NEW_OBJECT = 1, PP_RECREATE_MISSING = 2 };
/* Mints a token when the DS is admitted under the current published views;
 * MDS_ERR_NOSPC + reason otherwise. Legacy/rr: admitted when the DS is ONLINE in the cache. */
enum mds_status placement_gate_admit_create(uint32_t ds_id, enum placement_purpose p,
                                            struct placement_token *tok, enum placement_reason *reason);
bool placement_token_valid(const struct placement_token *tok, uint32_t ds_id, uint64_t now_mono_ms); /* same ds, age <= 2000 ms */

enum mds_status mds_proxy_lookup_ds_file_fh(const struct mds_proxy_ctx *ctx, uint32_t ds_id, uint64_t fileid,
                                            uint32_t stripe, uint32_t mirror, uint8_t *fh_out, uint32_t *fh_len);  /* MDS_ERR_NOTFOUND when absent; never creates */
enum mds_status mds_proxy_create_ds_file(const struct mds_proxy_ctx *ctx, uint32_t ds_id, uint64_t fileid,
                                         uint32_t stripe, uint32_t mirror, const struct placement_token *tok);
enum mds_status mds_proxy_create_ds_file_fh(const struct mds_proxy_ctx *ctx, uint32_t ds_id, uint64_t fileid,
                                            uint32_t stripe, uint32_t mirror, const struct placement_token *tok,
                                            uint8_t *fh_out, uint32_t *fh_len);
```

`mds_proxy_ensure_ds_file` becomes: `lookup (stat path)` → exists ? `MDS_OK` : `placement_gate_admit_create(ds_id, PP_RECREATE_MISSING, &tok, &why)` → `MDS_ERR_NOSPC` (log once per 10 s) or `mds_proxy_create_ds_file(…, &tok)`. `mds_proxy_ensure_ds_file_fh` becomes `lookup_fh` → `MDS_ERR_NOTFOUND` ? admit → `create_fh` : return; the NFS3 RPC fallback stays inside the lookup/create functions unchanged. The batch worker calls the ensure wrapper per slot (it already does — verify) so it needs no change beyond the wrapper. `placement_gate_admit_create` is the *only* minting path and the `create_*` functions refuse `tok == NULL` or an invalid token with `MDS_ERR_INVAL`.

The gate needs the registry to check `DS_ONLINE` in legacy/rr: it reads `ds_cache_get(g.cache, ds_id, &info)`; when the gate is not initialised (legacy) it admits every registered-or-unknown DS (upstream behaviour: no check) — document that legacy mode never refuses here.

- [ ] **Step 1: Write the failing tests**

```c
static void test_ensure_creates_in_rr_and_refuses_full_ds_in_fill(void)
{
    struct mds_catalogue *cat; struct ds_cache *cache = cache_with_ds(&cat, 1);   /* ds_id 1 */
    char *dir = make_ds_dir(); struct mds_proxy_ctx *proxy = NULL;
    ASSERT_EQ(mds_proxy_ctx_create(&proxy), MDS_OK);
    ASSERT_EQ(mds_proxy_mount_set(proxy, 1, dir), MDS_OK);
    struct mds_config cfg; memset(&cfg, 0, sizeof(cfg)); cfg.placement_mode = PM_RR; cfg.placement_mode_set = true;
    ASSERT_EQ(placement_gate_init(&cfg, cache), 0);
    ASSERT_EQ(mds_proxy_ensure_ds_file(proxy, 1, 100, 0, 0), MDS_OK);          /* rr: created */
    placement_gate_destroy();
    cfg.placement_mode = PM_FILL; cfg.placement_capacity_max_age_ms = 120000;
    ASSERT_EQ(placement_gate_init(&cfg, cache), 0);
    struct ds_capacity_obs full = { 1000, 0, 1, ds_cache_mono_ms(), 0 };
    ASSERT_EQ(ds_cache_set_capacity_obs(cache, 1, &full), 0); placement_gate_publish_capacity();
    ASSERT_EQ(mds_proxy_ensure_ds_file(proxy, 1, 100, 0, 0), MDS_OK);          /* exists: lookup succeeds, no gate */
    ASSERT_EQ(mds_proxy_ensure_ds_file(proxy, 1, 101, 0, 0), MDS_ERR_NOSPC);   /* absent + full: refused */
    char p[512]; snprintf(p, sizeof(p), "%s/data/101_0_0", dir); struct stat st;
    ASSERT_TRUE(stat(p, &st) != 0);                                             /* nothing created */
    uint8_t fh[128]; uint32_t fl = sizeof(fh);
    ASSERT_EQ(mds_proxy_ensure_ds_file_fh(proxy, 1, 101, 0, 0, fh, &fl), MDS_ERR_NOSPC);
    ASSERT_TRUE(stat(p, &st) != 0);
    fl = sizeof(fh);
    ASSERT_EQ(mds_proxy_lookup_ds_file_fh(proxy, 1, 101, 0, 0, fh, &fl), MDS_ERR_NOTFOUND);
    struct ds_capacity_obs half = { 1000, 500, 1, ds_cache_mono_ms(), 0 };
    ASSERT_EQ(ds_cache_set_capacity_obs(cache, 1, &half), 0); placement_gate_publish_capacity();
    ASSERT_EQ(mds_proxy_ensure_ds_file(proxy, 1, 101, 0, 0), MDS_OK);
    ASSERT_TRUE(stat(p, &st) == 0);
    placement_gate_destroy(); mds_proxy_ctx_destroy(proxy); rm_ds_dir(dir); ds_cache_destroy(cache); mds_catalogue_close(cat);
}
static void test_token_is_bound_to_ds_and_age(void)
{
    struct placement_token t = { 1, PP_NEW_OBJECT, 1000, 0 };
    ASSERT_EQ(placement_token_valid(&t, 1, 2999), true);
    ASSERT_EQ(placement_token_valid(&t, 1, 3001), false);
    ASSERT_EQ(placement_token_valid(&t, 2, 1500), false);
    ASSERT_EQ(placement_token_valid(NULL, 1, 1500), false);
}
static void test_create_refuses_without_token(void)
{
    char *dir = make_ds_dir(); struct mds_proxy_ctx *proxy = NULL;
    ASSERT_EQ(mds_proxy_ctx_create(&proxy), MDS_OK);
    ASSERT_EQ(mds_proxy_mount_set(proxy, 1, dir), MDS_OK);
    ASSERT_EQ(mds_proxy_create_ds_file(proxy, 1, 5, 0, 0, NULL), MDS_ERR_INVAL);
    struct placement_token wrong = { 2, PP_NEW_OBJECT, ds_cache_mono_ms(), 0 };
    ASSERT_EQ(mds_proxy_create_ds_file(proxy, 1, 5, 0, 0, &wrong), MDS_ERR_INVAL);
    mds_proxy_ctx_destroy(proxy); rm_ds_dir(dir);
}
static void test_legacy_mode_creates_freely(void)
{
    char *dir = make_ds_dir(); struct mds_proxy_ctx *proxy = NULL;
    ASSERT_EQ(mds_proxy_ctx_create(&proxy), MDS_OK);
    ASSERT_EQ(mds_proxy_mount_set(proxy, 1, dir), MDS_OK);
    ASSERT_EQ(placement_gate_mode(), PM_LEGACY);
    ASSERT_EQ(mds_proxy_ensure_ds_file(proxy, 1, 7, 0, 0), MDS_OK);
    mds_proxy_ctx_destroy(proxy); rm_ds_dir(dir);
}
```

Register `test_placement_create_boundary` in `tests/CMakeLists.txt` (executable + foreach).

- [ ] **Step 2: Run to verify it fails** — `mds/scripts/pm-run.sh test_placement_create_boundary` → `PM_RESULT BUILD_FAILED`.

- [ ] **Step 3: Implement** as described under Interfaces. In `mds_proxy_lookup_ds_file_fh`, the local path branch uses `access(file_path, F_OK)` then `proxy_name_to_handle`; the RPC fallback runs `ds_nfs3_lookup_fh` (a LOOKUP never creates). `mds_proxy_create_ds_file_fh` = today's body (mkdir + `open(O_WRONLY|O_CREAT)` + `fchmod` + handle) guarded by `placement_token_valid`.

- [ ] **Step 4: Run to verify it passes** — `mds/scripts/pm-run.sh "test_placement_create_boundary|test_proxy_io|test_ds_prealloc_batch|test_compound"` → PASS; grep proof: `grep -n "O_CREAT" src/mds/proxy_io.c` shows only the fd-cache flag comment/constant lines and the two `mds_proxy_create_ds_file*` bodies.

- [ ] **Step 5: Commit**

```bash
git add include/proxy_io.h src/mds/proxy_io.c include/placement_gate.h src/fsal_obj/placement_gate.c tests/unit/test_placement_create_boundary.c tests/CMakeLists.txt && git commit -m "feat(proxy_io): split lookup from create; every DS file creation needs a placement admission token"
```

---

### Task 8: `config show` rows and rejection reasons in logs

**Files:**
- Modify: `src/cluster/cluster_transport.c` (`render_cfg_placement`), `src/mds/compound_layout.c` / `compound_data_io.c` (reason in the existing NOSPC log lines)
- Test: `tests/unit/test_mds_admin.c` (append a render test if `render_config_show` is reachable; otherwise `tests/unit/test_cluster_transport.c` where `config show` round-trips exist — pick the file that already tests `config show` and extend it)

**Interfaces:**
- Consumes: Task 5 `placement_gate_mode()`, `placement_gate_generation()`, `placement_gate_ctx()`.
- Produces: `config show` prints `placement_mode=<legacy|rr|fill|smart>`, `placement_mode_effective=<same>`, `placement_config_generation=<hex|->`, `placement_kernel_id=<hex>`, and per registered DS `placement_ds.<id>=domain=<d> state=<ONLINE|…> capacity_age_ms=<n|unknown> avail=<bytes> total=<bytes> weight=<n> reason=<NONE|…>`.

- [ ] **Step 1: Write the failing test** — a `render_config_show`-level test: load a `fill` config, init the gate with a memdb cache holding DS 1 with a fresh observation, call `cluster_transport_request_config_show` against a loopback server as the existing `config show` test does, and assert the buffer contains `placement_mode=fill`, `placement_config_generation=` followed by 64 hex chars, and `placement_ds.1=domain=ds:1 state=ONLINE`.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** — extend `render_cfg_placement` with the `RENDER_KEY` pattern used there; per-DS rows come from `placement_gate_ctx()` + `placement_candidates()` over `ds_cache`'s current list so `reason` is the live gate verdict. Add `placement_reason_name(why)` to the two `NFS4ERR_NOSPC` log lines at the selection sites (`MDS_LOG_WARN(LOG_COMP_MDS, "LAYOUTGET fileid=%llu: no placement (%s)", …)`).

- [ ] **Step 4: Run to verify it passes** — `mds/scripts/pm-run.sh "test_cluster_transport|test_mds_admin"`.

- [ ] **Step 5: Commit** — `git commit -m "feat(admin): config show reports placement mode, generation, kernel and per-DS gate verdicts"`.

---

### Task 9: Docs, examples, CI, manifest and patch export

**Files:**
- Fork — Create: `docs/placement-modes.md`, `docs/examples/mds.conf.rr`, `docs/examples/mds.conf.fill`, `.github/workflows/placement-modes.yml`
- XinnorLab/pNFS — Create: `docs/placement-modes/contract-manifest.json`, `mds/manifest.json`, `scripts/export-patches.sh`, `mds/patches/6b4dcde/*.patch`; Modify: `README.md`, `modules/wrr/README.md` (the fork's `src/modules/wrr/wrr.c` is now canonical), `docs/TODO.md` (Stage B/C and §14 deferrals)

- [ ] **Step 1: Write the operator doc** `docs/placement-modes.md` (fork): the mode table from spec §1, the key table from spec §4 with defaults, the validation error catalogue (`PLACEMENT_MODE_CONFLICT`, `PLACEMENT_MODE_UNSUPPORTED_BUILD`, `RANGE`, `DOMAIN_WEIGHT_FORBIDDEN`, `MIRROR_COUNT_UNSUPPORTED`), the alias rules (declare `ds_capacity_domain`, grades a/b/c), the reason names and what an operator does about each, the startup line format, the `config show` keys, and the explicit statement that `smart` is `NOT_READY` in this stage. Example configs:

```ini
# docs/examples/mds.conf.fill
placement_mode = fill
ds_capacity_poll_ms = 30000
placement_capacity_max_age_ms = 90000
placement_min_free_bytes = 1073741824
# two exports of one xiNAS filesystem = one capacity domain
ds_capacity_domain.0 = xinas-01/fs-uuid-01
ds_capacity_domain.1 = xinas-01/fs-uuid-01
```

- [ ] **Step 2: Contract manifest** `docs/placement-modes/contract-manifest.json` (pNFS) — one object per key: `{ "key": "placement_mode", "values": ["rr","fill","smart"], "default": null, "applies_to": ["all"] }`, … for every key in spec §4, plus `"weight": { "scale": 65536, "domain_weight_manual_range": [1, 10000], "max_ds": 256, "sum_bound_log2": 62 }` and `"reasons": [ …PR_ names… ]`. A test in the fork (`tests/unit/test_placement_config.c`, one more case) asserts `PM_WEIGHT_SCALE == 65536`, `PM_DOMAIN_WEIGHT_MAX == 10000`, `PM_DEFAULT_CAP_MAX_AGE_MS == 120000` — the numbers the manifest states; Stage C adds the cross-repo consistency job.

- [ ] **Step 3: CI workflow** (fork) `.github/workflows/placement-modes.yml`: on push/PR to `xinnor/**`; ubuntu-latest; apt as in `qa.yml` (`cmake`, `libntirpc-dev`, `libssl-dev`, …); `cmake -S . -B build -DENABLE_RONDB=OFF -DENABLE_TESTS=ON -DENABLE_EBPF=OFF -DENABLE_WRR=ON -DENABLE_DS_PREALLOC=OFF`; build; `ctest --test-dir build --output-on-failure`; proofs: `nm build/src/mds/pnfs-mds | grep -q " T mds_wrr_kernel_id"` and `./build/tests/test_wrr_kernel` (kernel id), the `placement_select` grep and the `O_CREAT` grep from the Global Constraints, each as a shell step that fails on a hit.

- [ ] **Step 4: Patch export + manifest** (pNFS): `scripts/export-patches.sh` = `git -C "$FORK" format-patch -o mds/patches/6b4dcde --zero-commit --no-signature 6b4dcde..xinnor/placement-modes` then writes `mds/manifest.json` `{ "upstream_sha": "6b4dcde3cd6a8b02e8a012695ff0c29e93a0caf4", "fork": "XinnorLab/pnfs-lattice", "fork_branch": "xinnor/placement-modes", "fork_sha": "<git rev-parse>", "patches": [ { "file": "…", "sha256": "…" } ], "cmake_flags": ["-DENABLE_WRR=ON", "-DENABLE_DS_PREALLOC=OFF", "-DENABLE_TESTS=ON"], "connector_profile_digest": "<from connectors/…/docs/compatibility-manifest.json>" }`. Run it, commit the patches.

- [ ] **Step 5: Run the full fork suite once** — `mds/scripts/pm-run.sh all` → `100% tests passed`.

- [ ] **Step 6: Commit** (fork: docs + workflow; pNFS: manifest, script, patches, README/TODO) and push both (`git push origin xinnor/placement-modes`; pNFS `git push origin main`). Open no PR in the fork yet — the branch is the integration line until Stage B.

---

### Task 10: Stand trial for `rr` and `fill` (announced; restarts the lab MDS)

**Files:**
- Create (pNFS): `mds/scripts/pm-deploy.sh` (from `modules/wrr/scripts/wrr_deploy.sh`: builds on node225 from `/home/lattice/pnfs-lattice-pm/build`, installs to both MDS keeping `/usr/local/bin/pnfs-mds.wrr` as the previous binary, writes the managed placement keys), `mds/scripts/pm-trial.sh` (from `wrr_trial.sh`: 40 files via node225 → MDS 1, counts per DS)
- Create: `docs/placement-modes/stand-2026-09-xx.md` (results)

Pre-condition (from Sergey): explicit go-ahead in chat for the maintenance window; the announce message lists both MDS, the expected downtime (two restarts of ~30 s each) and the rollback (`install pnfs-mds.wrr` + old `mds.conf` from the backup the deploy script takes).

- [ ] **Step 1: `rr`** — deploy with `placement_mode = rr` (legacy keys removed); startup line shows `placement_mode=rr`; 40 single-stripe files → expect ≈20:20 alternating by registry order; `mds-admin config show --mds-port 50051` (verify the port: the lab's `grpc_port`) prints the placement rows.
- [ ] **Step 2: `fill`** — deploy with `placement_mode = fill`, `ds_capacity_poll_ms = 10000`, `placement_capacity_max_age_ms = 30000`; both DS ≈98 % free → expect ≈20:20; then fill DS 1 (node 71) to ≈20 % free with `fallocate` on its export (announce; file removed after) → expect ≈4:1 over 100 files; then `placement_min_free_bytes` above DS 1's free bytes → expect 100:0 and the `CAPACITY_FULL` counter; stop `xinas-agent`? (no — capacity is statvfs; instead unmount `/mnt/ds1` on MDS 1 → `CAPACITY_STALE` after 30 s, files go only to DS 0, no NOSPC while DS 0 is fresh).
- [ ] **Step 3: legacy regression** — deploy the old `mds.conf` (`placement_policy = wrr`, `ds_weight.0 = 55`, `ds_weight.1 = 45`) on the new binary → 100 files ≈ 57:43 as on 2026-09-22, startup line unchanged (`placement_policy=wrr (dispatcher active)`).
- [ ] **Step 4: record** the counts, the `config show` output and the metrics lines in `docs/placement-modes/stand-2026-09-xx.md`; leave the MDS on the legacy config unless Sergey says otherwise; commit and push.

---

## Self-review (done while writing)

- Spec coverage, Stage A scope: §1 modes (T2, T4, T6), §2 baseline (T1–T7 cite the lines), §3 single entry (T4/T6), §4 keys/validation/generation (T2), §5 gate/weights/inventory/race snapshot (T4/T5/T6), §5a create boundary + token (T7), §6 capacity record/freshness/min-free/domains/aliases (T3/T4), §8 kernel (T1), §9 startup line/config show/metrics (T5/T8), §12 packaging/CI/examples (T9), §13 unit/fairness/integration/stand rows for rr/fill (T1–T7, T10). Not in Stage A by design: §7 (smart client), §10 (CLI), §11 (preflight), perf row of §13 (Stage C).
- Placeholders: none — every step names the file, the function and the assertion; Task 8's test refers to the existing `config show` test file, which the implementer locates by `grep -n config_show tests/unit/*.c`.
- Type consistency: `placement_ctx`, `placement_candidate`, `placement_reject_counts`, `placement_reason`, `placement_token`, `ds_capacity_obs`, `ds_capacity_view_row`, `placement_weight(domain_weight, ppm, n, *overflow)`, `placement_admit(ctx, ds_list, n, *stripe_count, mirror_count, rr_key, entries, *reason)`, `placement_select_gated(...)`, `mds_wrr_weighted_pick2(w, n, *out)` are used with the same shapes in every task.
