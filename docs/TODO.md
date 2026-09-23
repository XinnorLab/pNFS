# Deferred work

One place for what a change deliberately left out: what is missing, what
the code does instead, why it was cut, what "done" looks like.  Delete an
entry when it lands.

## Placement modes (design `docs/superpowers/specs/2026-09-23-placement-modes-design.md`)

- **Stage B — `smart`.** The MDS connector client (`ds_connector.c`: UDS
  poll, envelope + binding validation, immutable assessment cache, TTL on
  the MDS clock), the readiness facts, `smart` weights (`fill` × ppm) and
  the connector `preflight`.  Today `placement_mode = smart` is refused
  at startup (`PLACEMENT_MODE_UNSUPPORTED_BUILD`); the gate returns
  `MODE_NOT_READY` if it is ever reached.  Done = spec §7 and §11 with
  the tests of §13 (binding, replay, TTL, no fallback).
- **Stage C — `lattice-placement` CLI, acceptance.** `mode show |
  validate | set | verify`, the operations runbook, the stand and
  performance acceptance rows (§10, §13).  Until then the mode is edited
  by hand and verified with `mds-admin config show`.
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
- **Cross-repo manifest check.** `docs/placement-modes/contract-manifest.json`
  is pinned by a fork unit test (`test_manifest_constants`) by hand; a CI
  job that diffs the two is Stage C.
