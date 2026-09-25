# Per-profile digest pins — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one connector publish assessments from several profiles and let the MDS pin each profile by id, replacing the one-digest-per-batch rule in the MDS client, the connector preflight and the `lattice-placement` CLI.

**Architecture:** A shared C parser (`pm_parse_profile_pins`) turns `ds_connector_expected_profiles = id=digest,...` into a sorted pin array used by the config loader, the config generation and the connector client. The client builds the batch's `id → digest` map from every well-formed record (drop on an inconsistency or more than 8 ids), checks each record against the pins by id, and publishes the map in the assessment view; `config show` prints it as `placement_connector_profiles`. The connector and the CLI follow the same rule in Python.

**Tech Stack:** C11 + cmocka-style unit tests (pnfs-lattice fork, CMake, run by the fork CI only), Python 3 + pytest (pNFS connector and CLI).

**Spec:** `docs/superpowers/specs/2026-09-25-per-profile-digest-design.md` (pNFS). Read it before any task.

## Global Constraints

- Profile id: `^[A-Za-z0-9._-]{1,63}$` everywhere (connector config, batch schema, MDS record parser, MDS key, CLI).
- At most 8 profiles: `PM_PROFILES_MAX = 8` (C), `MAX_PROFILES = 8` (Python), manifest `max_items: 8`.
- New MDS key: `ds_connector_expected_profiles`; the old key `ds_connector_expected_profile_digest` is a config error in the MDS and `LEGACY_KEY` in the CLI.
- New `config show` row: `placement_connector_profiles` (`id=digest,...` sorted by id, `-` when empty); `placement_connector_profile_digest` is removed.
- New drop reasons: `DC_PROFILE_INCONSISTENT` / `"PROFILE_INCONSISTENT"`, `DC_PROFILE_LIMIT` / `"PROFILE_LIMIT"`.
- The digest is not part of the per-DS binding pin (a reload is not a rebind).
- Repositories: fork `~/Documents/GitHub/pnfs-lattice-profile-map` (branch `xinnor/profile-map`, PR into `xinnor/placement-modes`); pNFS `~/Documents/GitHub/pNFS/.claude/worktrees/profile-map` (branch `fix/profile-map`, PR into `main`). Branches and PRs, no direct pushes to the base branches.
- The fork's C tests cannot run on this Mac (no CMake, no container runtime). Each fork task ends with a syntax check (`cc -fsyntax-only`, below) and the fork CI (`placement-modes.yml`) is the gate. Python: `.venv/bin/python -m pytest -q` from `connectors/lattice-ds-connector` and from `tools/lattice-placement`.
- Commit messages: Conventional Commits, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

Syntax check used by the fork tasks (from the fork root):

```bash
for f in src/common/placement_config.c src/common/config.c src/mds/ds_connector.c \
         src/fsal_obj/placement_gate.c src/cluster/cluster_transport.c \
         tests/unit/test_placement_config.c tests/unit/test_ds_connector.c; do
  cc -std=gnu11 -fsyntax-only -Iinclude -Isrc -Ithird_party -DENABLE_DS_CONNECTOR=1 -DENABLE_WRR=1 "$f" 2>&1 \
    | grep -E 'error' | grep -v -E "file not found|unknown type name '(SVCXPRT|CLIENT|XDR)" || true
done
```

Headers from system packages the Mac lacks (ntirpc, grpc, lmdb) produce "file not found"; those are filtered. Any other error is real and must be fixed before committing. If a file cannot be checked at all because of a missing header, say so in the task report instead of claiming it compiles.

---

## Part A — pnfs-lattice fork

### Task 1: Profile pin type, parser and formatter

**Files:**
- Modify: `include/placement_modes.h` (key names near line 59, constants near line 71, drop enum near line 113)
- Modify: `src/common/placement_config.c` (drop names near line 76; new functions at the end of the file)
- Test: `tests/unit/test_placement_config.c`

**Interfaces:**
- Produces:
  - `#define PM_KEY_CONN_PROFILES "ds_connector_expected_profiles"`
  - `#define PM_PROFILES_MAX 8`, `#define PM_PROFILE_ID_MAX 64` (63 chars + NUL)
  - `struct pm_profile_pin { char id[PM_PROFILE_ID_MAX]; char digest[PM_DIGEST_MAX]; };`
  - `bool pm_profile_id_valid(const char *s, size_t len);`
  - `int pm_parse_profile_pins(const char *val, struct pm_profile_pin out[PM_PROFILES_MAX], uint32_t *count, char *err, size_t errcap);` — 0 ok (sorted by id), -1 error (message in `err`).
  - `int pm_format_profile_pins(const struct pm_profile_pin *p, uint32_t n, char *buf, size_t cap);` — bytes written, -1 on overflow; `"-"` when `n == 0`.
  - `enum ds_connector_drop` gains `DC_PROFILE_INCONSISTENT`, `DC_PROFILE_LIMIT` (before `DC_COUNT`).

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_placement_config.c` before `main`, and add `RUN_TEST(test_profile_pins);` after `RUN_TEST(test_manifest_constants);`:

```c
static void test_profile_pins(void)
{
    struct pm_profile_pin p[PM_PROFILES_MAX];
    uint32_t n = 99;
    char err[160];
    char buf[PM_PROFILES_MAX * (PM_PROFILE_ID_MAX + PM_DIGEST_MAX + 1)];
    static const char *bad[] = {
        "",
        "xinas-mvp",
        "=sha256:a",
        "xinas-mvp=",
        "a b=sha256:a",
        "a=sha256:x,a=sha256:y",
        "x=d,,y=d",
        "p1=d,p2=d,p3=d,p4=d,p5=d,p6=d,p7=d,p8=d,p9=d",
        "0123456789012345678901234567890123456789012345678901234567890123=d",   /* 64 chars */
    };

    ASSERT_EQ(pm_parse_profile_pins("zfs-mvp=sha256:b, xinas-mvp=sha256:a", p, &n, err, sizeof(err)), 0);
    ASSERT_EQ(n, 2u);
    ASSERT_EQ(strcmp(p[0].id, "xinas-mvp"), 0);
    ASSERT_EQ(strcmp(p[0].digest, "sha256:a"), 0);
    ASSERT_EQ(strcmp(p[1].id, "zfs-mvp"), 0);
    ASSERT_EQ(strcmp(p[1].digest, "sha256:b"), 0);
    ASSERT_EQ(pm_format_profile_pins(p, n, buf, sizeof(buf)) > 0, 1);
    ASSERT_EQ(strcmp(buf, "xinas-mvp=sha256:a,zfs-mvp=sha256:b"), 0);
    ASSERT_EQ(pm_format_profile_pins(p, 0, buf, sizeof(buf)), 1);
    ASSERT_EQ(strcmp(buf, "-"), 0);
    ASSERT_EQ(pm_format_profile_pins(p, n, buf, 8), -1);
    ASSERT_EQ(pm_profile_id_valid("xinas-mvp.v1_2", 14), true);
    ASSERT_EQ(pm_profile_id_valid("a=b", 3), false);
    ASSERT_EQ(pm_profile_id_valid("", 0), false);
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        err[0] = '\0';
        ASSERT_EQ(pm_parse_profile_pins(bad[i], p, &n, err, sizeof(err)), -1);
        ASSERT_EQ(err[0] != '\0', 1);
    }
    ASSERT_EQ(strcmp(ds_connector_drop_name(DC_PROFILE_INCONSISTENT), "PROFILE_INCONSISTENT"), 0);
    ASSERT_EQ(strcmp(ds_connector_drop_name(DC_PROFILE_LIMIT), "PROFILE_LIMIT"), 0);
}
```

- [ ] **Step 2: Run the syntax check** (see Global Constraints). Expected: errors in `test_placement_config.c` — `pm_profile_pin`, `PM_PROFILES_MAX`, `DC_PROFILE_INCONSISTENT` undeclared. This is the RED state available on this machine.

- [ ] **Step 3: Implement.** In `include/placement_modes.h` add after the `PM_KEY_CONN_PROFILE_DIGEST` line (that line stays until Task 2 removes its last user):

```c
#define PM_KEY_CONN_PROFILES         "ds_connector_expected_profiles"
```

After `#define PM_DIGEST_MAX 128` add:

```c
#define PM_PROFILES_MAX              8
#define PM_PROFILE_ID_MAX            64    /* [A-Za-z0-9._-]{1,63} + NUL */

/* One pinned connector profile: ds_connector_expected_profiles item, and
 * one entry of a batch's profile map (per-profile digest design, §3). */
struct pm_profile_pin {
    char id[PM_PROFILE_ID_MAX];
    char digest[PM_DIGEST_MAX];
};

bool pm_profile_id_valid(const char *s, size_t len);
/* "id=digest[,id=digest...]" -> out[] sorted by id; 0 ok, -1 error (err set). */
int  pm_parse_profile_pins(const char *val, struct pm_profile_pin out[PM_PROFILES_MAX],
                           uint32_t *count, char *err, size_t errcap);
/* "id=digest,..." or "-" when n == 0; bytes written, -1 when cap is too small. */
int  pm_format_profile_pins(const struct pm_profile_pin *p, uint32_t n,
                            char *buf, size_t cap);
```

Make sure `placement_modes.h` includes `<stdbool.h>`, `<stddef.h>` and `<stdint.h>` (add the missing ones at the top).

In `enum ds_connector_drop`, before `DC_COUNT`:

```c
    DC_PROFILE_INCONSISTENT, /* one profile id with two digests in one batch */
    DC_PROFILE_LIMIT,        /* more than PM_PROFILES_MAX profile ids in one batch */
```

In `src/common/placement_config.c`, in `drop_names[]` after `[DC_TOO_LARGE] = "TOO_LARGE",`:

```c
    [DC_PROFILE_INCONSISTENT] = "PROFILE_INCONSISTENT",
    [DC_PROFILE_LIMIT] = "PROFILE_LIMIT",
```

Append to `src/common/placement_config.c` (add `#include <ctype.h>` if absent):

```c
bool pm_profile_id_valid(const char *s, size_t len)
{
    size_t i;

    if (s == NULL || len == 0 || len >= PM_PROFILE_ID_MAX) {
        return false;
    }
    for (i = 0; i < len; i++) {
        unsigned char c = (unsigned char)s[i];

        if (!(isalnum(c) || c == '.' || c == '_' || c == '-')) {
            return false;
        }
    }
    return true;
}

static int pin_cmp(const void *a, const void *b)
{
    return strcmp(((const struct pm_profile_pin *)a)->id,
                  ((const struct pm_profile_pin *)b)->id);
}

static void trim(const char **s, size_t *len)
{
    while (*len > 0 && isspace((unsigned char)**s)) {
        (*s)++;
        (*len)--;
    }
    while (*len > 0 && isspace((unsigned char)(*s)[*len - 1])) {
        (*len)--;
    }
}

int pm_parse_profile_pins(const char *val, struct pm_profile_pin out[PM_PROFILES_MAX],
                          uint32_t *count, char *err, size_t errcap)
{
    const char *p = val;
    uint32_t n = 0;
    uint32_t i;

    *count = 0;
    if (val == NULL || val[0] == '\0') {
        (void)snprintf(err, errcap, "empty value");
        return -1;
    }
    for (;;) {
        const char *end = strchr(p, ',');
        size_t len = end != NULL ? (size_t)(end - p) : strlen(p);
        const char *eq = memchr(p, '=', len);
        const char *id = p;
        const char *dg;
        size_t idl;
        size_t dgl;

        if (eq == NULL) {
            (void)snprintf(err, errcap, "item '%.*s' is not id=digest", (int)len, p);
            return -1;
        }
        idl = (size_t)(eq - p);
        dg = eq + 1;
        dgl = len - idl - 1;
        trim(&id, &idl);
        trim(&dg, &dgl);
        if (!pm_profile_id_valid(id, idl)) {
            (void)snprintf(err, errcap, "profile id '%.*s' must match [A-Za-z0-9._-]{1,63}",
                           (int)idl, id);
            return -1;
        }
        if (dgl == 0 || dgl >= PM_DIGEST_MAX) {
            (void)snprintf(err, errcap, "profile %.*s: digest must be 1..%d bytes",
                           (int)idl, id, PM_DIGEST_MAX - 1);
            return -1;
        }
        if (n == PM_PROFILES_MAX) {
            (void)snprintf(err, errcap, "more than %d profiles", PM_PROFILES_MAX);
            return -1;
        }
        (void)snprintf(out[n].id, sizeof(out[n].id), "%.*s", (int)idl, id);
        (void)snprintf(out[n].digest, sizeof(out[n].digest), "%.*s", (int)dgl, dg);
        for (i = 0; i < n; i++) {
            if (strcmp(out[i].id, out[n].id) == 0) {
                (void)snprintf(err, errcap, "profile %s pinned twice", out[n].id);
                return -1;
            }
        }
        n++;
        if (end == NULL) {
            break;
        }
        p = end + 1;
    }
    qsort(out, n, sizeof(out[0]), pin_cmp);
    *count = n;
    return 0;
}

int pm_format_profile_pins(const struct pm_profile_pin *p, uint32_t n,
                           char *buf, size_t cap)
{
    size_t off = 0;
    uint32_t i;
    int w;

    if (cap == 0) {
        return -1;
    }
    if (n == 0) {
        w = snprintf(buf, cap, "-");
        return (w < 0 || (size_t)w >= cap) ? -1 : w;
    }
    for (i = 0; i < n; i++) {
        w = snprintf(buf + off, cap - off, "%s%s=%s", i == 0 ? "" : ",", p[i].id, p[i].digest);
        if (w < 0 || (size_t)w >= cap - off) {
            buf[0] = '\0';
            return -1;
        }
        off += (size_t)w;
    }
    return (int)off;
}
```

- [ ] **Step 4: Run the syntax check.** Expected: no new errors in any listed file (Task 1 only adds declarations).

- [ ] **Step 5: Commit**

```bash
git add include/placement_modes.h src/common/placement_config.c tests/unit/test_placement_config.c
git commit -m "feat(placement): profile pin map parser, formatter and drop reasons

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: `ds_connector_expected_profiles` in the MDS config

**Files:**
- Modify: `include/pnfs_mds.h:744` (the `ds_connector_expected_profile_digest` field)
- Modify: `src/common/config.c:1427-1450` (the connector string keys)
- Modify: `src/common/placement_config.c` (`placement_config_generation`, the `conn_profile=` line)
- Test: `tests/unit/test_placement_config.c` (the smart-defaults test around line 95 and `test_smart_connector_conflicts_and_ranges`)

**Interfaces:**
- Consumes: `pm_parse_profile_pins`, `pm_format_profile_pins`, `struct pm_profile_pin`, `PM_KEY_CONN_PROFILES`, `PM_KEY_CONN_PROFILE_DIGEST_REMOVED` (Task 1).
- Produces: `#define PM_KEY_CONN_PROFILE_DIGEST_REMOVED "ds_connector_expected_profile_digest"` (replaces `PM_KEY_CONN_PROFILE_DIGEST`); `struct mds_config` fields `uint32_t ds_connector_expected_profile_count;` and `struct pm_profile_pin ds_connector_expected_profiles[PM_PROFILES_MAX];` (replacing `ds_connector_expected_profile_digest`).

- [ ] **Step 1: Update the tests.** In the smart-defaults test replace

```c
    ASSERT_EQ(cfg.ds_connector_expected_profile_digest[0], '\0');
```

with

```c
    ASSERT_EQ(cfg.ds_connector_expected_profile_count, 0u);
```

In the "explicit, consistent values" INI string replace `ds_connector_expected_profile_digest = sha256:abc\n` with `ds_connector_expected_profiles = zfs-mvp=sha256:z,xinas-mvp=sha256:abc\n`, and replace the final assertion with:

```c
    ASSERT_EQ(cfg.ds_connector_expected_profile_count, 2u);
    ASSERT_EQ(strcmp(cfg.ds_connector_expected_profiles[0].id, "xinas-mvp"), 0);
    ASSERT_EQ(strcmp(cfg.ds_connector_expected_profiles[0].digest, "sha256:abc"), 0);
    ASSERT_EQ(strcmp(cfg.ds_connector_expected_profiles[1].id, "zfs-mvp"), 0);
    {
        /* the pins are part of the config generation */
        struct mds_config other; char p2[128];
        ASSERT_EQ(write_tmp_ini("placement_mode = smart\nds_connector_enabled = true\nds_connector_socket = /tmp/c.sock\nds_connector_poll_ms = 5000\nds_connector_request_deadline_ms = 4000\nds_connector_max_ds = 8\nds_connector_access_scope = lab\nds_connector_expected_profiles = xinas-mvp=sha256:abc\n", p2), 0);
        ASSERT_EQ(mds_config_load(p2, &other), MDS_OK);
        ASSERT_EQ(strcmp(cfg.placement_config_generation, other.placement_config_generation) != 0, 1);
    }
```

Add to `bad[]` in `test_smart_connector_conflicts_and_ranges`:

```c
        "placement_mode = smart\nds_connector_expected_profile_digest = sha256:abc\n",   /* removed key */
        "placement_mode = smart\nds_connector_expected_profiles = \n",
        "placement_mode = smart\nds_connector_expected_profiles = bad id=sha256:a\n",
        "placement_mode = smart\nds_connector_expected_profiles = a=x,a=y\n",
```

- [ ] **Step 2: Run the syntax check.** Expected: errors — `no member named 'ds_connector_expected_profile_count'`.

- [ ] **Step 3: Implement.** In `include/placement_modes.h` replace the `PM_KEY_CONN_PROFILE_DIGEST` line with

```c
/* Replaced by PM_KEY_CONN_PROFILES; its presence is a config error. */
#define PM_KEY_CONN_PROFILE_DIGEST_REMOVED "ds_connector_expected_profile_digest"
```

In `include/pnfs_mds.h` replace

```c
    char                  ds_connector_expected_profile_digest[PM_DIGEST_MAX];
```

with

```c
    uint32_t              ds_connector_expected_profile_count;
    struct pm_profile_pin ds_connector_expected_profiles[PM_PROFILES_MAX];
```

In `src/common/config.c`, remove `strcmp(key, PM_KEY_CONN_PROFILE_DIGEST) == 0 ||` from the string-key condition and its `else if` branch, and add before that `else if`:

```c
        } else if (strcmp(key, PM_KEY_CONN_PROFILE_DIGEST_REMOVED) == 0) {
            (void)fprintf(stderr,
                "ERROR: %s was replaced by %s = <profile-id>=<digest>[,...]\n",
                key, PM_KEY_CONN_PROFILES);
            (void)fclose(fp);
            return MDS_ERR_INVAL;
        } else if (strcmp(key, PM_KEY_CONN_PROFILES) == 0) {
            char perr[160];

            if (pm_parse_profile_pins(val, cfg->ds_connector_expected_profiles,
                                      &cfg->ds_connector_expected_profile_count,
                                      perr, sizeof(perr)) != 0) {
                (void)fprintf(stderr, "ERROR: %s: %s\n", key, perr);
                (void)fclose(fp);
                return MDS_ERR_INVAL;
            }
```

(The branch continues into the existing `} else if (strcmp(key, PM_KEY_CONN_ACCESS_SCOPE) == 0 || ...` chain; keep the brace structure consistent with the neighbours.)

In `placement_config_generation` replace

```c
        APPEND("conn_profile=%s\n", cfg->ds_connector_expected_profile_digest);
```

with

```c
        {
            char pins[PM_PROFILES_MAX * (PM_PROFILE_ID_MAX + PM_DIGEST_MAX + 1)];

            if (pm_format_profile_pins(cfg->ds_connector_expected_profiles,
                                       cfg->ds_connector_expected_profile_count,
                                       pins, sizeof(pins)) < 0) {
                free(buf);
                return;
            }
            APPEND("conn_profiles=%s\n", pins);
        }
```

Check `cap` of the generation buffer: it must hold the pins line (≤ 8 × 193 + 16 bytes). If `cap` is computed from a fixed size, add `PM_PROFILES_MAX * (PM_PROFILE_ID_MAX + PM_DIGEST_MAX + 1)` to it.

- [ ] **Step 4: Run the syntax check.** Expected: `config.c`, `placement_config.c` and `test_placement_config.c` clean. `ds_connector.c` (`ds_connector_configure`) still reads the removed field and fails; that one error is fixed in Task 3 — note it in the commit body.

- [ ] **Step 5: Commit**

```bash
git add include/pnfs_mds.h src/common/config.c src/common/placement_config.c tests/unit/test_placement_config.c
git commit -m "feat(config): ds_connector_expected_profiles replaces the single profile digest pin

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: Per-profile rule in the connector client, profile map in the view

**Files:**
- Modify: `include/ds_connector.h` (`struct ds_connector_cfg`, `struct ds_connector_pin`)
- Modify: `include/placement_gate.h:63` (`placement_assessment_view.profile_digest`) and `:170` (`placement_readiness.profile_digest`)
- Modify: `src/mds/ds_connector.c` (`struct rec` ~457, `parse_record` ~555, `pin_matches`/`pin_set` ~612-645, `ds_connector_apply_batch` ~660-1110, `ds_connector_configure` ~1579)
- Modify: `src/fsal_obj/placement_gate.c:910`
- Modify: `src/cluster/cluster_transport.c:6694`
- Test: `tests/unit/test_ds_connector.c`

**Interfaces:**
- Consumes: Task 1 and Task 2.
- Produces:
  - `struct ds_connector_cfg`: `uint32_t expected_profile_count; struct pm_profile_pin expected_profiles[PM_PROFILES_MAX];` (replacing `expected_profile_digest`).
  - `struct placement_assessment_view` and `struct placement_readiness`: `uint32_t profile_count; struct pm_profile_pin profiles[PM_PROFILES_MAX];` (replacing `profile_digest`).
  - `config show` row `placement_connector_profiles`.

- [ ] **Step 1: Write the failing tests.** In `tests/unit/test_ds_connector.c`:

Change `st_init` to take a pin list:

```c
static void st_init(const char *profile_pins, const char *config_pin)
{
    struct ds_connector_cfg cfg;
    char err[160];
    memset(&cfg, 0, sizeof(cfg));
    cfg.contract_major = 1;
    cfg.max_ds = 256;
    snprintf(cfg.access_scope, sizeof(cfg.access_scope), "cluster-default");
    if (profile_pins) {
        if (pm_parse_profile_pins(profile_pins, cfg.expected_profiles,
                                  &cfg.expected_profile_count, err, sizeof(err)) != 0) {
            fprintf(stderr, "  bad test pins %s: %s\n", profile_pins, err);
            abort();
        }
    }
    if (config_pin) snprintf(cfg.expected_config_digest, sizeof(cfg.expected_config_digest), "%s", config_pin);
    ds_connector_state_init(&ST, &cfg);
}
```

Every existing call `st_init("sha256:p", ...)` becomes `st_init("xinas-mvp=sha256:p", ...)`; `st_init(NULL, ...)` stays.

Turn `rec()` into a wrapper so a record can carry another profile id: rename the existing body to `rec_pid` with a leading `const char *pid` parameter and replace `\"profile\":{\"id\":\"xinas-mvp\",` in its format with `\"profile\":{\"id\":\"%s\",`, passing `pid` in the argument list at the matching position; then:

```c
static const char *rec(uint32_t ds, unsigned gen, const char *target, const char *inc,
                       const char *server, const char *path, unsigned port,
                       const char *scope, const char *access, const char *quality,
                       unsigned ttl, const char *profile, const char *allowed,
                       unsigned ppm, const char *domain_json)
{
    return rec_pid("xinas-mvp", ds, gen, target, inc, server, path, port, scope, access,
                   quality, ttl, profile, allowed, ppm, domain_json);
}

/* rec_ok(ds) with another profile. */
static const char *rec_prof(uint32_t ds, const char *pid, const char *digest)
{
    return rec_pid(pid, ds, 2, "mnt/data", "\"mnt/data:0:u\"",
                   ds == 0 ? "192.168.64.51" : "192.168.64.71", "/mnt/data", 2049,
                   "NEW_ALLOCATION", "cluster-default", "VALID", 15000, digest, "true",
                   1000000, "\"ctrl-1/fs-1/inc\"");
}
```

Replace `test_profile_digest_pin_and_consistency` with these tests and register them in `main` in its place:

```c
static void test_profiles_two_ids_without_pins(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    char recs[8192];
    reg_init(); st_init(NULL, NULL);
    snprintf(recs, sizeof(recs), "%s,%s", rec_prof(0, "zfs-mvp", "sha256:z"),
             rec_prof(1, "xinas-mvp", "sha256:p"));
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", recs), &v, &rep), DC_OK);
    ASSERT_EQ(rep.accepted, 2u);
    ASSERT_EQ(rep.rejected_binding, 0u);
    ASSERT_EQ(v.profile_count, 2u);
    ASSERT_EQ(strcmp(v.profiles[0].id, "xinas-mvp"), 0);     /* sorted by id */
    ASSERT_EQ(strcmp(v.profiles[1].id, "zfs-mvp"), 0);
    ASSERT_EQ(strcmp(v.profiles[1].digest, "sha256:z"), 0);
}

static void test_profiles_pinned_by_id(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    char recs[8192];
    snprintf(recs, sizeof(recs), "%s,%s", rec_prof(0, "xinas-mvp", "sha256:p"),
             rec_prof(1, "zfs-mvp", "sha256:z"));
    /* both pinned */
    reg_init(); st_init("xinas-mvp=sha256:p,zfs-mvp=sha256:z", NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", recs), &v, &rep), DC_OK);
    ASSERT_EQ(rep.accepted, 2u);
    /* an unpinned id: only its record is rejected */
    reg_init(); st_init("xinas-mvp=sha256:p", NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", recs), &v, &rep), DC_OK);
    ASSERT_EQ(rep.accepted, 1u);
    ASSERT_EQ(rep.rejected_binding, 1u);
    ASSERT_EQ(v.rows[1].present, false);
    ASSERT_TRUE(strstr(rep.detail, "not pinned") != NULL);
    /* a wrong digest for a pinned id: only its record is rejected */
    reg_init(); st_init("xinas-mvp=sha256:p,zfs-mvp=sha256:other", NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", recs), &v, &rep), DC_OK);
    ASSERT_EQ(rep.accepted, 1u);
    ASSERT_EQ(rep.rejected_binding, 1u);
    ASSERT_TRUE(strstr(rep.detail, "!= pinned") != NULL);
}

static void test_profiles_inconsistent_batch_is_dropped(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    char recs[8192];
    const char *wrong_endpoint;
    reg_init(); st_init(NULL, NULL);
    snprintf(recs, sizeof(recs), "%s,%s", rec_prof(0, "xinas-mvp", "sha256:p"),
             rec_prof(1, "xinas-mvp", "sha256:q"));
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", recs), &v, &rep),
              DC_PROFILE_INCONSISTENT);
    ASSERT_EQ(v.batch_valid, false);
    ASSERT_TRUE(strstr(rep.detail, "xinas-mvp") != NULL);
    /* still dropped when the second record fails a binding check */
    wrong_endpoint = rec(1, 2, "mnt/data", "\"mnt/data:0:u\"", "10.9.9.9", "/mnt/data", 2049,
                         "NEW_ALLOCATION", "cluster-default", "VALID", 15000, "sha256:q", "true",
                         1000000, "\"ctrl-1/fs-1/inc\"");
    snprintf(recs, sizeof(recs), "%s,%s", rec_prof(0, "xinas-mvp", "sha256:p"), wrong_endpoint);
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", recs), &v, &rep),
              DC_PROFILE_INCONSISTENT);
}

static void test_profiles_limit_and_invalid_id(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    char recs[16384];
    char id[16];
    size_t off = 0;
    reg_init(); st_init(NULL, NULL);
    for (uint32_t ds = 0; ds < PM_PROFILES_MAX + 1; ds++) {
        snprintf(id, sizeof(id), "p%u", (unsigned)ds);
        off += (size_t)snprintf(recs + off, sizeof(recs) - off, "%s%s", ds == 0 ? "" : ",",
                                rec_prof(ds, id, "sha256:p"));
    }
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", recs), &v, &rep),
              DC_PROFILE_LIMIT);
    /* an id outside [A-Za-z0-9._-]{1,63} is a shape rejection */
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE",
                          rec_prof(0, "bad id", "sha256:p")), &v, &rep), DC_OK);
    ASSERT_EQ(rep.rejected_shape, 1u);
    ASSERT_EQ(rep.accepted, 0u);
}

static void test_profile_reload_with_and_without_pin(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    /* no pin: a reload (new digest, same binding tuple) is accepted -- the
     * digest is not part of the per-DS pin */
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE",
                          rec_prof(0, "xinas-mvp", "sha256:p")), &v, &rep), DC_OK);
    ASSERT_EQ(apply(batch("rt-1", "e1", 2, "2026-09-24T10:00:02Z", "c", "COMPLETE",
                          rec_prof(0, "xinas-mvp", "sha256:q")), &v, &rep), DC_OK);
    ASSERT_EQ(rep.accepted, 1u);
    ASSERT_EQ(strcmp(v.profiles[0].digest, "sha256:q"), 0);
    /* pinned to the old digest: the reloaded record is rejected */
    reg_init(); st_init("xinas-mvp=sha256:p", NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE",
                          rec_prof(0, "xinas-mvp", "sha256:q")), &v, &rep), DC_OK);
    ASSERT_EQ(rep.accepted, 0u);
    ASSERT_EQ(rep.rejected_binding, 1u);
}
```

Also replace the remaining `profile_digest` uses in the file: `ASSERT_EQ(strcmp(v.profile_digest, "sha256:p"), 0);` (around line 155) becomes

```c
    ASSERT_EQ(v.profile_count, 1u);
    ASSERT_EQ(strcmp(v.profiles[0].id, "xinas-mvp"), 0);
    ASSERT_EQ(strcmp(v.profiles[0].digest, "sha256:p"), 0);
```

and the readiness assertion around line 651 (`strcmp(r.profile_digest, "sha256:p")`) becomes

```c
    ASSERT_EQ(r.profile_count, 1u);
    ASSERT_EQ(strcmp(r.profiles[0].digest, "sha256:p"), 0);
```

- [ ] **Step 2: Run the syntax check.** Expected: errors — `no member named 'expected_profiles'`, `'profile_count'`, `'profiles'`.

- [ ] **Step 3: Implement.**

`include/ds_connector.h`: in `struct ds_connector_cfg` replace `char expected_profile_digest[PM_DIGEST_MAX];   /* "" = not pinned */` with

```c
    uint32_t expected_profile_count;                       /* 0 = not pinned */
    struct pm_profile_pin expected_profiles[PM_PROFILES_MAX];
```

and delete `char profile_digest[PM_DIGEST_MAX];` from `struct ds_connector_pin` (the digest is not part of the pin).

`include/placement_gate.h`: in both `struct placement_assessment_view` and `struct placement_readiness` replace `char profile_digest[PM_DIGEST_MAX];` with

```c
    uint32_t profile_count;                          /* profiles of the last accepted batch */
    struct pm_profile_pin profiles[PM_PROFILES_MAX]; /* sorted by id */
```

`src/mds/ds_connector.c`:

1. `struct rec`: add `char profile_id[PM_PROFILE_ID_MAX];` before `profile_digest`.
2. `parse_record`: replace the profile block with

```c
    pr = tok_get(d, obj, "profile");
    if (!tok_is(d, pr, JSMN_OBJECT) ||
        !tok_copy(d, tok_get(d, pr, "id"), r->profile_id, sizeof(r->profile_id)) ||
        !pm_profile_id_valid(r->profile_id, strlen(r->profile_id))) {
        set_detail(rep, "ds %u: profile.id invalid", r->ds_id);
        return false;
    }
    if (!tok_copy(d, tok_get(d, pr, "digest"), r->profile_digest, sizeof(r->profile_digest))) {
        set_detail(rep, "ds %u: profile.digest invalid", r->ds_id);
        return false;
    }
```

   (`tok_copy` fails on a value that does not fit the buffer, so a 64+ char id is rejected by it; `pm_profile_id_valid` rejects the characters.)
3. Update the comment above `pin_matches` to: `The pin excludes the profile on purpose: profiles are checked on every batch (pins by id + batch consistency), and a connector profile reload must not strand the DS in BINDING_MISMATCH until a process restart (review finding B-6).` Delete the `p->profile_digest` line from `pin_set`.
4. Add above `ds_connector_apply_batch`:

```c
static const struct pm_profile_pin *profile_find(const struct pm_profile_pin *p, uint32_t n,
                                                 const char *id)
{
    uint32_t i;

    for (i = 0; i < n; i++) {
        if (strcmp(p[i].id, id) == 0) {
            return &p[i];
        }
    }
    return NULL;
}

/* Record one well-formed record's profile in the batch map (design §3.4). */
static enum ds_connector_drop batch_profile_note(struct pm_profile_pin *map, uint32_t *n,
                                                 const struct rec *r,
                                                 struct ds_connector_report *rep)
{
    const struct pm_profile_pin *seen = profile_find(map, *n, r->profile_id);

    if (seen != NULL) {
        if (strcmp(seen->digest, r->profile_digest) != 0) {
            set_detail(rep, "profile %s: digests %s and %s in one batch",
                       r->profile_id, seen->digest, r->profile_digest);
            return DC_PROFILE_INCONSISTENT;
        }
        return DC_OK;
    }
    if (*n == PM_PROFILES_MAX) {
        set_detail(rep, "more than %d profiles in one batch", PM_PROFILES_MAX);
        return DC_PROFILE_LIMIT;
    }
    (void)snprintf(map[*n].id, sizeof(map[*n].id), "%s", r->profile_id);
    (void)snprintf(map[*n].digest, sizeof(map[*n].digest), "%s", r->profile_digest);
    (*n)++;
    return DC_OK;
}

static int profile_pin_cmp(const void *a, const void *b)
{
    return strcmp(((const struct pm_profile_pin *)a)->id,
                  ((const struct pm_profile_pin *)b)->id);
}
```

5. In `ds_connector_apply_batch`: replace `char batch_profile[PM_DIGEST_MAX];` with

```c
    struct pm_profile_pin batch_profiles[PM_PROFILES_MAX];
    uint32_t n_batch_profiles = 0;
```

   delete `batch_profile[0] = '\0';`, and directly after the `parse_record` failure branch (`rep->rejected_shape++; continue; }`) insert:

```c
            {
                enum ds_connector_drop pdrop =
                    batch_profile_note(batch_profiles, &n_batch_profiles, &r, rep);

                if (pdrop != DC_OK) {
                    drop = pdrop;
                    goto out_drop;
                }
            }
```

   The note runs before the `max_ds`, registry, duplicate and binding checks, so a record rejected by any of them still contributes to the map (design §3.4). `drop` itself is left untouched on success: every other path in the loop either `continue`s or sets it before `goto out_drop`.

   Replace the expected-digest check and the `batch_profile` block (the two `if` statements at ~999-1011) with:

```c
            if (st->cfg.expected_profile_count > 0) {
                const struct pm_profile_pin *pin =
                    profile_find(st->cfg.expected_profiles, st->cfg.expected_profile_count,
                                 r.profile_id);

                if (pin == NULL) {
                    rep->rejected_binding++;
                    set_detail(rep, "ds %u: profile %s not pinned", r.ds_id, r.profile_id);
                    continue;
                }
                if (strcmp(pin->digest, r.profile_digest) != 0) {
                    rep->rejected_binding++;
                    set_detail(rep, "ds %u: profile %s digest %s != pinned", r.ds_id,
                               r.profile_id, r.profile_digest);
                    continue;
                }
            }
```

   In the commit section replace `(void)snprintf(out->profile_digest, ...batch_profile);` with

```c
    qsort(batch_profiles, n_batch_profiles, sizeof(batch_profiles[0]), profile_pin_cmp);
    out->profile_count = n_batch_profiles;
    memcpy(out->profiles, batch_profiles, n_batch_profiles * sizeof(batch_profiles[0]));
```

6. `ds_connector_configure`: replace the `expected_profile_digest` snprintf with

```c
    dcfg.expected_profile_count = cfg->ds_connector_expected_profile_count;
    memcpy(dcfg.expected_profiles, cfg->ds_connector_expected_profiles,
           sizeof(dcfg.expected_profiles));
```

`src/fsal_obj/placement_gate.c:910`: replace the `profile_digest` memcpy with

```c
        out->profile_count = ctx.assess->profile_count;
        memcpy(out->profiles, ctx.assess->profiles, sizeof(out->profiles));
```

`src/cluster/cluster_transport.c:6694-6695`: replace the `placement_connector_profile_digest` RENDER_KEY with

```c
        {
            char profiles[PM_PROFILES_MAX * (PM_PROFILE_ID_MAX + PM_DIGEST_MAX + 1)];

            if (pm_format_profile_pins(rd.profiles, rd.profile_count,
                                       profiles, sizeof(profiles)) < 0) {
                (void)snprintf(profiles, sizeof(profiles), "-");
            }
            RENDER_KEY("placement_connector_profiles", "%s", profiles);
        }
```

Finally `git grep -n "profile_digest\b\|expected_profile_digest\|PM_KEY_CONN_PROFILE_DIGEST\b" -- src include tests` must only show `r.profile_digest`/`r->profile_digest` in `ds_connector.c`, `.digest` fields, and nothing else.

- [ ] **Step 4: Run the syntax check.** Expected: clean for all listed files (subject to the missing-system-header filter).

- [ ] **Step 5: Commit**

```bash
git add include/ds_connector.h include/placement_gate.h src/mds/ds_connector.c \
        src/fsal_obj/placement_gate.c src/cluster/cluster_transport.c tests/unit/test_ds_connector.c
git commit -m "feat(ds_connector): pin connector profiles by id; publish the batch's profile map

A batch may carry several profiles. Each record is checked against the
pin for its profile.id; one id with two digests, or more than 8 ids,
drops the batch. config show prints placement_connector_profiles.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: Fork docs, push, PR, CI

**Files:**
- Modify: `docs/config-keys.md:105`, `docs/examples/mds.conf.smart:11`, `docs/placement-modes.md:148,161,208`

- [ ] **Step 1: Update the docs.** `docs/config-keys.md` line 105 becomes:

```markdown
- `ds_connector_expected_profiles` — optional pins, `<profile-id>=<digest>[,...]` (at most 8, id `[A-Za-z0-9._-]{1,63}`); a record whose profile id is not pinned or whose digest differs from its pin is rejected. `ds_connector_expected_profile_digest` was removed and is a config error. `ds_connector_expected_config_digest` — optional pin; a batch with another config digest is dropped.
```

`docs/examples/mds.conf.smart` line 11 becomes `# ds_connector_expected_profiles = xinas-mvp=sha256:...`. In `docs/placement-modes.md` replace the single-digest sentences at 148 and 161 with the per-id rule (design §3.3–§3.4, one short paragraph) and rename `placement_connector_profile_digest` to `placement_connector_profiles` at 208.

- [ ] **Step 2: Grep for leftovers.** Run `git grep -n "expected_profile_digest\|placement_connector_profile_digest"`. Expected: only the removal note in `docs/config-keys.md` and the error string in `config.c`.

- [ ] **Step 3: Commit and push**

```bash
git add docs
git commit -m "docs(placement): per-profile digest pins

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin xinnor/profile-map
```

- [ ] **Step 4: Open the PR into `xinnor/placement-modes`** with `gh pr create --repo XinnorLab/pnfs-lattice --base xinnor/placement-modes --head xinnor/profile-map`, body: problem (review P1), rule, tests, "C tests not run locally (no CMake here); the fork CI is the gate", ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

- [ ] **Step 5: Wait for `Placement modes / build-test-prove` and fix until green.** Report the result; do not merge without the user.

---

## Part B — pNFS

### Task 5: Connector — profile id rule, 8-profile limit, schema pattern

**Files:**
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/contract.py`
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/config.py` (`_parse_profile` ~303, profile loop ~514-528)
- Modify: `connectors/lattice-ds-connector/contracts/connector-batch.schema.json:49`
- Test: `connectors/lattice-ds-connector/tests/test_config.py`, `tests/test_runtime.py` (schema)

**Interfaces:**
- Produces: `contract.PROFILE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,63}$"`, `contract.MAX_PROFILES = 8`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`:

```python
def test_profile_id_must_be_a_pin_key(token_file, tmp_path):
    for bad in ("bad id", "a=b", "x" * 64, "a,b"):
        doc = example(token_file, tmp_path)
        doc["profiles"][0]["id"] = bad
        doc["instances"][0]["profile"] = bad
        assert ("PROFILE_ID_INVALID", "profiles[0].id") in errors_of(doc)


def test_at_most_eight_profiles(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    for i in range(8):
        p = copy.deepcopy(doc["profiles"][0])
        p["id"] = "extra-%d" % i
        doc["profiles"].append(p)
    assert ("LIMIT", "profiles") in errors_of(doc)


def test_two_xinas_instances_on_two_profiles_validate(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    other = copy.deepcopy(doc["profiles"][0])
    other["id"] = "xinas-strict"
    other["degraded_multiplier_ppm"] = 0
    doc["profiles"].append(other)
    inst = copy.deepcopy(doc["instances"][0])
    inst["id"] = "xi-02"
    inst["profile"] = "xinas-strict"
    inst["bindings"] = [dict(inst["bindings"][0], ds_id=7)]
    doc["instances"].append(inst)
    config, issues = validate_config_dict(doc)
    assert config is not None, [(i.code, i.path, i.message) for i in issues]
    assert config.profiles["xinas-mvp"].digest != config.profiles["xinas-strict"].digest
```

Append to `tests/test_runtime.py`:

```python
def test_batch_schema_rejects_a_profile_id_that_cannot_be_pinned(schemas):
    jsonschema = pytest.importorskip("jsonschema")
    prof = (schemas["batch"]["properties"]["instances"]["items"]["properties"]["assessments"]
            ["items"]["properties"]["profile"])
    assert prof["properties"]["id"]["pattern"] == "^[A-Za-z0-9._-]{1,63}$"
    assert prof["properties"]["id"]["maxLength"] == 63
    assert not jsonschema.Draft7Validator(prof).is_valid({"id": "bad id", "version": "1", "digest": "d"})
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_config.py tests/test_runtime.py` from `connectors/lattice-ds-connector`. Expected: the three config tests and the schema test FAIL (no `PROFILE_ID_INVALID`, no `LIMIT`, no `pattern`). `test_two_xinas_instances_on_two_profiles_validate` may already pass — it is a guard, keep it.

- [ ] **Step 3: Implement.** `contract.py`:

```python
#: A profile id is used verbatim as a key of the MDS pin map
#: (ds_connector_expected_profiles = id=digest,...), so it cannot carry
#: '=', ',' or whitespace. Shared with the batch schema and the MDS parser.
PROFILE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,63}$"
#: PM_PROFILES_MAX in the MDS.
MAX_PROFILES = 8
```

`config.py` (`import re` if absent; `from . import contract` is already imported — check): in `_parse_profile` after `pid = _str(c, raw, "id", path)`:

```python
    if pid is not None and not re.match(contract.PROFILE_ID_PATTERN, pid):
        c.error("PROFILE_ID_INVALID", f"{path}.id", "must match [A-Za-z0-9._-]{1,63} (it is a key of the MDS pin map)")
```

and after `raw_profiles` is known to be a list:

```python
    if len(raw_profiles) > contract.MAX_PROFILES:
        c.error("LIMIT", "profiles", f"more than {contract.MAX_PROFILES} profiles (the MDS pins at most {contract.MAX_PROFILES})")
```

`connector-batch.schema.json`, `profile.properties.id`:

```json
"id": { "type": "string", "minLength": 1, "maxLength": 63, "pattern": "^[A-Za-z0-9._-]{1,63}$" },
```

- [ ] **Step 4: Run the full connector suite** `.venv/bin/python -m pytest -q`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add connectors/lattice-ds-connector
git commit -m "feat(connector): profile ids fit the MDS pin map; at most 8 profiles

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 6: Connector preflight — profile map, inconsistency, `--expect-profiles`

**Files:**
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/preflight.py` (`evaluate`, `render`, `run`)
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/cli.py` (`cmd_preflight` ~123, argparse ~199)
- Test: `connectors/lattice-ds-connector/tests/test_preflight.py`

**Interfaces:**
- Consumes: `contract.PROFILE_ID_PATTERN`, `contract.MAX_PROFILES`.
- Produces: `preflight.parse_profile_pins(text: str) -> Dict[str, str]` (raises `ValueError`), `preflight.format_profiles(d: Dict[str, str]) -> str` (`"-"` when empty), `evaluate(health, batch, expect_ds=(), expect_profiles: Optional[Dict[str, str]] = None)`; report key `profiles: Dict[str, str]` (sorted) replaces `profile_digest`; reasons `PROFILE_INCONSISTENT:<id>`, `PROFILE_NOT_PINNED:<id>`, `PROFILE_PIN_MISMATCH:<id>`; `run(socket, expect_ds, as_json, expect_profiles=None)`; CLI flag `--expect-profiles`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_preflight.py`: give `record()` a `pid="xinas-mvp"` parameter used as `"profile": {"id": pid, ...}`; change `test_ready_on_a_healthy_batch` to assert `r["profiles"] == {"xinas-mvp": "sha256:p"}` and `"profiles=xinas-mvp=sha256:p" in text`; in the parametrize list replace the `PROFILE_DIGESTS:2` row with

```python
        (lambda b: b["instances"][0]["assessments"].append(record(1, profile="sha256:q", domain="d2")), [0, 1], "PROFILE_INCONSISTENT:xinas-mvp"),
```

and append:

```python
def test_two_profiles_in_one_batch_are_ready():
    b = batch(record(0), record(1, pid="zfs-mvp", profile="sha256:z", domain="d2"))
    r = evaluate(HEALTH_OK, b, expect_ds=[0, 1])
    assert r["ready"], r["reasons"]
    assert r["profiles"] == {"xinas-mvp": "sha256:p", "zfs-mvp": "sha256:z"}


def test_expect_profiles():
    b = batch(record(0), record(1, pid="zfs-mvp", profile="sha256:z", domain="d2"))
    ok = evaluate(HEALTH_OK, b, expect_profiles={"xinas-mvp": "sha256:p", "zfs-mvp": "sha256:z"})
    assert ok["ready"], ok["reasons"]
    r = evaluate(HEALTH_OK, b, expect_profiles={"xinas-mvp": "sha256:OTHER"})
    assert "PROFILE_PIN_MISMATCH:xinas-mvp" in r["reasons"]
    assert "PROFILE_NOT_PINNED:zfs-mvp" in r["reasons"]


def test_parse_and_format_profile_pins():
    from lattice_ds_connector.preflight import format_profiles, parse_profile_pins
    assert parse_profile_pins(" zfs-mvp=sha256:z, xinas-mvp=sha256:p ") == {"xinas-mvp": "sha256:p", "zfs-mvp": "sha256:z"}
    assert format_profiles({"zfs-mvp": "z", "xinas-mvp": "p"}) == "xinas-mvp=p,zfs-mvp=z"
    assert format_profiles({}) == "-"
    for bad in ("", "x", "a b=d", "a=d,a=e", "a=", ",".join("p%d=d" % i for i in range(9))):
        with pytest.raises(ValueError):
            parse_profile_pins(bad)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_preflight.py`. Expected: FAIL (`KeyError: 'profiles'`, missing functions, `PROFILE_DIGESTS:2` instead of `PROFILE_INCONSISTENT`).

- [ ] **Step 3: Implement** in `preflight.py`:

```python
import re
...
def parse_profile_pins(text: str) -> Dict[str, str]:
    """``id=digest[,id=digest...]`` -> dict; ValueError on the MDS's own
    config errors (same rule as pm_parse_profile_pins)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty value")
    out: Dict[str, str] = {}
    for item in text.split(","):
        if "=" not in item:
            raise ValueError("item %r is not id=digest" % item.strip())
        pid, dig = (s.strip() for s in item.split("=", 1))
        if not re.match(contract.PROFILE_ID_PATTERN, pid):
            raise ValueError("profile id %r must match [A-Za-z0-9._-]{1,63}" % pid)
        if not dig:
            raise ValueError("profile %s: empty digest" % pid)
        if pid in out:
            raise ValueError("profile %s pinned twice" % pid)
        out[pid] = dig
    if len(out) > contract.MAX_PROFILES:
        raise ValueError("more than %d profiles" % contract.MAX_PROFILES)
    return dict(sorted(out.items()))


def format_profiles(profiles: Dict[str, str]) -> str:
    return ",".join("%s=%s" % kv for kv in sorted(profiles.items())) or "-"
```

In `evaluate` add the parameter `expect_profiles: Optional[Dict[str, str]] = None`; replace `profile_digests = set()` with `profiles: Dict[str, str] = {}` and `inconsistent: List[str] = []`; replace the digest collection with

```python
            pid, pdig = profile.get("id"), profile.get("digest")
            if isinstance(pid, str) and isinstance(pdig, str):
                if pid in profiles and profiles[pid] != pdig and pid not in inconsistent:
                    inconsistent.append(pid)
                profiles.setdefault(pid, pdig)
```

replace the `PROFILE_DIGESTS` block with

```python
    for pid in inconsistent:
        reasons.append("PROFILE_INCONSISTENT:%s" % pid)
    if expect_profiles is not None:
        for pid in sorted(profiles):
            if pid not in expect_profiles:
                reasons.append("PROFILE_NOT_PINNED:%s" % pid)
            elif expect_profiles[pid] != profiles[pid]:
                reasons.append("PROFILE_PIN_MISMATCH:%s" % pid)
```

and in `report` replace `"profile_digest": ...` with `"profiles": dict(sorted(profiles.items())),`. Update the module docstring ("one profile digest" → "one digest per profile id"). In `render` replace `profile_digest=%s` / `report.get("profile_digest")` with `profiles=%s` / `format_profiles(report.get("profiles") or {})`. Add `expect_profiles: Optional[Dict[str, str]] = None` to `run(...)` and pass it to `evaluate`.

`cli.py`: add `pre.add_argument("--expect-profiles", default="", help="id=digest,... the MDS will pin (ds_connector_expected_profiles)")`; in `cmd_preflight`:

```python
    from .preflight import parse_profile_pins
    pins = None
    if args.expect_profiles:
        try:
            pins = parse_profile_pins(args.expect_profiles)
        except ValueError as exc:
            print("error: --expect-profiles: %s" % exc)
            return 2
    return preflight_run(args.socket, expect, args.json, pins)
```

(Match `preflight_run`'s actual positional order when you open `run`.)

- [ ] **Step 4: Run the full connector suite.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add connectors/lattice-ds-connector
git commit -m "feat(connector): preflight reports a profile map; --expect-profiles

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 7: `lattice-placement` — live state, verify, show

**Files:**
- Create: `tools/lattice-placement/lattice_placement/profiles.py`
- Modify: `tools/lattice-placement/lattice_placement/live.py` (~74, ~87, ~192)
- Modify: `tools/lattice-placement/lattice_placement/verify.py` (~44, ~128)
- Modify: `tools/lattice-placement/tests/fixtures/config-show-smart-mds2.json:47`, `config-show-smart-mds1-none.json:47`
- Test: `tools/lattice-placement/tests/test_live.py`, `tests/test_verify.py`, new `tests/test_profiles.py`

**Interfaces:**
- Produces: `profiles.PROFILE_ID_PATTERN`, `profiles.MAX_PROFILES = 8`, `profiles.parse_pins(text) -> Dict[str, str]` (ValueError), `profiles.format_pins(d) -> str`; `MdsState.profiles: Optional[Dict[str, str]]` (replaces `profile_digest`); verify error `CONNECTOR_PROFILES_MISMATCH`.

- [ ] **Step 1: Write the failing tests.** `tests/test_profiles.py`:

```python
import pytest

from lattice_placement.profiles import MAX_PROFILES, format_pins, parse_pins


def test_parse_and_format():
    assert parse_pins(" zfs-mvp=sha256:z, xinas-mvp=sha256:p ") == {"xinas-mvp": "sha256:p", "zfs-mvp": "sha256:z"}
    assert format_pins({"zfs-mvp": "z", "xinas-mvp": "p"}) == "xinas-mvp=p,zfs-mvp=z"
    assert format_pins({}) == "-"
    assert MAX_PROFILES == 8


@pytest.mark.parametrize("bad", ["", "x", "a b=d", "a=d,a=e", "a=", "x" * 64 + "=d",
                                 ",".join("p%d=d" % i for i in range(9))])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_pins(bad)
```

In both fixtures replace the `placement_connector_profile_digest` line: `config-show-smart-mds2.json` → `"placement_connector_profiles": "xinas-mvp=sha256:c9bee5b23463719c5ebbd3343f257b02730311686d7799b7ae0690cc1544edfa",`; `config-show-smart-mds1-none.json` → `"placement_connector_profiles": "-",`.

`tests/test_live.py`: `s2.profile_digest.startswith("sha256:c9bee5b2")` → `s2.profiles["xinas-mvp"].startswith("sha256:c9bee5b2")`; `s1.profile_digest is None` → `s1.profiles is None`.

`tests/test_verify.py` (`test_differences_across_mds`): replace the last two lines with

```python
    b = smart2("m2")
    b.profiles = {"xinas-mvp": "sha256:other"}
    assert any(e.startswith("CONNECTOR_PROFILES_MISMATCH") for e in verdict([a, b]).errors)
    b = smart2("m2")
    b.profiles = dict(a.profiles, **{"zfs-mvp": "sha256:z"})
    assert any(e.startswith("CONNECTOR_PROFILES_MISMATCH") for e in verdict([a, b]).errors)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q` from `tools/lattice-placement`. Expected: FAIL (no `profiles` module, no attribute `profiles`).

- [ ] **Step 3: Implement.** `lattice_placement/profiles.py`:

```python
# SPDX-License-Identifier: MIT
"""The MDS profile pin map, ``ds_connector_expected_profiles`` and the
``placement_connector_profiles`` row: ``id=digest[,id=digest...]``
(per-profile digest design §3). Same rule as the MDS's
pm_parse_profile_pins and the connector preflight."""

from __future__ import annotations

import re
from typing import Dict

PROFILE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,63}$"
MAX_PROFILES = 8


def parse_pins(text: str) -> Dict[str, str]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty value")
    out: Dict[str, str] = {}
    for item in text.split(","):
        if "=" not in item:
            raise ValueError("item %r is not id=digest" % item.strip())
        pid, dig = (s.strip() for s in item.split("=", 1))
        if not re.match(PROFILE_ID_PATTERN, pid):
            raise ValueError("profile id %r must match [A-Za-z0-9._-]{1,63}" % pid)
        if not dig:
            raise ValueError("profile %s: empty digest" % pid)
        if pid in out:
            raise ValueError("profile %s pinned twice" % pid)
        out[pid] = dig
    if len(out) > MAX_PROFILES:
        raise ValueError("more than %d profiles" % MAX_PROFILES)
    return dict(sorted(out.items()))


def format_pins(pins: Dict[str, str]) -> str:
    return ",".join("%s=%s" % kv for kv in sorted(pins.items())) or "-"
```

`live.py`: field `profiles: Optional[Dict[str, str]] = None` replaces `profile_digest`; `as_dict` emits `"profiles": self.profiles`; in `state_from_show` change the digest loop to `for key in ("placement_connector_config_digest",):` and add

```python
    raw_profiles = show.get("placement_connector_profiles")
    if raw_profiles not in (None, "", "-"):
        try:
            st.profiles = parse_pins(raw_profiles)
        except ValueError:
            st.profiles = {"<unparsable>": raw_profiles}
```

(`from .profiles import parse_pins`). An unparsable row still takes part in the comparison, so two MDS disagreeing on it are reported.

`verify.py`: `spread("CONNECTOR_PROFILES_MISMATCH", {s.host: format_pins(s.profiles) if s.profiles else None for s in smart})` replaces the digest spread; in `render_show` `profile_digest=%s` → `profiles=%s` with `format_pins(s.profiles) if s.profiles else "-"` (`from .profiles import format_pins`).

- [ ] **Step 4: Run the CLI suite.** Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/lattice-placement
git commit -m "feat(lattice-placement): read and compare the MDS profile map

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 8: `lattice-placement validate`, manifests and the manifest check

**Files:**
- Modify: `tools/lattice-placement/lattice_placement/validate.py` (~75 near the top of `validate_document`, ~234, `run_preflight` ~257)
- Modify: `tools/lattice-placement/lattice_placement/cli.py:58`
- Modify: `docs/placement-modes/contract-manifest.json` and copy to `tools/lattice-placement/lattice_placement/data/contract-manifest.json`
- Modify: `scripts/check-manifests.py` (`MACRO_TO_KEY`)
- Test: `tools/lattice-placement/tests/test_validate.py`

**Interfaces:**
- Consumes: `profiles.parse_pins` (Task 7); connector `--expect-profiles` (Task 6).
- Produces: validate codes `LEGACY_KEY`, `RANGE: ds_connector_expected_profiles: ...`; `run_preflight(connector_cli, socket, expect_ds, timeout_s=10.0, expect_profiles: Optional[str] = None)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_validate.py`:

```python
def test_legacy_profile_digest_key_is_an_error_in_every_mode():
    for mode in ("smart", "fill"):
        r = rep("placement_mode = %s\nds_connector_expected_profile_digest = sha256:a\n" % mode, mode)
        assert "LEGACY_KEY" in codes(r)
        assert any("ds_connector_expected_profiles" in e for e in r.errors)


@pytest.mark.parametrize("value,ok", [
    ("xinas-mvp=sha256:a", True),
    ("xinas-mvp=sha256:a,zfs-mvp=sha256:z", True),
    ("bad id=sha256:a", False),
    ("a=x,a=y", False),
    ("", False),
])
def test_expected_profiles_syntax(value, ok):
    r = rep("placement_mode = smart\nds_connector_expected_profiles = %s\n" % value, "smart")
    bad = [e for e in r.errors if e.startswith("RANGE: ds_connector_expected_profiles")]
    assert (not bad) == ok, r.errors


def test_manifest_lists_the_new_key_and_the_removed_one():
    assert "ds_connector_expected_profiles" in M.keys
    assert "ds_connector_expected_profile_digest" not in M.keys
    assert M.raw["removed_keys"] == [{"key": "ds_connector_expected_profile_digest",
                                      "replaced_by": "ds_connector_expected_profiles"}]
    assert "placement_connector_profiles" in M.config_show_keys
    assert "placement_connector_profile_digest" not in M.config_show_keys
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_validate.py`. Expected: FAIL.

- [ ] **Step 3: Implement.**

`validate.py`, after `warn = rep.warnings` in `validate_document`:

```python
    for removed in manifest.raw.get("removed_keys", []):
        if removed["key"] in eff:
            err.append("LEGACY_KEY: %s was removed; use %s = <profile-id>=<digest>[,...]"
                       % (removed["key"], removed["replaced_by"]))
```

In the smart block replace the pin loop with

```python
        if "ds_connector_expected_config_digest" in eff and eff["ds_connector_expected_config_digest"] == "":
            err.append("RANGE: ds_connector_expected_config_digest is empty (remove the key to leave it unpinned)")
        if "ds_connector_expected_profiles" in eff:
            try:
                parse_pins(eff["ds_connector_expected_profiles"])
            except ValueError as exc:
                err.append("RANGE: ds_connector_expected_profiles: %s (remove the key to leave profiles unpinned)" % exc)
```

(`from .profiles import parse_pins`). `run_preflight` gains `expect_profiles: Optional[str] = None` and appends `["--expect-profiles", expect_profiles]` when set. In `cli.py:58` pass `expect_profiles=doc.effective().get("ds_connector_expected_profiles")` — read the surrounding code for the variable holding the parsed document and use it.

`docs/placement-modes/contract-manifest.json`:
- replace the `ds_connector_expected_profile_digest` key object with
  ```json
  {
    "key": "ds_connector_expected_profiles",
    "type": "profile_map",
    "default": null,
    "applies_to": ["smart"],
    "max_items": 8,
    "implemented": "B",
    "note": "<profile-id>=<digest>[,...]; id [A-Za-z0-9._-]{1,63}; a record whose profile id is not pinned or whose digest differs from its pin is rejected"
  }
  ```
  keeping the neighbouring fields' style (copy `implemented` and any other attribute the old entry had);
- add a top-level `"removed_keys": [{"key": "ds_connector_expected_profile_digest", "replaced_by": "ds_connector_expected_profiles"}]`;
- in `config_show_keys` replace `placement_connector_profile_digest` with `placement_connector_profiles`;
- replace `"profile_digest_rule": ...` with `"profile_rule": "per record: with pins set, profile.id must be pinned and its digest equal the pin; one id with two digests, or more than 8 ids, drops the batch; checked on every batch, not part of the per-DS pin (a connector profile reload is not a rebind)"`;
- add `"LEGACY_KEY"` to `validation_errors` if that list enumerates validate codes (open it and decide by its existing entries);
- bump `"version"` by one minor step.

Then `cp docs/placement-modes/contract-manifest.json tools/lattice-placement/lattice_placement/data/contract-manifest.json`.

`scripts/check-manifests.py`: add `"PM_PROFILES_MAX": ("ds_connector_expected_profiles", "max_items"),` to `MACRO_TO_KEY`.

- [ ] **Step 4: Run** both Python suites and `python3 scripts/check-manifests.py` (without `--fork`: checks (a) and (b) only). Expected: suites pass; the manifest check passes (a) and (b).

- [ ] **Step 5: Commit**

```bash
git add tools/lattice-placement docs/placement-modes/contract-manifest.json scripts/check-manifests.py
git commit -m "feat(lattice-placement): validate ds_connector_expected_profiles; manifest removes the single digest key

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 9: pNFS documents

**Files:**
- Modify: `docs/superpowers/specs/2026-09-23-placement-modes-design.md` (§4 key table, §7 layer 2 at ~333-340, §10 at ~507, §11 at ~512, §13 at ~542, §15)
- Modify: `docs/placement-modes/operations.md:121,130`, `docs/placement-modes/examples/mds.conf.smart:11`

- [ ] **Step 1: Edit the live spec.**
  - §4 key table: add a row `| ds_connector_expected_profiles | unset | <id>=<digest>[,...], at most 8, id [A-Za-z0-9._-]{1,63} | smart |`, and a sentence that `ds_connector_expected_profile_digest` is a config error (removed 2026-09-25).
  - §7 layer 2: replace "`profile.digest` equal to `ds_connector_expected_profile_digest` when set, and identical across every record of the batch" with the per-id rule of design §3.3 and the batch-drop rule of §3.4; remove `profile.digest` from the pinned tuple and add "(the profile is not part of the pin: a connector profile reload is not a rebind)".
  - §10: "the connector `config_digest` / `profile_digest` must be identical" → "the connector `config_digest` and the profile map (`placement_connector_profiles`) must be identical".
  - §11: "one profile digest" → "one digest per profile id (`PROFILE_INCONSISTENT`), and with `--expect-profiles` the pins (`PROFILE_NOT_PINNED`, `PROFILE_PIN_MISMATCH`)".
  - §13 connector-client row: "profile digest mismatch" → "profile pins by id, one id with two digests drops the batch, more than 8 ids drops the batch".
  - §15: add a decision row "Profiles are pinned per id (`ds_connector_expected_profiles`), not one digest per batch — see `2026-09-25-per-profile-digest-design.md`."
- [ ] **Step 2: Edit operations.md and the example.** `operations.md:121`: `--set ds_connector_expected_profiles=xinas-mvp=sha256:…`; line 130 sample output `profiles=xinas-mvp=sha256:c9bee5b2…`. `examples/mds.conf.smart:11`: `# ds_connector_expected_profiles = xinas-mvp=sha256:...`.
- [ ] **Step 3: Grep** `git grep -n "expected_profile_digest\|profile_digest\b" -- docs ':!docs/superpowers/plans' ':!docs/placement-modes/stand-*'`. Expected: only the removal notes. Stand reports are historical and stay as they are.
- [ ] **Step 4: Commit**

```bash
git add docs
git commit -m "docs(placement-modes): per-profile digest pins in the live spec and the runbook

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 10: Export the fork patches (after the fork PR merges)

**Files:**
- Modify: `mds/patches/6b4dcde/*`, `mds/manifest.json` (generated)

- [ ] **Step 1:** Update the local fork checkout: `git -C ~/Documents/GitHub/pnfs-lattice fetch && git -C ~/Documents/GitHub/pnfs-lattice checkout xinnor/placement-modes && git -C ~/Documents/GitHub/pnfs-lattice merge --ff-only origin/xinnor/placement-modes`. The main fork checkout must be clean first (`git status --short` empty); if it is not, stop and ask.
- [ ] **Step 2:** `scripts/export-patches.sh ~/Documents/GitHub/pnfs-lattice`.
- [ ] **Step 3:** `python3 scripts/check-manifests.py --fork ~/Documents/GitHub/pnfs-lattice`. Expected: `MANIFEST-CHECK: ok (... fork verified)`, including (d) `PM_PROFILES_MAX` = `max_items` 8.
- [ ] **Step 4: Commit**

```bash
git add mds
git commit -m "chore(mds): export patches for per-profile digest pins

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 11: pNFS PR

- [ ] **Step 1:** Run both Python suites and the manifest check once more.
- [ ] **Step 2:** `git push -u origin fix/profile-map` and `gh pr create --repo XinnorLab/pNFS --base main --head fix/profile-map`, body: problem (review P1), the decision table from the design, the fork PR link, test results, ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- [ ] **Step 3:** Report both PR links and CI state to the user. Do not merge.
