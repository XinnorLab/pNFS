# Placement modes — operations runbook

The supported way to switch a pnfs-lattice cluster between `legacy`, `rr`,
`fill` and `smart` (requirement CLI-04; design spec §10). There is **no
online switch**: the mode is a start-time setting of every MDS, and the
helper never restarts a daemon for you. What the helper does do is make
each step checkable — validate before, verify after, and refuse to write a
file the MDS would reject.

Tools: `lattice-placement` (`tools/lattice-placement/`, Python 3.9,
stdlib; install with `pip install ./tools/lattice-placement` or run
`PYTHONPATH=tools/lattice-placement python3 -m lattice_placement …`),
`mds-admin` (the MDS build; on the lab
`LD_LIBRARY_PATH=/opt/rondb/lib:/opt/rondb/lib/mysql /usr/local/bin/mds-admin`),
`lattice-ds-connector preflight` (needed for `smart`).

Everything below assumes the placement-modes binary on every MDS
(`config show` prints `placement_build = …`; a daemon without that row
predates the modes and `verify` says so).

## Exit codes

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `mode validate` | READY | NOT_READY (errors listed) | the file cannot be read |
| `mode set` | dry-run printed / applied / no change | REFUSED (the result would not validate; the backup was restored if it was written) | bad arguments or I/O |
| `mode verify` | same effective mode, generation and build on every MDS; `smart`: connector valid + reachable, coverage full or partial | any difference, a desired mode that is not effective yet, connector invalid/unreachable, coverage none, or partial with `--require-full-coverage` | an MDS could not be read |
| `mode show` | rendered | — | an MDS could not be read |

## The switch, step by step

Run the helper on each MDS host for `validate` and `set` (they read and
write the **local** file); run `show`/`verify` from any host that has
`mds-admin` and reaches the cluster-transport port of every MDS
(`cluster_bind_addr:grpc_port`, 50051 on the lab — not loopback).

1. **Validate the target mode on every MDS** (read-only):

   ```bash
   lattice-placement mode validate fill --config /etc/pnfs-mds/mds.conf
   # smart additionally asks the LOCAL connector:
   lattice-placement mode validate smart --config /etc/pnfs-mds/mds.conf \
       --connector-socket /run/lattice-ds-connector/connector.sock --expect-ds 0,1
   ```

   `validate` judges the file as `mode set` would leave it (legacy keys
   are a warning, not an error); `--strict` judges the file as it is.
   `NOT_READY` with `CONNECTOR:…` reasons means the connector on **this**
   host is not ready — fix that before touching the config. A passed
   preflight is not a guarantee that the connector stays healthy; `verify`
   after the restart is the real check.

2. **Drain new creates** for the maintenance window: stop the workloads
   that create files. Existing layouts keep working through the restart of
   one MDS at a time (the other MDS serves its own shards); nothing about
   existing files changes with the mode (only new allocations are placed
   by it).

3. **Write the same configuration on every MDS**:

   ```bash
   lattice-placement mode set smart --config /etc/pnfs-mds/mds.conf \
       --set ds_connector_poll_ms=1000 --set ds_connector_request_deadline_ms=500      # dry-run: prints the diff
   lattice-placement mode set smart --config /etc/pnfs-mds/mds.conf \
       --set ds_connector_poll_ms=1000 --set ds_connector_request_deadline_ms=500 --apply
   ```

   `--apply` keeps `mds.conf.<ts>.bak`, writes atomically, re-validates
   what is on disk (and restores the backup if that fails), appends one
   JSON line to `/var/lib/lattice-placement/audit.log` and prints the
   sha256 before/after. Use identical `--set` values on every MDS: the
   MDS derives `placement_config_generation` from the placement keys, and
   `verify` requires it to be identical. Back to legacy:
   `mode set legacy --apply --ds-weight 0=55 --ds-weight 1=45`.

   Leaving `smart` prints the health-veto warning (a denied or UNKNOWN DS
   receives new objects again). Entering `smart` prints that no DS is
   admitted until the first fresh VALID assessment.

4. **Restart the MDS one at a time**, waiting for each to serve again:

   ```bash
   systemctl restart pnfs-mds && until ss -tln | grep -q ':2049 '; do sleep 2; done
   ```

   Before the restart `mode show` prints `desired=<new> effective=<old>` —
   the helper distinguishes the two on purpose.

5. **Verify** from one host:

   ```bash
   lattice-placement mode verify --mds 192.168.65.223,192.168.65.225 \
       --mds-admin /usr/local/bin/mds-admin --env LD_LIBRARY_PATH=/opt/rondb/lib:/opt/rondb/lib/mysql \
       --ssh root            # reads /etc/pnfs-mds/mds.conf on each host for the desired mode
   ```

   Exit 0 = the switch is complete. `COVERAGE_PARTIAL` is a warning: the
   named DS are not eligible yet (their reasons are printed — `NO_BINDING`
   needs a connector binding, `ASSESSMENT_UNKNOWN` a healthy source,
   `CAPACITY_*` a capacity observation); the cluster places on the others.
   `--require-full-coverage` turns that into exit 1 for operators who gate
   on it.

6. **Resume creates.**

**If any MDS did not come back or `verify` is not 0:** do not call the
cutover done. Restore that MDS from its backup and restart it
(`cp /etc/pnfs-mds/mds.conf.<ts>.bak /etc/pnfs-mds/mds.conf && systemctl restart pnfs-mds`),
then `verify` again — the cluster is either fully on the new mode or fully
on the old one, never half-switched (a half-switched cluster is what
`MODE_MISMATCH` / `GENERATION_MISMATCH` report).

## Upgrading to per-profile digest pins

This build replaces the single `ds_connector_expected_profile_digest` pin
with a per-profile map, `ds_connector_expected_profiles`. Three things to
know before rolling it out:

- **Upgrade the connector before `lattice-placement`.** `mode validate`
  now passes `--expect-profiles` to `lattice-ds-connector preflight`; an
  older connector does not know that flag and will refuse it.
- **The MDS config generation of every `smart` MDS changes with this
  build** (the hashed config line that feeds `generation` now includes
  `conn_profiles=…` instead of the old single-digest line). During a
  rolling upgrade, `mode verify` reports `GENERATION_MISMATCH` between
  hosts already on the new build and hosts still on the old one — expected
  until every MDS runs the new build, not a sign the switch went wrong.
- **Replace `ds_connector_expected_profile_digest` with
  `ds_connector_expected_profiles` before restarting any MDS.** The MDS
  refuses the old key as a config error; `mode set` migrates it away
  automatically wherever it sits in the file, so running `mode set
  <mode>` (or re-running it with no mode change) before the restart is
  enough.

## Upgrading to `ds_path` bindings

Add `ds_path` to the connector bindings of DS registered below a share
before upgrading the MDS: an MDS without the change ignores `ds_path`
and still accepts the record; an MDS with it rejects a parent without
`ds_path` (`rejected_binding`, detail `does not match the registry`).

## `smart` prerequisites

- A `lattice-ds-connector` unit on **every** MDS host (each MDS reads only
  its local socket; an MDS without one reports `coverage=none` and refuses
  every new placement in its shards — by design, and `verify` fails).
- The same connector configuration on every host (same
  `config_digest`); `verify` compares the digests the MDS report. After
  the first successful switch, pin them:
  `--set ds_connector_expected_config_digest=sha256:… --set ds_connector_expected_profiles=xinas-mvp=sha256:…`.
- `lattice-ds-connector preflight --expect-ds <every registered id>` READY
  on every host.

## A DS in a subdirectory of a share

Bind the share in `export_path` and the `ds[N]` path in `ds_path` — the
stand example: `export_path: /mnt/data`, `ds_path: /mnt/data/pnfs-ds`.
The connector vetoes `DS_PATH_UNDER_NESTED_SHARE` when another share lies
in between.

## Reading `show`

```
192.168.65.225: desired=smart effective=smart generation=faf2d6bbd3bf kernel=58494e01 build=connector=1 prealloc=0 wrr=1
  readiness: mode_active=1 connector_config_valid=1 connector_reachable=1 last_batch_valid=1 coverage=partial registered=2 covered=1 eligible=1
  connector: config_digest=sha256:30259fc1… profiles=xinas-mvp=sha256:c9bee5b2… last=accepted 1
  ds 0   ONLINE   domain=e4e9…:/dev/xi_data cap_age=1.4s avail=41.4TB/42.2TB assess=VALID allowed=1 ppm=1000000 ttl=14.9s age=0.4s weight=6422528000000 reason=NONE
  ds 1   ONLINE   domain=ds:1 cap_age=1.4s avail=33.9TB/34.6TB assess=NONE allowed=0 ppm=0 ttl=0.0s age=- weight=0 reason=NO_BINDING
  metrics: eligible_ds=1 rejections=NO_BINDING=20
```

| `reason` | Meaning | Operator action |
|---|---|---|
| `NONE` | eligible, `weight` is its share | — |
| `MODE_NOT_READY` | `smart` without a usable assessment view (connector never answered) | start/fix the local connector; check `last=` |
| `NO_BINDING` | the connector has no binding for this DS | add the binding (`lattice-ds-connector discover` for the incarnation) |
| `ASSESSMENT_UNKNOWN` | the connector answered but cannot vouch (source down/stale, evidence incomplete) | look at the connector's reason codes (`lattice-ds-connector show`) and the source (xiNAS agent) |
| `ASSESSMENT_STALE` | the last VALID record aged out (connector stopped or wedged) | restart the connector; `reachable=0` says the socket is gone |
| `CONNECTOR_DENIED` / `ZERO_MULTIPLIER` | the connector denies new allocations on this DS | the DS is unhealthy by profile; fix the DS |
| `CAPACITY_UNKNOWN` / `CAPACITY_STALE` | no (fresh) `statvfs` of the back-mount on this MDS | mount the DS back-mount on the MDS; check `ds_capacity_poll_ms` |
| `CAPACITY_FULL` | `available <= placement_min_free_bytes` | expected; raise capacity or lower the threshold |
| `DOMAIN_MAP_CONTRADICTION` / `SHARED_FS_ALIAS_UNMAPPED` / `DOMAIN_MAP_MISMATCH` | the declared capacity domains disagree with what `f_fsid` proves | fix `ds_capacity_domain.<id>` (see `docs/placement-modes.md` in the fork) |
| `DS_OFFLINE` | admin state | `mds-admin ds set-state` |

## What the helper will not do (CLI-05)

No rolling restart, no cluster-wide atomic switch, no edits to the
connector profile or to xiNAS. A future `mds-admin placement mode set`
with a cluster-wide change generation, quiesce and prepare/commit is out
of scope for the MVP; the per-MDS health states are never synchronised —
only the configuration is.
