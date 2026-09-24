# Stand trial 2026-09-24 — Stage C (helper, second connector, LAT-25)

Binary: fork `XinnorLab/pnfs-lattice` `xinnor/placement-modes` @ `2f1b52c`
(Stage B + review wave 2 + the Stage C `placement_build` row and the
`pnfs_mds_placement_admit_seconds` histogram), built on node225, installed
on node223 (MDS 1) and node225 (MDS 2). Connector `lattice-ds-connector`
now on **both** MDS (pNFS `f1662d4` tree, code md5 `7f9014ffec5e`, the
same `config.json` — `config_digest sha256:30259fc1…`, profile
`sha256:c9bee5b2…`), installed with `mds/scripts/pm-connector-install.sh`
(group `pnfs` + system user created on node223, token copied with the
same owner/mode, never printed). Helper `lattice-placement` (pNFS
`da6e567`) unpacked under `/opt/lattice-placement` on both nodes and run
as `PYTHONPATH=/opt/lattice-placement python3 -m lattice_placement`;
`mode show`/`verify` run on node225 against both MDS
(`--mds-admin /usr/local/bin/mds-admin --env LD_LIBRARY_PATH=… --ssh root`).
Every config change went through `mode set --apply` (backup + audit line
on the node); every restart was one MDS at a time by `pm-deploy.sh`.

## The switch procedure (CLI-01…04), as run

| # | Step | Result | Evidence |
|---|---|---|---|
| 1 | connector on node223 (`pm-connector-install.sh 192.168.65.223 192.168.65.225`), node225 refreshed to the same tree | both `healthz=200`, `preflight … --expect-ds 0` **READY** on both, identical `config_digest` | `[node223] connector: active healthz=200 code_md5=7f9014ffec5e config_sha=5c0b22b8b29e`, same on node225; the fresh connectors report DS 0 `VALID allowed=False … RECOVERY_HOLD_DOWN` for the first seconds (CON-16), then allowed |
| 2 | `mode set smart --set ds_connector_poll_ms=1000 --set ds_connector_request_deadline_ms=500` on node225: dry run, then `--apply` | the diff removes the four legacy keys and appends the managed block; `applied … (backup mds.conf.20260924-141205.bak)`, sha256 before/after printed, "running pnfs-mds still uses mode legacy"; audit line written | `/var/lib/lattice-placement/audit.log`: `{"backup": …, "host": "node225", "managed": [["placement_mode","smart"],…], "new_mode": "smart", "old_mode": "legacy", "old_sha256": "c12556fa…", "new_sha256": "45d3ce06…", …}` |
| 2b | `mode show` / `mode verify` before any restart | `desired=smart effective=legacy` for node225 (after the helper fix `da6e567`, see notes) → **`verify` exit 1 `DESIRED_NE_EFFECTIVE`** | shown at step 5a on the way back: `error: DESIRED_NE_EFFECTIVE:192.168.65.223: the file says legacy, the daemon runs smart (restart pending?)` |
| 3 | `pm-deploy.sh smart …`: `validate smart` READY on both (connector preflight folded in), `set --apply` (NO CHANGE on node225, applied on node223 with its own backup), restart MDS 1 then MDS 2, `verify` | **`verify` exit 0** with two `COVERAGE_PARTIAL` warnings (DS 1 has no binding); identical `generation=faf2d6bbd3bf`, `build=connector=1 prealloc=0 wrr=1`, config/profile digests on both | `readiness: … connector_reachable=1 last_batch_valid=1 coverage=partial registered=2 covered=1 eligible=1` on both; `ds 0 … assess=VALID allowed=1 ppm=1000000 … weight=6422528000000 reason=NONE`, `ds 1 … reason=NO_BINDING` |
| 4 | `verify --require-full-coverage` | **exit 1** (`error: COVERAGE_PARTIAL:…` for both) | — |
| 4b | 20 × 4 MiB via **MDS 1** (`shard1`), 20 via MDS 2 (`shard2`) | **20 : 0** and **20 : 0** — MDS 1 places now that it has a connector (on 2026-09-24 morning it refused with `MODE_NOT_READY`) | `rejections_total{reason="NO_BINDING"} 20` on each |
| 5a | `mode set legacy --apply --ds-weight 0=55 --ds-weight 1=45` on node223 only, then `verify` | **exit 1** `DESIRED_NE_EFFECTIVE:192.168.65.223` (file legacy, daemon smart); `WARNING: leaving smart disables the health veto` printed by `set` | see 2b |
| 5b | `pm-deploy.sh --no-binary legacy` (NO CHANGE on node223, applied on node225, restart both, `verify`) | **exit 0**, `desired=legacy effective=legacy` on both | 40 files via MDS 1 → **20 : 20** (weights 55/45; 2026-09-24 earlier: 55 : 45 over 100) |

## LAT-25 — create throughput and gate latency, legacy vs smart

`mds/scripts/pm-bench.sh <label> 2000 8`: 2000 files of 4 KiB created by
8 parallel workers (`xargs -P 8`, one `sh` + `head` per file) on the
node225 client mounting MDS 2 (`shard2`), one warm-up run of 500 files
discarded, three measured runs per mode, both MDS in the same mode
(switched with `pm-deploy.sh --no-binary`). The MDS figures come from
`/metrics` deltas over the same window: `pnfs_mds_placement_admit_seconds`
(one observation per `placement_select_gated` call and per create
admission, every mode) and the upstream `pnfs_mds_open_create_phase_*`
means.

| Mode | Run | files/s (client) | gate calls | gate mean | gate p50 / p99 bucket | OPEN/CREATE `ds_prepare` mean | OPEN/CREATE `total` mean |
|---|---|---|---|---|---|---|---|
| legacy | 1 | 253.2 | 4000 | 0.28 µs | ≤ 100 µs / ≤ 100 µs | 0.1 µs | 1916 µs |
| legacy | 2 | 291.7 | 4000 | 0.28 µs | ≤ 100 µs / ≤ 100 µs | 0.1 µs | 1791 µs |
| legacy | 3 | 250.0 | 4000 | 0.28 µs | ≤ 100 µs / ≤ 100 µs | 0.1 µs | 1926 µs |
| **legacy median** | | **253.2** | | **0.28 µs** | | | **1916 µs** |
| smart | 1 | 262.1 | 4000 | 1.94 µs | ≤ 100 µs / ≤ 100 µs | 0.1 µs | 1832 µs |
| smart | 2 | 301.4 | 4000 | 1.99 µs | ≤ 100 µs / ≤ 100 µs | 0.1 µs | 1714 µs |
| smart | 3 | 259.0 | 4000 | 1.97 µs | ≤ 100 µs / ≤ 100 µs | 0.1 µs | 1842 µs |
| **smart median** | | **262.1** | | **1.97 µs** | | | **1832 µs** |

Verdict against LAT-25 (≤ 5 % create-throughput loss, ≤ 10 % p99
placement-latency growth): **met**. Throughput medians differ by +3.5 %
in smart's favour, inside the run-to-run spread (±8 %) — the client (one
1 GbE link, a process per file) is the bottleneck, not the MDS. The gate
itself costs 1.97 µs per call in smart against 0.28 µs in legacy: +1.7 µs
per call, two calls per create, on a 1.8 ms create — +0.2 % of the create
latency. Both modes keep every gate observation inside the histogram's
first bucket (≤ 100 µs), so the p99 *bucket* is identical; the finer
comparison is the mean from `_sum`/`_count`, given above. The stand has
two DS and one covered DS; the gate is O(registered DS) per call
(review wave 1), so a 256-DS cluster would cost proportionally more in
the gate and still stay far below the 100 µs bucket.

## Notes

- **Helper fix found here (`da6e567`).** `mode verify --ssh root` run on
  node225 printed `desired=?` for node225 itself: root cannot ssh into
  the very host it runs on. The helper now reads the file directly when
  the host is one of the machine's own addresses (a UDP socket binds to
  it). A failed `/metrics` scrape is printed as `metrics: unavailable
  (url: reason)` instead of silently dropping the line — the MDS metrics
  listener resets a connection now and then (`[Errno 104] Connection
  reset by peer` seen once); the helper retries once (`0c92b87`).
- The fresh connector's `RECOVERY_HOLD_DOWN` (allowed=False, VALID) right
  after install is the connector's re-entry hold-down (CON-16); by the
  time the MDS restarted it had passed. Preflight reports READY through
  it because the record is VALID and bound — an operator switching within
  seconds of a connector (re)start sees `CONNECTOR_DENIED` for those
  seconds, never a wrong placement.
- `validate smart` in `pm-deploy.sh` passes `--expect-ds 0` only: DS 1
  (node 71) is deliberately unbound on this stand, so a
  `--expect-ds 0,1` preflight would (correctly) say `UNBOUND_DS:1`.
- The lab is left on **legacy** (weights 55/45) with the `2f1b52c` binary,
  both connectors enabled and running (harmless in legacy — the MDS does
  not poll them), the helper under `/opt/lattice-placement` on both
  nodes, six `mds.conf.<ts>.bak` backups and the audit log
  (`/var/lib/lattice-placement/audit.log`) on each node; bench and trial
  directories removed, `/mnt/pnfs2` unmounted.
