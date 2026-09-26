# Explicit DS path in the connector endpoint — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the MDS's open-ended "the endpoint path may be any ancestor of the registered DS path" rule with an explicit `endpoint.ds_path` that the connector publishes and the MDS matches exactly, and let the connector veto a DS path that lies under a nested share.

**Architecture:** The connector binding gains an optional `ds_path` (the path `ds[N]` registers), validated to be the share path or under it; the record's `endpoint` carries it. The MDS matches `ds_path` exactly against the registry and requires `export_path` to be it or a component-wise ancestor (never `/`); without `ds_path` it requires an exact `export_path`. The xiNAS policy vetoes `DS_PATH_UNDER_NESTED_SHARE` when another observed share or export lies between the share and the DS path.

**Tech Stack:** C11 unit tests in the pnfs-lattice fork (CMake; run by the fork CI only), Python 3 + pytest (pNFS connector).

**Spec:** `docs/superpowers/specs/2026-09-26-endpoint-ds-path-design.md` (pNFS). Read it before any task.

## Global Constraints

- `endpoint.ds_path` is optional in the batch schema (`{"type": "string", "pattern": "^/"}`); the connector emits it only when the binding sets it. Contract stays 1.x.
- Paths are compared after dropping trailing `/` (`/` stays `/`); "under" is component-wise (`/mnt/dat` is not an ancestor of `/mnt/data/x`).
- MDS: no `ds_path` → `export_path == registry path`; `ds_path` → `ds_path == registry path` and `export_path == ds_path` or a component-wise ancestor; `/` is never a parent (accepted only when `ds_path` is `/`).
- New connector config codes: `DS_PATH_OUTSIDE_EXPORT`, `ROOT_EXPORT_PARENT`, `DUPLICATE_DS_PATH`. New reason code: `DS_PATH_UNDER_NESTED_SHARE` (VALID deny), diagnostics key `nested_share_path`.
- Contract manifest version `1.2`; both manifest copies byte-identical.
- Stand value for the lab config: DS 0 `ds_path` = `/mnt/data/pnfs-ds`.
- Repositories: fork `~/Documents/GitHub/pnfs-lattice-endpoint` (branch `xinnor/endpoint-ds-path` from `origin/xinnor/placement-modes` = `0c5d6da`, PR into `xinnor/placement-modes`); pNFS `~/Documents/GitHub/pNFS/.claude/worktrees/endpoint-ds-path` (branch `fix/endpoint-ds-path`, currently on top of `fix/profile-map`).
- Fork C tests cannot run on this Mac; the local check is `/tmp/claude-501/fork-syntax.sh <fork-root>` (prints only `syntax-check done` when clean); the fork CI is the C gate.
- Python tests: `PYTHONDONTWRITEBYTECODE=1 ../../.venv/bin/python -m pytest -q` from `connectors/lattice-ds-connector` (pNFS tracks `__pycache__/*.pyc`; never commit them).
- Commit messages: Conventional Commits, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

## Part A — pnfs-lattice fork

### Task 1: `ds_path` in the MDS endpoint rule

**Files:**
- Modify: `include/ds_connector.h:108-114` (declaration and comment of `ds_connector_endpoint_matches`)
- Modify: `src/mds/ds_connector.c:378-412` (the matcher), `struct rec` (~457), `parse_record` endpoint block (~515-528), the caller (~1050)
- Test: `tests/unit/test_ds_connector.c` (`rec_pid` helper, `test_endpoint_rule` ~364, new batch test)

**Interfaces:**
- Produces: `bool ds_connector_endpoint_matches(const struct ds_connector_registry_ds *ds, const char *server, const char *export_path, const char *ds_path, uint32_t port);` — `ds_path` `NULL` or `""` means absent.

- [ ] **Step 1: Update the test helper and write the failing tests.**

In `tests/unit/test_ds_connector.c`, above `rec_pid`, add

```c
/* endpoint.ds_path every record carries; NULL = omit the key.  The
 * registry registers both DS at /mnt/data/pnfs-ds under the share
 * /mnt/data, as on the stand. */
static const char *REC_DS_PATH = "/mnt/data/pnfs-ds";
```

In `rec_pid`, build the optional fragment before the `snprintf` of the record:

```c
    char dsp[MDS_DS_EXPORT_MAX + 16];
    dsp[0] = '\0';
    if (REC_DS_PATH != NULL) {
        snprintf(dsp, sizeof(dsp), ",\"ds_path\":\"%s\"", REC_DS_PATH);
    }
```

and change the endpoint part of the format from `...\"port\":%u},` to `...\"port\":%u%s},`, passing `dsp` right after `port` in the argument list (re-read the whole format afterwards and match every conversion with its argument).

Replace the body of `test_endpoint_rule` with:

```c
static void test_endpoint_rule(void)
{
    struct ds_connector_registry_ds ds;
    memset(&ds, 0, sizeof(ds));
    snprintf(ds.host, sizeof(ds.host), "192.168.64.51");
    snprintf(ds.export_path, sizeof(ds.export_path), "/mnt/data/pnfs-ds");
    ds.tcp_port = 2049;
    /* no ds_path: the endpoint must name the registered path exactly */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data/pnfs-ds", NULL, 2049), true);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data/pnfs-ds/", NULL, 2049), true);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data", NULL, 2049), false);   /* parent, no ds_path */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data", "", 2049), false);     /* "" = absent */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/", NULL, 2049), false);
    /* ds_path pins the registered path; export_path is the share containing it */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data", "/mnt/data/pnfs-ds", 2049), true);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data/", "/mnt/data/pnfs-ds/", 2049), true);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data/pnfs-ds", "/mnt/data/pnfs-ds", 2049), true);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data", "/mnt/data/other", 2049), false);      /* not the registry path */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/dat", "/mnt/data/pnfs-ds", 2049), false);     /* prefix, not a component */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/srv/x", "/mnt/data/pnfs-ds", 2049), false);       /* not an ancestor */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data/pnfs-ds/sub", "/mnt/data/pnfs-ds", 2049), false);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/", "/mnt/data/pnfs-ds", 2049), false);           /* "/" is never a parent */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data", "relative", 2049), false);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "relative", NULL, 2049), false);
    /* host and port rules are unchanged */
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.52", "/mnt/data", "/mnt/data/pnfs-ds", 2049), false);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data", "/mnt/data/pnfs-ds", 2050), false);
    ds.tcp_port = 0;
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/mnt/data", "/mnt/data/pnfs-ds", 2050), true);  /* unknown registry port */
    /* a DS registered at the root: "/" is its own share */
    snprintf(ds.export_path, sizeof(ds.export_path), "/");
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/", NULL, 2049), true);
    ASSERT_EQ(ds_connector_endpoint_matches(&ds, "192.168.64.51", "/", "/", 2049), true);
}
```

Add a batch-level test and register it right after `RUN_TEST(test_endpoint_rule);`:

```c
static void test_endpoint_ds_path_in_a_batch(void)
{
    struct placement_assessment_view v; struct ds_connector_report rep;
    /* the share alone no longer matches a DS registered below it */
    REC_DS_PATH = NULL;
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", rec_ok(0)), &v, &rep), DC_OK);
    ASSERT_EQ(rep.accepted, 0u);
    ASSERT_EQ(rep.rejected_binding, 1u);
    /* ds_path naming another directory */
    REC_DS_PATH = "/mnt/data/other";
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", rec_ok(0)), &v, &rep), DC_OK);
    ASSERT_EQ(rep.rejected_binding, 1u);
    ASSERT_TRUE(strstr(rep.detail, "ds_path=/mnt/data/other") != NULL);
    /* a relative ds_path is a shape error */
    REC_DS_PATH = "relative";
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", rec_ok(0)), &v, &rep), DC_OK);
    ASSERT_EQ(rep.rejected_shape, 1u);
    /* the registered path: accepted */
    REC_DS_PATH = "/mnt/data/pnfs-ds";
    reg_init(); st_init(NULL, NULL);
    ASSERT_EQ(apply(batch("rt-1", "e1", 1, "2026-09-24T10:00:01Z", "c", "COMPLETE", rec_ok(0)), &v, &rep), DC_OK);
    ASSERT_EQ(rep.accepted, 1u);
}
```

`REC_DS_PATH` ends the test at its default value, so later tests see the stand layout. Every other existing test must still pass with the default: their records use `export_path "/mnt/data"`, now accepted through `ds_path`.

- [ ] **Step 2: Syntax check.** `/tmp/claude-501/fork-syntax.sh ~/Documents/GitHub/pnfs-lattice-endpoint`. Expected: errors in `test_ds_connector.c` (too many arguments to `ds_connector_endpoint_matches`).

- [ ] **Step 3: Implement.**

`include/ds_connector.h`:

```c
/* Endpoint rule (endpoint ds_path design §4): server == host,
 * port == tcp_port when both set; without ds_path the endpoint path equals
 * the registry path; with ds_path, ds_path equals the registry path and the
 * endpoint path is ds_path or a component-wise ancestor of it, "/" never a
 * parent.  Trailing '/' is ignored.  ds_path NULL or "" = absent. */
bool ds_connector_endpoint_matches(const struct ds_connector_registry_ds *ds,
                                   const char *server, const char *export_path,
                                   const char *ds_path, uint32_t port);
```

`src/mds/ds_connector.c`, replace the matcher:

```c
/* Length of p without trailing '/' ("/" keeps its one byte). */
static size_t path_len(const char *p)
{
    size_t n = strlen(p);

    while (n > 1 && p[n - 1] == '/') {
        n--;
    }
    return n;
}

static bool path_eq(const char *a, size_t al, const char *b, size_t bl)
{
    return al == bl && strncmp(a, b, al) == 0;
}

bool ds_connector_endpoint_matches(const struct ds_connector_registry_ds *ds,
                                   const char *server, const char *export_path,
                                   const char *ds_path, uint32_t port)
{
    size_t el;
    size_t rl;
    size_t dl;

    if (ds == NULL || server == NULL || export_path == NULL) {
        return false;
    }
    if (strncmp(ds->host, server, MDS_DS_HOST_MAX) != 0) {
        return false;
    }
    if (port != 0 && ds->tcp_port != 0 && port != ds->tcp_port) {
        return false;
    }
    if (export_path[0] != '/' || ds->export_path[0] != '/') {
        return false;
    }
    el = path_len(export_path);
    rl = path_len(ds->export_path);
    if (ds_path == NULL || ds_path[0] == '\0') {
        return path_eq(export_path, el, ds->export_path, rl);
    }
    if (ds_path[0] != '/') {
        return false;
    }
    dl = path_len(ds_path);
    if (!path_eq(ds_path, dl, ds->export_path, rl)) {
        return false;
    }
    if (path_eq(export_path, el, ds_path, dl)) {
        return true;
    }
    if (el == 1) {
        return false;   /* "/" is never the parent of a DS path */
    }
    return el < dl && strncmp(ds_path, export_path, el) == 0 && ds_path[el] == '/';
}
```

`struct rec`: add `char ds_path[MDS_DS_EXPORT_MAX];   /* "" when absent */` after `export_path`.

`parse_record`, after the `endpoint.transport` check:

```c
    r->ds_path[0] = '\0';
    v = tok_get(d, ep, "ds_path");
    if (v >= 0 && (!tok_copy(d, v, r->ds_path, sizeof(r->ds_path)) || r->ds_path[0] != '/')) {
        set_detail(rep, "ds %u: endpoint.ds_path invalid", r->ds_id);
        return false;
    }
```

Before relying on it, read `tok_get` and `tok_copy`: `tok_get` must return a negative value for a missing key, and `tok_copy` must fail for a non-string token and for a value that does not fit. If `tok_copy` accepts non-strings, add `!tok_is(d, v, JSMN_STRING) ||` in front.

The caller (~1050):

```c
            if (!ds_connector_endpoint_matches(rd, r.server, r.export_path, r.ds_path,
                                               (uint32_t)r.port)) {
                rep->rejected_binding++;
                set_detail(rep, "ds %u: endpoint %s:%s%s%s:%llu does not match the registry %s:%s:%u",
                           r.ds_id, r.server, r.export_path,
                           r.ds_path[0] != '\0' ? " ds_path=" : "", r.ds_path,
                           (unsigned long long)r.port,
                           rd->host, rd->export_path, (unsigned)rd->tcp_port);
                continue;
            }
```

`git grep -n ds_connector_endpoint_matches -- src include tests` must show only the updated call sites.

- [ ] **Step 4: Syntax check.** Expected: `syntax-check done` only. Trace `test_endpoint_rule` and `test_endpoint_ds_path_in_a_batch` against the code by hand and write the trace in the report (the C tests do not run here).

- [ ] **Step 5: Commit**

```bash
git add include/ds_connector.h src/mds/ds_connector.c tests/unit/test_ds_connector.c
git commit -m "feat(ds_connector): endpoint.ds_path pins the registered DS path; no open-ended parent match

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Fork docs, push, PR, CI

**Files:**
- Modify: `docs/placement-modes.md` (the endpoint-rule paragraph near line 147), `docs/config-keys.md` if it states the endpoint rule (grep `lies under`)

- [ ] **Step 1:** `git grep -n -i "lies under\|under the exported share\|endpoint rule\|trailing \`/\`" -- docs` and rewrite each statement of the endpoint rule to the rule in Global Constraints, adding one sentence: "A DS registered in a subdirectory of a share needs `ds_path` in the connector binding."
- [ ] **Step 2: Commit**

```bash
git add docs
git commit -m "docs(placement): the endpoint rule with ds_path

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 3 (controller):** push `xinnor/endpoint-ds-path`, open the PR into `xinnor/placement-modes`, wait for `Placement modes` CI green.

---

## Part B — pNFS

### Task 3: Connector — `ds_path` in the binding, the record and preflight

**Files:**
- Create: `connectors/lattice-ds-connector/lattice_ds_connector/paths.py`
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/modules/xinas_policy.py:308-316` (`path_contains` moves out)
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/config.py` (`Endpoint` ~112, `_parse_endpoint` ~348, the instance loop ~540)
- Modify: `connectors/lattice-ds-connector/contracts/connector-batch.schema.json:28-38`
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/preflight.py` (DS row and `render`)
- Test: `tests/test_config.py`, `tests/test_runtime.py`, `tests/test_preflight.py`

**Interfaces:**
- Produces: `paths.normalize(path: str) -> str`, `paths.path_contains(parent: str, path: str) -> bool` (unchanged semantics); `Endpoint.ds_path: Optional[str] = None`; `Endpoint.as_dict()` includes `"ds_path"` only when set; preflight row key `ds_path`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_config.py`:

```python
def _ep(doc, i=0):
    return doc["instances"][0]["bindings"][i]["endpoint"]


def test_ds_path_is_normalized_and_published(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    base = _ep(doc)["export_path"].rstrip("/")
    _ep(doc)["ds_path"] = base + "/pnfs-ds/"
    config, issues = validate_config_dict(doc)
    assert config is not None, [(i.code, i.path) for i in issues]
    ep = config.instances[0].bindings[0].endpoint
    assert ep.ds_path == base + "/pnfs-ds"
    assert ep.as_dict()["ds_path"] == base + "/pnfs-ds"
    plain, _ = validate_config_dict(example(token_file, tmp_path))
    assert "ds_path" not in plain.instances[0].bindings[0].endpoint.as_dict()


@pytest.mark.parametrize("ds_path, code", [
    ("/somewhere/else", "DS_PATH_OUTSIDE_EXPORT"),
    ("relative/path", "FORMAT"),
])
def test_ds_path_must_be_the_share_or_under_it(token_file, tmp_path, ds_path, code):
    doc = example(token_file, tmp_path)
    _ep(doc)["ds_path"] = ds_path
    assert code in {c for c, _ in errors_of(doc)}


def test_ds_path_prefix_is_not_a_component(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    base = _ep(doc)["export_path"].rstrip("/")
    _ep(doc)["ds_path"] = base + "x/pnfs-ds"          # e.g. /mnt/data/training-ax/pnfs-ds
    assert "DS_PATH_OUTSIDE_EXPORT" in {c for c, _ in errors_of(doc)}


def test_root_share_cannot_parent_a_ds_path(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    _ep(doc)["export_path"] = "/"
    _ep(doc)["ds_path"] = "/mnt/pnfs-ds"
    assert "ROOT_EXPORT_PARENT" in {c for c, _ in errors_of(doc)}


def test_two_bindings_on_one_ds_path(token_file, tmp_path):
    doc = example(token_file, tmp_path)
    first, second = _ep(doc, 0), _ep(doc, 1)
    second["server"] = first["server"]
    second["export_path"] = first["export_path"]
    assert "DUPLICATE_DS_PATH" in {c for c, _ in errors_of(doc)}
    # distinct ds_path under the same share is fine
    first["ds_path"] = first["export_path"].rstrip("/") + "/ds-a"
    second["ds_path"] = first["export_path"].rstrip("/") + "/ds-b"
    assert "DUPLICATE_DS_PATH" not in {c for c, _ in errors_of(doc)}
```

(Open `examples/connector-config.json` first: instance 0 must have two xinas bindings with distinct export paths; if the second binding points at a different share id, changing only the endpoint still triggers `DUPLICATE_DS_PATH` — other errors in the same doc, such as a target/path mismatch, are fine because the tests look for one code.)

Append to `tests/test_runtime.py`:

```python
def test_batch_schema_allows_an_absolute_ds_path(schemas):
    jsonschema = pytest.importorskip("jsonschema")
    ep = (schemas["batch"]["properties"]["instances"]["items"]["properties"]["assessments"]
          ["items"]["properties"]["endpoint"])
    assert ep["properties"]["ds_path"] == {"type": "string", "pattern": "^/"}
    assert "ds_path" not in ep["required"]
    base = {"server": "s", "export_path": "/mnt/data", "protocol": "NFS", "transport": "TCP", "port": 2049}
    assert jsonschema.Draft7Validator(ep).is_valid(dict(base, ds_path="/mnt/data/pnfs-ds"))
    assert not jsonschema.Draft7Validator(ep).is_valid(dict(base, ds_path="relative"))
```

In `tests/test_preflight.py` give `record()` an `endpoint=None` parameter (`"endpoint": endpoint` added to the dict only when not None) and append:

```python
def test_rows_show_ds_path():
    b = batch(record(0, endpoint={"server": "s", "export_path": "/mnt/data", "ds_path": "/mnt/data/pnfs-ds"}),
              record(1, domain="d2"))
    r = evaluate(HEALTH_OK, b)
    rows = {d["ds_id"]: d for d in r["ds"]}
    assert rows[0]["ds_path"] == "/mnt/data/pnfs-ds" and rows[1]["ds_path"] is None
    text = render(r)
    assert "ds_path=/mnt/data/pnfs-ds" in text and "ds_path=-" in text
```

- [ ] **Step 2: Run** the three test files. Expected: FAIL (no `ds_path` on `Endpoint`, no schema property, no row key).

- [ ] **Step 3: Implement.**

`lattice_ds_connector/paths.py`:

```python
# SPDX-License-Identifier: MIT
"""Path helpers shared by the config and the modules (string paths, no I/O)."""

from __future__ import annotations


def normalize(path: str) -> str:
    """Drop trailing '/' ('/' stays '/')."""
    return path.rstrip("/") or "/"


def path_contains(parent: str, path: str) -> bool:
    """True when ``path`` is ``parent`` or lies under it (component-wise)."""
    if not parent or not path:
        return False
    if path == parent:
        return True
    if parent == "/":
        return path.startswith("/")
    return path.startswith(parent.rstrip("/") + "/")
```

In `modules/xinas_policy.py` delete the `path_contains` definition and add `from ..paths import path_contains` next to the other imports (tests or modules that import `path_contains` from `xinas_policy` keep working; grep to confirm).

`config.py` (`from .paths import normalize, path_contains`):

```python
@dataclass(frozen=True)
class Endpoint:
    server: str
    export_path: str
    protocol: str = "NFS"
    transport: str = "TCP"
    port: int = 2049
    #: The path the MDS registers as ds[N] when the DS lives below the share
    #: (endpoint ds_path design §5); None = the share path itself.
    ds_path: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "server": self.server,
            "export_path": self.export_path,
            "protocol": self.protocol,
            "transport": self.transport,
            "port": self.port,
        }
        if self.ds_path is not None:
            out["ds_path"] = self.ds_path
        return out
```

In `_parse_endpoint`, after the port parse:

```python
    ds_path = _str(c, raw, "ds_path", path, required=False)
    if ds_path is not None and not ds_path.startswith("/"):
        c.error("FORMAT", f"{path}.ds_path", "must be an absolute path")
        ds_path = None
    if server is None or export_path is None or port is None:
        return None
    export_n = normalize(export_path)
    if ds_path is not None:
        ds_path = normalize(ds_path)
        if export_n == "/" and ds_path != "/":
            c.error("ROOT_EXPORT_PARENT", f"{path}.ds_path",
                    "'/' cannot be the share of a DS path; bind the share that contains it")
        elif not path_contains(export_n, ds_path):
            c.error("DS_PATH_OUTSIDE_EXPORT", f"{path}.ds_path",
                    f"{ds_path} is neither {export_n} nor under it")
    return Endpoint(server=server, export_path=export_n, protocol="NFS",
                    transport=transport, port=port, ds_path=ds_path)
```

(replacing the old `if server is None ...` / `return Endpoint(...)` lines).

In the instance loop, next to `seen_ds`:

```python
    seen_paths: Dict[Tuple[str, str], int] = {}
    ...
        for b in inst.bindings:
            ...
            key = (b.endpoint.server, b.endpoint.ds_path or b.endpoint.export_path)
            if key in seen_paths:
                c.error("DUPLICATE_DS_PATH", f"instances[{i}].bindings",
                        f"{key[0]}:{key[1]} is already bound as ds_id {seen_paths[key]}; "
                        "a DS below a share needs its own ds_path")
            seen_paths[key] = b.ds_id
```

Schema, `endpoint.properties`: add `"ds_path": { "type": "string", "pattern": "^/" }` (not in `required`).

Runtime: open `runtime.py` where `endpoints` is built (~458). If it uses `binding.endpoint.as_dict()`, nothing changes; otherwise switch it to `as_dict()` so `ds_path` reaches the record.

`preflight.py`: in the row dict add `"ds_path": (a.get("endpoint") or {}).get("ds_path"),`; in `render`'s DS line add ` ds_path=%s` with `r["ds_path"] or "-"` (keep the existing columns in place, append this one before `reasons=`).

Also update `docs/profile-xinas-mvp.md` or the connector README only if they document binding fields (grep `expected_client_networks` in `connectors/lattice-ds-connector/docs` and add `ds_path` next to it with one line).

- [ ] **Step 4: Run** the full connector suite. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add connectors/lattice-ds-connector
git commit -m "feat(connector): ds_path in the binding and the record endpoint

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: xiNAS policy — `DS_PATH_UNDER_NESTED_SHARE`

**Files:**
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/modules/xinas_policy.py` (`assess_binding`, after the `EXPORT_PATH_MISMATCH` check ~474)
- Modify: `connectors/lattice-ds-connector/lattice_ds_connector/contract.py` (`RUNTIME_REASONS`)
- Modify: `connectors/lattice-ds-connector/docs/profile-xinas-mvp.md` (reason table ~108)
- Test: `connectors/lattice-ds-connector/tests/test_policy.py`

**Interfaces:**
- Consumes: `Endpoint.ds_path`, `paths.path_contains`, `paths.normalize` (Task 3).
- Produces: `nested_share_path(export_path, ds_path, shares, resources) -> Optional[str]` in `xinas_policy`; reason `DS_PATH_UNDER_NESTED_SHARE`; diagnostics key `nested_share_path`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_policy.py` (`import dataclasses` at the top):

```python
def _ds_binding(ds_path):
    b = make_binding(0, "training-a", "/mnt/data/training-a", "training-a:7")
    return dataclasses.replace(b, endpoint=dataclasses.replace(b.endpoint, ds_path=ds_path))


def _with_share_at(result, path):
    r = copy.deepcopy(result)
    share = next(s for s in r["shares"] if s["share_id"] == "training-a")
    nested = copy.deepcopy(share)
    nested["share_id"] = "training-a-nested"
    nested["export_path"] = path
    r["shares"].append(nested)
    return r


def _with_export_at(result, path):
    r = copy.deepcopy(result)
    ex = next(x for x in r["resources"] if x["details"].get("kind") == "EXPORT")
    extra = copy.deepcopy(ex)
    extra["id"] = "export:nested"
    extra["details"]["export_path"] = path
    extra["details"]["present"] = True
    r["resources"].append(extra)
    return r


def test_ds_path_under_a_nested_share_is_a_valid_deny(base_result):
    a = one(_with_share_at(base_result, "/mnt/data/training-a/sub"),
            binding=_ds_binding("/mnt/data/training-a/sub/pnfs-ds"))
    assert a.quality == "VALID" and not a.allowed
    assert "DS_PATH_UNDER_NESTED_SHARE" in a.reason_codes
    assert a.diagnostics["nested_share_path"] == "/mnt/data/training-a/sub"


def test_ds_path_that_is_itself_a_nested_share_is_denied(base_result):
    a = one(_with_share_at(base_result, "/mnt/data/training-a/pnfs-ds"),
            binding=_ds_binding("/mnt/data/training-a/pnfs-ds"))
    assert "DS_PATH_UNDER_NESTED_SHARE" in a.reason_codes


def test_ds_path_under_a_nested_unmanaged_export_is_denied(base_result):
    a = one(_with_export_at(base_result, "/mnt/data/training-a/sub"),
            binding=_ds_binding("/mnt/data/training-a/sub/pnfs-ds"))
    assert "DS_PATH_UNDER_NESTED_SHARE" in a.reason_codes


def test_ds_path_next_to_a_nested_share_is_allowed(base_result):
    a = one(_with_share_at(base_result, "/mnt/data/training-a/other"),
            binding=_ds_binding("/mnt/data/training-a/sub/pnfs-ds"))
    assert a.allowed and "DS_PATH_UNDER_NESTED_SHARE" not in a.reason_codes


def test_ds_path_equal_to_the_share_needs_no_nested_check(base_result):
    a = one(base_result, binding=_ds_binding("/mnt/data/training-a"))
    assert a.allowed
```

(Check the field names before running: the assessment's diagnostics attribute in `modules/base.py` — use its real name; the source result's share list key and share id key in `tests/source_builder.py`; the EXPORT resource shape. Adjust the helpers to the real names, not the assertions.)

- [ ] **Step 2: Run** `tests/test_policy.py`. Expected: the three deny tests FAIL; the two allow tests may already pass (guards).

- [ ] **Step 3: Implement** in `xinas_policy.py` (import `normalize` from `..paths` too):

```python
def nested_share_path(export_path: str, ds_path: str,
                      shares: Dict[str, Dict[str, Any]],
                      resources: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The shallowest observed share or present export strictly below the
    bound share that contains ``ds_path`` (endpoint ds_path design §5)."""
    candidates = [s.get("export_path") for s in shares.values()]
    candidates += [_details(r).get("export_path") for r in resources.values()
                   if _kind(r) == "EXPORT" and _details(r).get("present") is not False]
    found = sorted(
        normalize(p) for p in candidates
        if isinstance(p, str) and p.startswith("/")
        and normalize(p) != export_path
        and path_contains(export_path, normalize(p))
        and path_contains(normalize(p), ds_path)
    )
    return found[0] if found else None
```

and in `assess_binding`, right after the `EXPORT_PATH_MISMATCH` block:

```python
    ds_path = binding.endpoint.ds_path
    if ds_path is not None and ds_path != binding.endpoint.export_path:
        nested = nested_share_path(binding.endpoint.export_path, ds_path, shares, resources)
        if nested is not None:
            v.add_veto("DS_PATH_UNDER_NESTED_SHARE")
            diag["nested_share_path"] = nested
```

`contract.py` `RUNTIME_REASONS`, after `EXPORT_PATH_MISMATCH`:

```python
    "DS_PATH_UNDER_NESTED_SHARE": "another observed share or export lies between the bound share and the binding's ds_path; VALID deny",
```

`docs/profile-xinas-mvp.md` reason table, after the `EXPORT_PATH_MISMATCH` row:

```markdown
| another share or export lies between the bound share and `ds_path` | VALID deny | `DS_PATH_UNDER_NESTED_SHARE` |
```

If a test enumerates `RUNTIME_REASONS` against the profile doc or the catalog, it now covers the new code; run the full suite.

- [ ] **Step 4: Run** the full connector suite. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add connectors/lattice-ds-connector
git commit -m "feat(connector): veto a DS path under a nested share

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5: Manifests, live spec, runbook, lab config

**Files:**
- Modify: `docs/placement-modes/contract-manifest.json` (`version`, `endpoint_rule` ~303) and copy to `tools/lattice-placement/lattice_placement/data/contract-manifest.json`
- Modify: `docs/superpowers/specs/2026-09-23-placement-modes-design.md` (§7 layer 2, the endpoint sentence ~334-337)
- Modify: `docs/placement-modes/operations.md`
- Modify: `connectors/lattice-ds-connector/examples/connector-config.lab-xinas-box.json`

- [ ] **Step 1: Manifest.** `"version": "1.2"`; `endpoint_rule`:

```json
"endpoint_rule": "endpoint.server == registry.host; endpoint.port == registry.tcp_port when both set; trailing '/' is ignored on every path; without endpoint.ds_path: endpoint.export_path == registry.export_path; with endpoint.ds_path: ds_path == registry.export_path and export_path == ds_path or a component-wise ancestor of it; '/' is never a parent"
```

`cp docs/placement-modes/contract-manifest.json tools/lattice-placement/lattice_placement/data/contract-manifest.json`. If a test pins the manifest version, update it.

- [ ] **Step 2: Live spec §7 layer 2.** Replace "`endpoint.server` and `endpoint.export_path` equal the registry's `host` and `export_path` for that `ds_id` (exact strings — the connector's binding must name the DS the way `ds[N]` registered it)" with: "`endpoint.server` equals the registry's `host`; without `endpoint.ds_path`, `endpoint.export_path` equals the registry's `export_path`; with it, `ds_path` equals the registry's `export_path` and `export_path` is `ds_path` or a component-wise ancestor of it, never `/` (trailing `/` ignored; `2026-09-26-endpoint-ds-path-design.md`)". Keep the port clause that follows.

- [ ] **Step 3: operations.md.** Add a subsection "A DS in a subdirectory of a share": bind the share in `export_path` and the `ds[N]` path in `ds_path`, with the stand example (`export_path: /mnt/data`, `ds_path: /mnt/data/pnfs-ds`); the connector vetoes `DS_PATH_UNDER_NESTED_SHARE` when another share lies in between. Add to the upgrade notes: "Add `ds_path` to the connector bindings of DS registered below a share before upgrading the MDS: an MDS without the change ignores `ds_path` and still accepts the record; an MDS with it rejects a parent without `ds_path` (`rejected_binding`, detail `does not match the registry`)."

- [ ] **Step 4: Lab config.** In `examples/connector-config.lab-xinas-box.json`, DS 0's `endpoint` gets `"ds_path": "/mnt/data/pnfs-ds"`. Run the connector's config validation on it the way the existing tests do for examples (grep `lab-xinas-box` in tests; if a test loads it, run it).

- [ ] **Step 5: Run** both Python suites and `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check-manifests.py` (no `--fork`). Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add docs tools/lattice-placement/lattice_placement/data/contract-manifest.json connectors/lattice-ds-connector/examples
git commit -m "docs(placement-modes): the endpoint rule with ds_path; manifest 1.2; stand config

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 6: Export patches and PR (after the fork PR merges)

- [ ] **Step 1:** If XinnorLab/pNFS#1 has merged, rebase `fix/endpoint-ds-path` onto `origin/main` (drop nothing; the profile-map commits disappear as already merged).
- [ ] **Step 2:** Fast-forward the main fork checkout to the merged `origin/xinnor/placement-modes` (it must be clean), `scripts/export-patches.sh ~/Documents/GitHub/pnfs-lattice`, `check-manifests.py --fork ~/Documents/GitHub/pnfs-lattice` → ok.
- [ ] **Step 3: Commit** `chore(mds): export patches for the endpoint ds_path rule`.
- [ ] **Step 4:** Both Python suites once more; push; `gh pr create --repo XinnorLab/pNFS --base main`; wait for CI; report.
