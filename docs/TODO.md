# Deferred work

One place for what a change deliberately left out: what is missing, what
the code does instead, why it was cut, what "done" looks like.  Delete an
entry when it lands.

## Placement modes (design `docs/superpowers/specs/2026-09-23-placement-modes-design.md`)

- **Live cluster-wide switch (CLI-05)**, `mirror_count > 1` in `smart`,
  `ENABLE_DS_PREALLOC=ON` with `smart` (LAT-18), per-filesystem mode
  override, health-driven rebalance: out of scope (spec §14).
- **Peer capacity observations for metadata-only MDS.** `fill`/`smart`
  trust only the local `statvfs`; an MDS without back-mounts sees every
  DS as `CAPACITY_UNKNOWN`.  Done = a timestamped observation column in
  the DS registry row and a freshness rule for merged rows.
- **`config show` buffer.** The whole response is one 8 KiB buffer
  (`handle_config_show_admin`, `cluster_transport.c`); rows that do not
  fit are dropped silently.  The sections render in a fixed order
  (identity, tuning, caches, placement, `render_cfg_ds`, `render_cfg_misc`),
  so when the buffer fills the later ones vanish first.  Each smart
  `placement_ds.<id>` row is about 45 bytes longer since verdict
  retention (`verdict=… hold_left_ms=…`) and the readiness row 26 bytes;
  with the lab's ~110-byte domain ids an unfiltered `config show` starts
  losing `placement_ds` rows at roughly 15–20 DS.  The "~32 keys at <256
  bytes each" comment next to the buffer is stale.  On large clusters use
  the `placement_ds.<id>` filter; the helper does not yet flag fewer rows
  than `registered_ds`.  Done = a paged or larger admin response in
  upstream (and the comment corrected), or a helper finding when
  `len(ds) < registered_ds`.
- **Adversarial review 2026-09-23, carried over.** (1) LAT-25 measured
  2026-09-24 (`stand-2026-09-24-stage-c.md`): no throughput loss, +1.7 µs
  per gate call; still open is the same row on a cluster with many DS
  (the gate is O(registered DS) per call). (2) `config show` prints four placement keys
  in legacy mode too (additive, needed by `mode show`); the log line is
  the only legacy-visible text kept identical. (3) Upstream bug found
  during the work: `promote_inline_to_ds` loops over the *requested*
  stripe count while `placement_select` may have shrunk it, creating
  phantom `ds 0` objects — legacy keeps the upstream behaviour on
  purpose; report upstream. (4) A DS whose NFS back-mount dropped is a
  failed probe for the sweep now, but the legacy `proportional` path
  still records the MDS root filesystem (upstream behaviour). Done =
  benchmark row in the stand report, an upstream issue for (3).
- **Flaky upstream test in the fork CI.** `bench_mk_rm_scale` (multi-threaded
  create/remove ladder against the in-memory catalogue, no placement code)
  failed once on ubuntu-latest and passed on re-run and on node225. Done =
  either an upstream fix for the memdb race or a retry/exclusion of that
  integration bench in `placement-modes.yml`; watch the next runs first.
- **Adversarial review 2026-09-24 (Stage B, wave 2), carried over.** All
  17 findings are fixed on the fork (96d02b8) except two that are
  behaviour, not bugs: (1) a backwards wall-clock step on the connector
  host drops batches as `OLD_GENERATED_AT` until the clock passes the
  last accepted `generated_at` or the connector restarts (new
  `runtime_epoch`) — `smart` fails closed, the detail names it; done =
  the connector CLI/runbook tells the operator to restart the unit on a
  clock step, or the connector bumps `runtime_epoch` itself when it sees
  its own clock go backwards. (2) The connector-side profile reload is
  accepted without a rebind now (digest checked per batch, not pinned);
  done = an acceptance row that reloads the profile on the stand and
  shows no `BINDING_MISMATCH`.
- **Stand 2026-09-24 (smart), carried over.** (1) The capacity domain the
  xinas profile publishes (`<controller>/<fs uuid>/<fs incarnation>:<device>`)
  is 123 bytes on the lab; the MDS row holds 127. A longer device path
  makes the record a shape error. Done = a length rule in
  `profile-xinas-mvp.md` (or a shorter domain form) and a connector-side
  check. (2) `pm-run.sh` now prints the tree it tests (`PM_TREE …`);
  `--no-sync` still means "the pushed base commit, no local changes".
- **Stage C stand 2026-09-24, carried over.** The MDS metrics listener
  resets a connection now and then (`Connection reset by peer` on
  `/metrics`); the helper retries once. Done = an upstream look at the
  metrics HTTP server's accept/close path.
- **Profile reload and clock-step rows** (from the wave-2 list above)
  and the LAT-25 row on a many-DS cluster remain the open acceptance
  rows; the `verify`-driven switch, partial coverage and the
  `DESIRED_NE_EFFECTIVE` gate are covered by `stand-2026-09-24-stage-c.md`.

## Smart verdict retention (design `docs/superpowers/specs/2026-09-29-smart-verdict-retention-design.md`)

- **The MDS patch series does not carry verdict retention yet.**
  `mds/manifest.json` still pins `fork_sha` 639b6c5 and `mds/patches/6b4dcde/`
  has no retention patches, while the contract manifest (1.3) and the
  connector's contract 1.1 describe the retention rows, metrics and TTLs
  up to one hour.  Deferred on purpose: the fork branch
  `xinnor/smart-verdict-retention` is not merged, and an export now would
  pin an unmerged SHA.  Done = after the fork PR merges into
  `xinnor/placement-modes`, run `scripts/export-patches.sh <fork checkout>`
  for the merged branch in the pNFS PR (or one merged together with it),
  so `mds/manifest.json` `fork_sha` and `mds/patches/` match; the pNFS PR
  merges only after that (`scripts/check-manifests.py` ok).
- **The stand trial (R10) has not run.** Verdict retention is proven by
  unit tests and the fork CI, not on the lab.  Done = the design §9 stand
  rows on node223/node225 with the shortened holds (`critical_hold_ms =
  180000`, `verdict_hold_ms = 90000` in the lab profile), in an announced
  window with the MDS upgraded first and the connector second, one
  `pm-bench.sh` run per mode, and a stand record under
  `docs/placement-modes/`.  The window also updates
  `ds_connector_expected_profiles` on every MDS (the profile digest
  changed) and installs the new connector unit file.
- **Recapture the helper's two synthetic fixtures after the stand.**
  `tools/lattice-placement/tests/fixtures/config-show-smart-retention.json`
  and `config-show-smart-steering-off.json` are built from the fork's
  rendered row format (test output of the fork build), not captured from a
  running MDS.  Done = both replaced by `mds-admin config show --json`
  captures from the retention build on the lab and the README note about
  them removed.
- **Synced `pm-run.sh` results before the 2026-09-29 fixes may have
  tested a stale tree.**  Two defects, one long-standing and one that
  made it worse.  (1) `9a37177` (which added the `PM_TREE` line) moved the
  local → box upload loop into the `--no-sync` `else` branch, so a synced
  run never uploaded the working tree at all: node225 extracted whatever
  tarball an earlier run had left on the box, while `PM_TREE` still
  printed `(synced)`.  `b7ac016` put the loop back.  This is the primary
  cause for runs between `9a37177` and `b7ac016`.  (2) The box → node225
  copy of the tarball swallowed a failed `scp` (`|| true`, from `5519e04`),
  so a failed copy left node225 extracting an older archive as well.
  Fixed in `60b93f4` (a failed copy prints `PM_RESULT SYNC_FAILED` and
  exits 3; the archive is deleted after extraction).  What node225
  reported for synced runs between `9a37177` and `b7ac016` (defect 1), and
  in principle for any synced run before `60b93f4` (defect 2), may not
  describe the tree the run claimed.  Done = the results the design or a
  stand record relies on are re-run synced on the fixed script, or
  confirmed by a `--no-sync` run of the same commit or the fork CI.
