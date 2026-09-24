# Placement Modes — Stage C Implementation Plan (CLI, second connector, acceptance)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the operator surface of the placement modes — the `lattice-placement` helper (`mode show | validate | set | verify`), the documented switch procedure, the second per-MDS connector on the lab, the LAT-25 performance row and the cross-repo manifest check — so `smart` can be switched on and verified on a two-MDS cluster without hand-editing INI files or reading journals.

**Architecture:** The MDS already exposes everything the helper needs over `mds-admin config show --json` (desired/effective mode, generation, kernel id, readiness, per-DS rows) and `/metrics`; Stage C adds one build-flags row and one placement-latency histogram to the fork, then builds the helper in this repo as a Python 3.9 stdlib package next to the connector (`tools/lattice-placement/`). The helper never restarts a daemon and never infers "active" from a file: `show`/`verify` ask live MDS, `validate` is read-only, `set` edits one local file inside a managed block with backup + audit + re-validation. The stand work installs a connector on node223 (MDS 1) so both MDS can run `smart`, and measures create throughput / placement p99 legacy vs smart.

**Tech Stack:** C11 (fork, CMake, upstream `ASSERT_*`/`RUN_TEST` tests); Python 3.9 stdlib + pytest (helper); bash (stand scripts on xinas-box); GitHub Actions (fork + this repo).

**Spec:** `docs/superpowers/specs/2026-09-23-placement-modes-design.md` §9 (observability), §10 (CLI), §12 (packaging/CI), §13 (acceptance); requirements CLI-01…05 and §7 of `lattice_placement_modes_mvp_requirements.md`.

## Global Constraints

- Every repository artifact in English; Conventional Commits; commits end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; author `XinnorLab <135218967+XinnorLab@users.noreply.github.com>`; push via the `github-xinnorlab` SSH alias.
- Spec-first: a behaviour not in the design spec is added to the spec before the code (Task C0 amends §9/§10).
- Fork commits stay reviewable one-per-task; `scripts/export-patches.sh` after every fork commit; `mds/manifest.json` and `docs/placement-modes/contract-manifest.json` are the single source of names/defaults/ranges.
- Helper: Python 3.9, stdlib only, same style as the connector (`argparse`, dataclasses, pure functions with tests, JSON output flag). No SSH library: remote reads shell out to `ssh` only when `--ssh` is given.
- The helper never restarts `pnfs-mds`, never edits the connector config or xiNAS, never claims a cluster-wide switch (CLI-04/05).
- Stand: the lab MDS restart only inside an announced window with Sergey's go-ahead (Tasks C6/C7 need it); token values in `/root/lattice-viewer.token` and `/etc/lattice-ds-connector/secrets/xi-01.token` are copied between hosts with `scp`, never printed.
- Build/test loop for the fork: `mds/scripts/pm-run.sh <ctest-regex|all|build>` (syncs the local working tree; `--no-sync` tests the pushed base commit only).

---

## File structure

| File | Responsibility |
|---|---|
| fork `src/cluster/cluster_transport.c`, `include/placement_gate.h`, `src/fsal_obj/placement_gate.c`, `src/common/mds_metrics.c`, `include/mds_metrics.h`, `tests/unit/test_cluster_transport.c`, `tests/unit/test_placement_gate.c`, `docs/placement-modes.md` | `placement_build` config-show row; `pnfs_mds_placement_admit_seconds` histogram around every gated selection and create admission |
| `tools/lattice-placement/pyproject.toml`, `README.md` | package, console script `lattice-placement` |
| `tools/lattice-placement/lattice_placement/manifest.py` | loads the bundled copy of the contract manifest (keys, ranges, defaults, profiles) |
| `tools/lattice-placement/lattice_placement/ini.py` | line-preserving INI document with the `config.c` grammar; managed block |
| `tools/lattice-placement/lattice_placement/validate.py` | pure config validation mirroring `placement_config_validate`; connector preflight folding |
| `tools/lattice-placement/lattice_placement/live.py` | `mds-admin config show --json` + `/metrics` readers, parsed into `MdsState` |
| `tools/lattice-placement/lattice_placement/verify.py` | pure `show` table + `verify` verdict over `MdsState` rows |
| `tools/lattice-placement/lattice_placement/setmode.py` | dry-run diff, `--apply` (backup, atomic write, audit, re-validate, rollback) |
| `tools/lattice-placement/lattice_placement/cli.py` | argparse wiring, exit codes, `--json` |
| `tools/lattice-placement/tests/` | pytest: `test_ini.py`, `test_validate.py`, `test_setmode.py`, `test_live.py`, `test_verify.py`, `test_cli.py`, fixtures with the stand's real `config show` texts |
| `.github/workflows/ci.yml` (this repo) | connector tests, helper tests, manifest check |
| `scripts/check-manifests.py` | patch digests, fork reproducibility, defaults vs `placement_modes.h`, manifest bundle identity |
| `docs/placement-modes/operations.md` | the switch procedure (CLI-04), rollback, what each exit code means |
| `mds/scripts/pm-connector-install.sh`, `pm-bench.sh`, `pm-deploy.sh` (updated) | stand: connector install on a node, create benchmark, deploy via the helper |
| `docs/placement-modes/stand-2026-09-25.md` | Stage C stand + perf report |

---

### Task C0: Spec amendment (§9, §10) — what Stage C makes precise

**Files:**
- Modify: `docs/superpowers/specs/2026-09-23-placement-modes-design.md` §9 (observability), §10 (CLI)
- Modify: `docs/placement-modes/contract-manifest.json` (`config_show_keys` += `placement_build`, `metrics` += `pnfs_mds_placement_admit_seconds`, new `profiles_with_placement_policy`, new `cli` block)

- [ ] **Step 1: §9 additions.** `config show` row `placement_build = wrr=<0|1> connector=<0|1> prealloc=<0|1>` (build facts, every mode); metric `pnfs_mds_placement_admit_seconds` (the upstream 12-bucket histogram, 100 µs … +Inf, `_bucket/_sum/_count`) observed once per `placement_select_gated` call in every mode (legacy included, so the perf row compares like with like) and once per `placement_gate_admit_create`.
- [ ] **Step 2: §10 precisions.** (a) `show`/`verify` read the live MDS with `mds-admin config show --json` (`--mds-admin`, `--mds-port` default 50051, `--env KEY=VALUE` passthrough for `LD_LIBRARY_PATH`) and, secondarily, `/metrics` (`--metrics-port` 9090, `--no-metrics`); the desired mode comes from `--config` (local file) or `--ssh` (`ssh <user>@<host> cat <path>`), otherwise printed as `?` — never inferred. (b) `validate` profile rule: any `workload_profile` other than `default` conflicts with `placement_mode` (every shipped profile sets a placement policy; the manifest lists them). (c) `set <mode>` writes the managed block `# lattice-placement managed block … # end lattice-placement managed block`; `set legacy` writes `placement_policy_enabled = true`, `placement_policy = <--legacy-policy, default wrr>` and `ds_weight.<id> = <w>` from `--ds-weight id=w`; other managed keys come from `--set key=value` (manifest keys only); the audit line is JSON (`ts, user, sudo_user, host, file, old_mode, new_mode, old_sha256, new_sha256, backup`); if the rewritten file fails validation the backup is restored and the exit code is 1. (d) `verify` compares `placement_config_generation`, effective mode, `placement_build`, and in `smart` the connector config/profile digests across MDS; exit 1 on any difference or on `connector_config_valid=0`, `connector_reachable=0`, `coverage=none`; `coverage=partial` is a warning (exit 0) unless `--require-full-coverage`.
- [ ] **Step 3: manifest.** Add `"profiles_with_placement_policy": ["hpc", "ai_training", "genomics", "media"]`, `"cli": {"managed_block_begin": "# lattice-placement managed block", "managed_block_end": "# end lattice-placement managed block", "legacy_keys": ["placement_policy", "placement_policy_enabled", "placement_capacity_weighting", "ds_weight.<id>"], "audit_log": "/var/lib/lattice-placement/audit.log", "verify_exit": {"0": "same mode/generation/build on every MDS; smart: connector valid+reachable, coverage full or partial", "1": "any difference, connector invalid/unreachable, coverage none, or partial with --require-full-coverage", "2": "an MDS could not be read"}}`.
- [ ] **Step 4: Commit** `docs(placement-modes): Stage C spec — build row, admit histogram, CLI details`.

### Task C1: Fork — `placement_build` row and `pnfs_mds_placement_admit_seconds`

**Files:**
- Modify: `src/cluster/cluster_transport.c` (`render_cfg_placement`), `include/mds_metrics.h`, `src/common/mds_metrics.c`, `src/fsal_obj/placement_gate.c` (`placement_select_gated`, `placement_gate_admit_create`), `docs/placement-modes.md`
- Test: `tests/unit/test_cluster_transport.c`, `tests/unit/test_placement_gate.c`

**Interfaces:**
- `struct branch_metrics` gains `struct mds_histogram placement_admit_hist;` (zero-initialised static storage is a valid empty histogram — check `mds_histogram.h`; if it needs `mds_histogram_reset`, call it from `placement_gate_init`).
- Rendered after the placement counters: `mds_histogram_render(&g_branch_metrics.placement_admit_hist, "pnfs_mds_placement_admit_seconds", …)` with a `# HELP` line "Time spent in the placement gate per selection or create admission, every mode."
- Row: `RENDER_KEY("placement_build", "wrr=%d connector=%d prealloc=%d", mds_wrr_kernel_id() != 0, PM_BUILD_CONNECTOR, PM_BUILD_PREALLOC)` where `PM_BUILD_CONNECTOR` is `1` under `#ifdef ENABLE_DS_CONNECTOR` else `0`, `PM_BUILD_PREALLOC` likewise under `ENABLE_DS_PREALLOC` (define both in `placement_modes.h`).

- [ ] **Step 1: Failing tests.** `test_cluster_transport.c::test_config_show_placement_rows`: assert a line `placement_build = wrr=1 connector=1 prealloc=0` (the test build has WRR on, connector on, prealloc off — take the expected string from the compile-time macros so the CI job with `ENABLE_DS_PREALLOC=ON` also passes). `test_placement_gate.c`: new `test_admit_histogram_counts_every_selection` — init fill singleton with one DS, read `atomic_load(&g_branch_metrics.placement_admit_hist.count)`, call `placement_select_gated` twice and `placement_gate_admit_create` once, assert count grew by 3; a legacy (uninitialised gate) `placement_select_gated` call also increments.
- [ ] **Step 2: Run** `pm-run.sh "test_cluster_transport|test_placement_gate"` → FAIL.
- [ ] **Step 3: Implement** (macros, histogram field, `uint64_t t0 = ds_cache_mono_ns()` — add a monotonic-ns helper in `placement_gate.c` with `clock_gettime(CLOCK_MONOTONIC)`; observe in a single exit path of each function), render, docs row + metric.
- [ ] **Step 4: Run** the two tests, then `pm-run.sh all` → 69/69 (+1).
- [ ] **Step 5: Commit** `feat(placement): placement_build row and the admit-latency histogram` ; push; watch CI; `scripts/export-patches.sh`; commit the manifests here (`docs(placement-modes): patches at <sha>`).

### Task C2: Helper package, manifest loader, INI document, `mode validate`

**Files:**
- Create: `tools/lattice-placement/pyproject.toml`, `tools/lattice-placement/README.md`, `tools/lattice-placement/lattice_placement/{__init__.py,__main__.py,manifest.py,ini.py,validate.py,cli.py}`, `tools/lattice-placement/lattice_placement/data/contract-manifest.json` (copy; `check-manifests.py` asserts identity with `docs/placement-modes/contract-manifest.json`)
- Test: `tools/lattice-placement/tests/{conftest.py,test_ini.py,test_validate.py,test_cli_validate.py}`

**Interfaces:**

```python
# ini.py
@dataclass
class Line:
    kind: str            # "pair" | "comment" | "blank" | "section" | "junk"
    raw: str             # original text without the newline
    key: Optional[str] = None
    value: Optional[str] = None

class IniDocument:
    lines: List[Line]
    @classmethod
    def parse(cls, text: str) -> "IniDocument": ...   # config.c grammar: strip; '#', ';' comments; '[' sections skipped; first '=' splits; no '=' = junk (kept)
    def get(self, key: str) -> Optional[str]: ...      # last wins (config.c)
    def items(self) -> List[Tuple[str, str]]: ...      # every pair, in order (duplicates kept)
    def prefixed(self, prefix: str) -> Dict[str, str]: ...  # e.g. "ds_capacity_domain." -> {"0": "xi/fs-1"}
    def remove_keys(self, keys: Iterable[str], prefixes: Iterable[str]) -> int: ...
    def replace_managed_block(self, begin: str, end: str, pairs: List[Tuple[str, str]]) -> None: ...  # removes an existing block, appends a new one (or nothing when pairs is empty)
    def render(self) -> str: ...                        # byte-identical for untouched lines

# manifest.py
@dataclass(frozen=True)
class KeySpec: key: str; type: Optional[str]; default: Any; range: Optional[Tuple[int,int]]; values: Optional[List[str]]; applies_to: List[str]; max_len: Optional[int]
class Manifest:
    keys: Dict[str, KeySpec]          # exact keys; prefixed ones under "ds_capacity_domain.<ds_id>", "placement_domain_weight.<domain>"
    modes: List[str]; legacy_policy_values: List[str]; profiles_with_placement_policy: List[str]; cli: Dict[str, Any]; weight: Dict[str, Any]
def load() -> Manifest: ...           # from the package data file

# validate.py
@dataclass
class Report:
    ready: bool
    mode: str                          # requested mode
    errors: List[str]                  # "PLACEMENT_MODE_CONFLICT: …" strings, same texts as placement_config.c where they exist
    warnings: List[str]
    effective: Dict[str, str]          # the keys the MDS will see (defaults filled)
    connector: Optional[Dict[str, Any]] = None   # preflight report when smart
def validate_document(doc: IniDocument, mode: str, manifest: Manifest) -> Report: ...   # pure
def fold_preflight(report: Report, preflight: Optional[Dict[str, Any]]) -> Report: ... # pure: NOT_READY when preflight is missing or not ready; reasons prefixed "CONNECTOR:"
def run_preflight(connector_cli: str, socket: str, expect_ds: List[int]) -> Optional[Dict[str, Any]]: ...  # subprocess `lattice-ds-connector preflight --json --socket S [--expect-ds …]`, None on failure to run
```

Validation rules (mirror `placement_config_validate`, in this order): unknown `placement_mode` value → `RANGE`; legacy keys present with a mode → `PLACEMENT_MODE_CONFLICT`; `workload_profile` ∉ {absent, default} → `PLACEMENT_MODE_CONFLICT`; `ds_weight.*` with fill → conflict; `ds_connector_enabled` contradictions (explicit false with smart, explicit true with rr/fill/legacy/absent mode) → conflict; `placement_domain_weight.*` without `placement_allow_manual_base_weights = true` or in rr/fill → `DOMAIN_WEIGHT_FORBIDDEN`; numbers strict (`^[0-9]+$`), ranges from the manifest → `RANGE`; `placement_capacity_max_age_ms <= ds_capacity_poll_ms` (default 60000 when absent) → `RANGE`; fill/smart with `ds_capacity_poll_ms = 0` → `RANGE`; `ds_capacity_domain.<id>`: id numeric < 256, value non-empty ≤ 127 → `RANGE`; `placement_stripe_shrink` ∉ {allow,strict} → `RANGE`; smart: socket absolute and `< 108` bytes, deadline ≤ poll, `default_mirror_count > 1` → `MIRROR_COUNT_UNSUPPORTED`, `ds_connector_max_ds` 1..256, `ds_connector_access_scope` non-empty; `default_stripe_count` with mirrors — leave to the MDS. Warnings: `placement_min_free_bytes` ≥ 1 PiB ("check the unit"); duplicate keys (last wins); `smart` without `ds_connector_expected_config_digest` ("verify prints the digest every MDS sees; pin it after the first switch").

CLI (`cli.py`, this task): `lattice-placement mode validate <rr|fill|smart|legacy> --config PATH [--connector-socket P] [--connector-cli lattice-ds-connector] [--expect-ds 0,1] [--json]` → prints `READY` / `NOT_READY <errors…>` + effective keys; exit 0/1; 2 when the file is unreadable. `legacy` validates only that no `placement_mode` key is present and the legacy keys are well-formed.

- [ ] **Step 1: Failing tests.** `test_ini.py`: round-trip of a file with comments, `[section]`, `key=value`, `key = value`, junk line, duplicate keys (render identical; `get` returns the last); `remove_keys` deletes exactly the legacy lines and keeps comments; `replace_managed_block` appends once and replaces on the second call; a value containing `=` keeps everything after the first `=`. `test_validate.py`: every `bad[]` case of the fork's `test_placement_config.c` (copy the INI texts) → not ready with the expected error prefix; the good cases ready with the expected `effective`; profile rule (`workload_profile = hpc` + mode → conflict; `= default` fine); `fold_preflight` with `{"ready": false, "reasons": ["CONNECTOR_NOT_READY"]}` → not ready, reason `CONNECTOR:CONNECTOR_NOT_READY`; with `None` → `CONNECTOR:UNAVAILABLE`. `test_cli_validate.py`: temp INI files, exit codes 0/1/2, `--json` shape, smart with a fake `--connector-cli` script that prints a canned JSON.
- [ ] **Step 2: Run** `cd tools/lattice-placement && python -m pytest -q` → import errors.
- [ ] **Step 3: Implement** the four modules + `cli.py` with only `mode validate` wired.
- [ ] **Step 4: Run** pytest → PASS; `python -m lattice_placement mode validate smart --config ../../mds/../pnfs-lattice-examples` is not available here, use `docs/placement-modes/examples/mds.conf.smart` copied from the fork (`docs/examples/`) → `READY` without a connector socket? No: smart without a reachable connector must print `NOT_READY CONNECTOR:UNAVAILABLE` — document that `--connector-socket` is required for smart and the run happens on the MDS host.
- [ ] **Step 5: Commit** `feat(lattice-placement): package, INI document, mode validate`.

### Task C3: `mode set` — dry-run diff, `--apply` with backup, audit and re-validation

**Files:**
- Create: `tools/lattice-placement/lattice_placement/setmode.py`
- Modify: `cli.py`
- Test: `tools/lattice-placement/tests/test_setmode.py`, `test_cli_set.py`

**Interfaces:**

```python
@dataclass
class Plan:
    mode: str; old_mode: str; before: str; after: str; diff: str; removed_keys: List[str]; managed: List[Tuple[str, str]]; warnings: List[str]
def plan_set(text: str, mode: str, extra: Dict[str, str], legacy_policy: str, ds_weights: Dict[int, int], manifest: Manifest) -> Plan: ...   # pure; extra keys must be manifest keys applying to the mode (else ValueError with the key)
@dataclass
class ApplyResult: backup: str; audit_line: Dict[str, Any]; sha_before: str; sha_after: str
def apply_plan(path: str, plan: Plan, audit_log: str, now: datetime, user: str, sudo_user: Optional[str], host: str) -> ApplyResult: ...
    # 1 read current bytes, refuse if sha differs from plan.before (file changed since the plan)
    # 2 backup  <path>.<YYYYmmdd-HHMMSS>.bak  (copy2)
    # 3 write   <path>.lattice-placement.tmp in the same dir, fsync, os.replace
    # 4 validate_document(IniDocument.parse(after), mode) — on errors: copy the backup back, os.replace, raise ApplyRefused(errors)
    # 5 append one JSON line to audit_log (mkdir -p 0750, open O_APPEND), fsync
```

`old_mode` is read from the file: `placement_mode` value if present else `legacy`. `mode set legacy` removes the managed block and every `placement_*`/`ds_capacity_domain.*`/`ds_connector_*` key, then writes the legacy keys given. Messages printed after `--apply`: `applied: <path> (backup <bak>) — running pnfs-mds still uses <old_mode>; restart it inside the maintenance window (docs/placement-modes/operations.md)`; leaving smart: `WARNING: leaving smart disables the health veto — a denied or UNKNOWN DS will receive new objects again`; entering smart: `NOTE: no DS is admitted until the first fresh VALID assessment; run mode verify after the restart`.

- [ ] **Step 1: Failing tests.** `plan_set` on the lab legacy file (the real text from the stand: `placement_policy_enabled = true`, `placement_policy = wrr`, `ds_weight.0 = 55`, `ds_weight.1 = 45`, three `ds_capacity_poll_ms = 10000` lines) with `smart` → diff removes the four legacy lines, keeps the poll lines and comments, appends the block with `placement_mode = smart` + extras; `set legacy --ds-weight 0=55 --ds-weight 1=45` on a smart file → block gone, legacy keys back; unknown `--set foo=1` → ValueError; `--set ds_weight.0=1` with fill → refused (conflict from validate). `apply_plan`: backup exists and equals the original, file rewritten atomically (no `.tmp` left), audit JSON line has every field, sha values match; a second `apply_plan` with a stale plan raises; an `after` made invalid (monkeypatch validate) restores the backup and raises `ApplyRefused`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; wire `mode set <mode> --config PATH [--apply] [--set k=v]… [--legacy-policy wrr] [--ds-weight id=w]… [--audit-log PATH] [--json]`; exit 0 on dry-run and successful apply, 1 on refusal, 2 on I/O errors.
- [ ] **Step 4: Run** pytest → PASS.
- [ ] **Step 5: Commit** `feat(lattice-placement): mode set — dry-run diff, backup, atomic apply, audit, re-validation`.

### Task C4: `mode show` and `mode verify`

**Files:**
- Create: `tools/lattice-placement/lattice_placement/live.py`, `verify.py`
- Modify: `cli.py`
- Test: `tests/test_live.py` (parsers over the real texts captured on 2026-09-24 — legacy, smart MDS 2, smart MDS 1 — stored under `tests/fixtures/config-show-*.txt` and `metrics-*.txt`), `tests/test_verify.py`, `tests/test_cli_show_verify.py` (fake `mds-admin` executable written by the test that echoes a fixture chosen by `--mds-host`)

**Interfaces:**

```python
@dataclass
class DsRow: ds_id: int; domain: str; state: str; capacity_age_ms: Optional[int]; avail: Optional[int]; total: Optional[int]; assessment_age_ms: Optional[int]; quality: Optional[str]; allowed: Optional[bool]; ppm: Optional[int]; ttl_ms: Optional[int]; weight: Optional[int]; reason: str
@dataclass
class Readiness: mode_active: bool; connector_config_valid: bool; connector_reachable: bool; last_batch_valid: bool; coverage: str; registered_ds: int; covered_ds: int; eligible_ds: int
@dataclass
class MdsState:
    host: str; ok: bool; error: Optional[str]
    desired_mode: Optional[str]          # from the file, None = not read ("?")
    mode: Optional[str]; mode_effective: Optional[str]; generation: Optional[str]; kernel_id: Optional[str]; build: Dict[str, int]
    readiness: Optional[Readiness]; config_digest: Optional[str]; profile_digest: Optional[str]; last_detail: Optional[str]
    ds: List[DsRow]
    metrics: Dict[str, float]            # pnfs_mds_placement_eligible_ds, rejections by reason, connector_reachable …
def parse_config_show(text_or_json: str) -> Dict[str, str]: ...
def parse_ds_row(value: str) -> DsRow: ...            # "domain=… state=… capacity_age_ms=none|123 …" (k=v tokens; the domain may contain ':' and '/', never spaces)
def parse_readiness(value: str) -> Readiness: ...
def parse_metrics(text: str) -> Dict[str, float]: ...  # only pnfs_mds_placement_* / pnfs_mds_connector_* lines
def read_mds(host: str, *, mds_admin: str, port: int, env: Dict[str,str], metrics_port: Optional[int], desired: Optional[str]) -> MdsState: ...  # subprocess + urllib, timeouts 5 s
def read_desired_mode(path: str, ssh_target: Optional[str]) -> Optional[str]: ...   # local read or `ssh <target> cat <path>`; parse with IniDocument

# verify.py (pure)
@dataclass
class Verdict: exit_code: int; errors: List[str]; warnings: List[str]; per_mds: List[Dict[str, Any]]
def verdict(states: List[MdsState], require_full: bool) -> Verdict: ...
def render_show(states: List[MdsState]) -> str: ...
```

`verify` rules: any state `not ok` → exit 2 (`MDS_UNREADABLE:<host>`); mismatch of `mode_effective`, `generation`, or `build` across MDS → exit 1 (`MODE_MISMATCH`, `GENERATION_MISMATCH`, `BUILD_MISMATCH` with the values per host); any MDS with `desired_mode` known and ≠ `mode_effective` → error `DESIRED_NE_EFFECTIVE:<host>` (exit 1 — the daemon has not been restarted); in smart: `connector_config_valid=0` / `connector_reachable=0` / `coverage=none` → exit 1 with the host; `config_digest` or `profile_digest` differing across MDS → exit 1 (`CONNECTOR_DIGEST_MISMATCH`); `coverage=partial` → warning listing the non-eligible DS with their reasons, exit 0, or exit 1 with `--require-full-coverage`. `show` prints per MDS: `host desired=<m|?> effective=<m> generation=<12 hex> kernel=<id> build=wrr=1 connector=1 prealloc=0`, the readiness line (smart), then one line per DS (`ds 0 domain=… state=… cap_age=…s avail=…/total … assess=VALID allowed=1 ppm=… ttl=…s weight=… reason=…`), then `WARN: MDS differ in <what>` lines computed by the same comparison as `verify`.

- [ ] **Step 1: Failing tests.** Parsers over the three fixtures (assert the numbers seen on the stand: MDS 2 smart `covered_ds=1`, ds 0 `weight=6422528000000`, ds 1 `reason=NO_BINDING`; MDS 1 `coverage=none`; legacy has no readiness). `verdict`: two identical smart states with partial coverage → exit 0 + warning; with `--require-full-coverage` → 1; MDS 1 none → 1; different generations → 1; one unreadable → 2; desired ≠ effective → 1. CLI: `mode show --mds 10.0.0.1,10.0.0.2 --mds-admin <fake>` renders both; `mode verify` exit codes; `--ssh` path uses a fake `ssh` on `PATH` that prints a fixture file.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(lattice-placement): mode show and mode verify over live MDS state`.

### Task C5: Packaging, this repo's CI, manifest check, operations runbook

**Files:**
- Create: `.github/workflows/ci.yml`, `scripts/check-manifests.py`, `docs/placement-modes/operations.md`
- Modify: `tools/lattice-placement/README.md`, `README.md` (repo index), `docs/TODO.md` (drop "Cross-repo manifest check", keep what stays open)

- [ ] **Step 1: `scripts/check-manifests.py`** (stdlib): (a) every `mds/manifest.json` patch file exists and its sha256 matches; (b) `tools/lattice-placement/lattice_placement/data/contract-manifest.json` is byte-identical to `docs/placement-modes/contract-manifest.json`; (c) with `--fork <path>` (CI clones `XinnorLab/pnfs-lattice` at `fork_sha`, depth enough for `6b4dcde..fork_sha`): `git format-patch --zero-commit --no-signature` into a temp dir reproduces the committed patches byte for byte; (d) defaults in the contract manifest equal the macros in the fork's `include/placement_modes.h` (`PM_DEFAULT_CAP_MAX_AGE_MS`, `PM_DEFAULT_CONN_POLL_MS`, `PM_DEFAULT_CONN_DEADLINE_MS`, `PM_DEFAULT_CONN_MAX_DS`, `PM_DEFAULT_CONN_ACCESS_SCOPE`, `PM_DEFAULT_CONN_SOCKET`, `PM_DOMAIN_WEIGHT_MAX`, `PM_WEIGHT_SCALE`) and the kernel id equals `0x58494e01` in `modules/wrr/wrr.c` of this repo and the fork's `src/modules/wrr/wrr.c`; (e) `profiles_with_placement_policy` equals the non-default names in the fork's `g_profiles`. Exit 1 with one line per finding. Test locally against `~/Documents/GitHub/pnfs-lattice`.
- [ ] **Step 2: CI** `ci.yml`: job `connector` (ubuntu-latest, python 3.9 and 3.12 matrix, `pip install -e 'connectors/lattice-ds-connector[test]'`, `pytest -q`); job `lattice-placement` (same matrix, `pytest -q` in `tools/lattice-placement`); job `manifests` (checkout, `git clone --filter=blob:none https://github.com/XinnorLab/pnfs-lattice fork && git -C fork checkout <fork_sha from mds/manifest.json>`, `python3 scripts/check-manifests.py --fork fork`). Triggers: push to main, pull_request.
- [ ] **Step 3: `operations.md`** — the CLI-04 procedure as numbered commands on a two-MDS cluster: 1 `mode validate <mode> --config /etc/pnfs-mds/mds.conf [--connector-socket …]` on **each** MDS; 2 drain new creates (maintenance window; what "drain" means here: stop the clients' create workload, existing layouts keep working); 3 `mode set <mode> --config … --apply` on each MDS (same `--set` values → same generation), compare the printed `sha256` of the files; 4 `systemctl restart pnfs-mds` one MDS at a time, wait for `:2049`; 5 `mode verify --mds m1,m2` from any node with `mds-admin` (exit codes table); 6 resume creates; rollback = `cp <backup> mds.conf && systemctl restart pnfs-mds` on the MDS that failed, then `verify` again — never leave the cluster half-switched; what `smart` needs before step 1 (connector unit active on **every** MDS, `lattice-ds-connector preflight` READY, same connector config → same `config_digest`); pinning `ds_connector_expected_config_digest` after the first successful switch; the health-veto warning when leaving smart; what `coverage=partial` means and when to use `--require-full-coverage`; the reasons table (`MODE_NOT_READY`, `NO_BINDING`, `ASSESSMENT_*`, `CONNECTOR_DENIED`, `CAPACITY_*`) with the operator action for each.
- [ ] **Step 4: README/pyproject**: install (`pip install ./tools/lattice-placement` or run from the tree with `PYTHONPATH`), the four commands with examples from the lab, exit codes.
- [ ] **Step 5: Run** `python3 scripts/check-manifests.py --fork ~/Documents/GitHub/pnfs-lattice` → clean; push; watch this repo's first CI run.
- [ ] **Step 6: Commit** `ci(pNFS): connector + lattice-placement tests, manifest check; docs: operations runbook`.

### Task C6: Stand — second connector on node223, the switch procedure end to end (announced; restarts both MDS)

Pre-condition: Sergey's go-ahead for the window. Runs from xinas-box.

**Files:**
- Create: `mds/scripts/pm-connector-install.sh` (`<node-ip>`: creates group `pnfs` + system user `lattice-ds-connector` if missing, installs the connector tree from the pNFS checkout tarball to `/opt/lattice-ds-connector`, copies `config.json`, `xinas-box-ca.pem` and the token from node225 with the same owners/modes (root:pnfs 640 / 644 / lattice-ds-connector:pnfs 600) via the box, installs the unit, `systemctl enable --now`, waits for `/healthz` 200, runs `preflight --expect-ds 0 --json`); also refreshes node225's `/opt/lattice-ds-connector` to the same tree (same code on both MDS)
- Modify: `mds/scripts/pm-deploy.sh` (`smart`/`legacy` now go through `lattice-placement mode set --apply` on each node — the helper is copied to `/root/lattice-placement/` — and print `mode verify`)
- Create: `docs/placement-modes/stand-2026-09-25.md`

- [ ] **Step 1** install the connector on node223; `preflight` on both nodes READY with identical `config_digest`.
- [ ] **Step 2** `mode validate smart` on both MDS (via the helper on each node; expected READY, socket reachable); `mode set smart --apply` on both (dry-run first, then apply; audit lines exist); `mode show` before the restart → `desired=smart effective=legacy` on both, `verify` exit 1 `DESIRED_NE_EFFECTIVE`.
- [ ] **Step 3** restart MDS 1 then MDS 2 (announced); `mode verify --mds 192.168.65.223,192.168.65.225` → exit 0 with the partial-coverage warning (DS 1 unbound), `--require-full-coverage` → exit 1; `mode show` shows both `coverage=partial`, identical generation and digests.
- [ ] **Step 4** 20 files via MDS 1 (`shard1`) → 20 : 0 (MDS 1 places now), 20 via MDS 2 → 20 : 0.
- [ ] **Step 5** `mode set legacy --apply --ds-weight 0=55 --ds-weight 1=45` on both, restart, `verify` exit 0, 40 files via MDS 1 ≈ 22 : 18.
- [ ] **Step 6** report rows + the exact commands; `docs/TODO.md`: drop "Second connector for MDS 1".

### Task C7: Stand — LAT-25 create benchmark, legacy vs smart

**Files:**
- Create: `mds/scripts/pm-bench.sh` (`<label> <nfiles> <parallel>`: on node225 against MDS 2 `shard2`, `N` empty-then-4-KiB files created by `P` parallel workers (`xargs -P`), wall-clock files/s; before/after snapshot of `pnfs_mds_placement_admit_seconds_{bucket,sum,count}` and `pnfs_mds_open_create_phase_ns_{sum,count}{phase="ds_prepare"|"total"}` on MDS 2 → prints throughput, mean admit µs, the p99 bucket, mean `ds_prepare` µs)
- Modify: `docs/placement-modes/stand-2026-09-25.md`

- [ ] **Step 1** legacy: 3 runs × 2000 files × P=8 (warm-up run discarded), record files/s and the histogram deltas.
- [ ] **Step 2** switch MDS 2 to smart (helper + restart, verify), 3 runs, same numbers; back to legacy.
- [ ] **Step 3** row in the report: medians, loss %, p99 bucket, verdict against ≤ 5 % / ≤ 10 %; if the histogram's 100 µs first bucket swallows both, say so and report the mean from `_sum/_count` as the finer figure.

### Task C8: Wrap-up

- [ ] Update `docs/superpowers/specs/2026-09-23-placement-modes-design.md` §7.7 wording ("`smart` is documented as NOT_READY until…") to point at the acceptance rows now met, and the "Delivery order" decision with the landed SHAs.
- [ ] `docs/TODO.md`: keep only what is still open (live switch, mirror_count, prealloc+smart, peer observations, config-show buffer, domain-id length, upstream promotion bug, flaky bench).
- [ ] Memory note + MEMORY.md line.

## Self-review

- Spec coverage: §9 build row/histogram (C0/C1), §10 four commands (C2–C4), §12 packaging + CI + manifest check (C5), §13 readiness/verify unit rows (C4 tests), stand row `show`/`verify` output and the perf row (C6/C7), CLI-01 (live MDS, desired vs effective, warning on differences), CLI-02 (read-only validate incl. connector), CLI-03 (dry-run default, local file only, backup, atomic, audit, re-validation, no restart), CLI-04 (operations.md), CLI-05 (out of scope, stated in operations.md). Acceptance §7.6 (smart→rr warning, rr→smart note, desired/effective, verify mismatch) in C3/C4.
- Placeholders: the connector install recipe copies what node225 has (owners/modes listed); the benchmark numbers are measured, not assumed.
- Type consistency: `IniDocument` (C2) used by C3/C4; `Report` (C2) used by C3's re-validation; `MdsState`/`Verdict` (C4) used by `pm-deploy.sh` output only through the CLI.
