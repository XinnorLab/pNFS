# Deferred work

One place for what a change deliberately left out: what is missing, what
the code does instead, why it was cut, what "done" looks like.  Delete an
entry when it lands.

## Placement modes (design `docs/superpowers/specs/2026-09-23-placement-modes-design.md`)

- **Stage C — `lattice-placement` CLI, acceptance.** `mode show |
  validate | set | verify`, the operations runbook, the stand and
  performance acceptance rows (§10, §13).  Until then the mode is edited
  by hand and verified with `mds-admin config show --mds-host <bind addr>
  --mds-port <grpc_port>` plus `lattice-ds-connector preflight`.
- **Second connector for MDS 1.** The lab runs the connector on node225
  (MDS 2) only; MDS 1 in `smart` reports `coverage=none` and refuses new
  placements by design. Done = the connector installed on node223 with
  the viewer token and the same config (Stage C acceptance for two MDS).
- **Live cluster-wide switch (CLI-05)**, `mirror_count > 1` in `smart`,
  `ENABLE_DS_PREALLOC=ON` with `smart` (LAT-18), per-filesystem mode
  override, health-driven rebalance: out of scope (spec §14).
- **Peer capacity observations for metadata-only MDS.** `fill`/`smart`
  trust only the local `statvfs`; an MDS without back-mounts sees every
  DS as `CAPACITY_UNKNOWN`.  Done = a timestamped observation column in
  the DS registry row and a freshness rule for merged rows.
- **`config show` buffer.** Per-DS rows are appended to the 8 KiB
  response; on large clusters use the `placement_ds.<id>` filter.  Done =
  a paged or larger admin response in upstream.
- **Adversarial review 2026-09-23, carried over.** (1) LAT-25 performance
  budget is unmeasured: the create boundary runs the gate per stripe
  slot; O(rows) per call after wave 1, but no benchmark vs legacy yet
  (Stage C acceptance row). (2) `config show` prints four placement keys
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
- **Cross-repo manifest check.** `docs/placement-modes/contract-manifest.json`
  is pinned by a fork unit test (`test_manifest_constants`) by hand; a CI
  job that diffs the two is Stage C.
