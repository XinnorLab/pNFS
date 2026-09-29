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
`lattice-ds-connector preflight` (for `smart`, which steers only with it).

Everything below assumes the placement-modes binary on every MDS
(`config show` prints `placement_build = …`; a daemon without that row
predates the modes and `verify` says so).

## Exit codes

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `mode validate` | READY | NOT_READY (errors listed) | the file cannot be read |
| `mode set` | dry-run printed / applied / no change | REFUSED (the result would not validate; the backup was restored if it was written) | bad arguments or I/O |
| `mode verify` | same effective mode, generation and build on every MDS and no error reported; `smart`: an unreachable connector and coverage partial or none are **warnings** (the cluster places neutrally, it does not steer) | any difference, a desired mode that is not effective yet, `connector_config_valid=0`, a connector config digest or profile map that differs between MDS, an unreadable (`CONNECTOR_PROFILES_INVALID`) or, in `smart`, absent (`CONNECTOR_PROFILES_MISSING`) profile pin row; with `--require-full-coverage` also partial, none or retained coverage | an MDS could not be read |
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
   `smart` places without a connector, so `CONNECTOR:…` findings are
   **warnings** here, not `NOT_READY`: fix the connector on **this** host
   before the switch if you want the mode to steer from the first
   allocation. The file's own errors still make `validate` `NOT_READY`.
   A passed preflight is not a guarantee that the connector stays
   healthy; `verify` after the restart is the real check.

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

   Leaving `smart` prints the health-veto warning (a DS the connector
   denies receives new objects again). Entering `smart` prints that a DS
   without a connector verdict is placed neutrally, as in `fill` (see
   "What neutral means" below).

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

   Exit 0 = the switch is complete: every MDS runs the same mode,
   generation and build. It does **not** prove that `smart` is steering.
   `COVERAGE_PARTIAL` is a warning: the named DS have no verdict in force
   and are placed neutrally (their gate reasons are printed; `CAPACITY_*`
   needs a capacity observation). `STEERING_OFF` (`coverage=none`) and
   `CONNECTOR_UNREACHABLE` are warnings too: the cluster places
   neutrally. Two findings fail on their own: `NO_ELIGIBLE_DS` (the MDS
   admits no DS) and, against an MDS that predates verdict retention,
   `COVERAGE_NONE` / `CONNECTOR_UNREACHABLE` (that MDS refuses a DS
   without a verdict). Where steering must be proven, gate on
   `--require-full-coverage`: it makes partial and no coverage exit 1,
   and also a DS held only by a retained verdict (`COVERAGE_RETAINED`,
   the connector is not observing it). The flag gates coverage and
   retention only: `CONNECTOR_UNREACHABLE` stays a warning under it, so a
   connector that has just died still reads as full coverage until its
   verdicts run out (see "Alerts for `smart`").

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

Follow this order; each step depends on the one before it. See also "A DS
in a subdirectory of a share" below for the `ds_path`/`export_path`
syntax itself.

1. **Upgrade the connector on every MDS host.** An old connector build
   does not reject an unknown key, so it ignores `ds_path` in its config
   — upgrading the connector first is safe, and is required before step 2
   can do anything.
2. **Add `ds_path` to the bindings of the DS registered below a share, on
   every host, then reload it (`SIGHUP`) or restart it**
   (`systemctl restart lattice-ds-connector`). Do this on every host
   *before* upgrading any MDS (step 5): once an MDS runs the new build it
   rejects a binding that lacks `ds_path` for a DS under a share
   (`rejected_binding`, detail `does not match the registry`). While the
   rollout is in progress, hosts already updated and hosts not yet
   updated report different `config_digest` values — `mode verify`'s
   `CONNECTOR_CONFIG_DIGEST_MISMATCH` during this window is expected, not
   a failed switch; it clears once every host has `ds_path` added.
3. **Check the binding took**: `lattice-ds-connector preflight
   --expect-ds <id>` prints `ds_path=/mnt/data/pnfs-ds` (not `-`) on that
   DS's row.
4. **If `ds_connector_expected_config_digest` is pinned, re-pin it on
   every MDS before restarting any of them.** `config_digest` hashes the
   raw connector config, so adding `ds_path` changes it; with the old
   pin still in place every MDS drops every batch: `smart` gets no new
   verdicts (with verdict retention the verdicts in force run out and the
   DS are placed neutrally; an MDS build without it refuses all
   placements) until the pin is updated. Read the new digest off
   `lattice-ds-connector preflight` or `show` (`config_digest=sha256:…`,
   identical on every host once step 2 is done everywhere), then on every
   MDS:

   ```bash
   lattice-placement mode set smart --config /etc/pnfs-mds/mds.conf \
       --set ds_connector_expected_config_digest=sha256:… --apply
   ```

5. **Upgrade the MDS**, one host at a time (see "The switch, step by
   step" above), then `lattice-placement mode verify`. Check
   `placement_connector_last_detail` on each host has no `rejected_binding`
   "does not match the registry" detail — that means some binding still
   lacks `ds_path`, or its `ds_path` differs from the registered `ds[N]`
   path; see `connectors/lattice-ds-connector/docs/troubleshooting.md`.

## Upgrading to verdict retention

Design: `docs/superpowers/specs/2026-09-29-smart-verdict-retention-design.md`.
The MDS build, the connector (contract 1.1) and the helper change
together; this is the order.

1. **MDS first, the connector right after it, host by host.** Upgrade
   the MDS build on a host (see "The switch, step by step" for the
   restart), then the connector on that host. The new MDS with the
   *current* connector (contract 1.0, 20 s TTLs, `UNKNOWN` on failure)
   holds verdicts for the connector's 20 s and places the DS neutrally
   afterwards instead of refusing it, with one exception: the current
   connector publishes the denies that carry no evidence time
   (`SHARE_ABSENT`, `IDENTITY_MISMATCH`) as `UNKNOWN`
   (`EVIDENCE_EXPIRED`). The old MDS refused on those; the new MDS places
   that DS neutrally. On an MDS without profile pins, for as long as that
   pairing runs, a DS whose share was deleted or whose binding names the
   wrong controller receives new files, so do not leave a host in it.
   (With the pins of step 2 the MDS rejects every record of the old
   connector anyway, and every DS is neutral until the new connector
   runs.)
   The reverse pairing — the new connector with an old MDS — is accepted
   (TTL up to 1 h) and holds denies for their hold, but that MDS still
   refuses on `UNKNOWN`: do not run it on purpose. Upgrade the
   `lattice-placement` helper after the MDS: against an MDS that predates
   retention it keeps `CONNECTOR_UNREACHABLE` and `COVERAGE_NONE` as
   errors (that MDS refuses a DS without a verdict), so a new helper's
   `verify` is strict until every MDS runs the new build.
2. **Update the profile pins in the same window.** `critical_hold_ms` and
   `verdict_hold_ms` are part of the profile digest, so the digest of
   every profile changes with the connector. If
   `ds_connector_expected_profiles` is pinned, set the new digest on
   **every MDS** in the same window, or the MDS rejects every record
   (`rejected_binding`, digest differs from the pin) and the cluster
   places neutrally without steering until you do. Read the new digest
   before restarting the unit: `lattice-ds-connector validate-config
   --config /etc/lattice-ds-connector/config.json` with the new build
   prints `profile xinas-mvp … digest=sha256:…`. Then, on each MDS:

   ```bash
   lattice-placement mode set smart --config /etc/pnfs-mds/mds.conf \
       --set ds_connector_expected_profiles=xinas-mvp=sha256:<new> --apply   # plus every other --set you keep
   ```

   A placement key is read at start, so the pin takes effect with the MDS
   restart of step 1: apply it before that restart, restart the MDS, then
   install and restart the new connector on that host. Between the two
   restarts the DS are neutral.
3. **Install the new unit file with the connector**
   (`systemd/lattice-ds-connector.service`: `StateDirectory`,
   `ReadWritePaths` for `/var/lib/lattice-ds-connector`), then
   `systemctl daemon-reload` and restart. With an old unit the connector
   runs, but it cannot write its state file (`state_write_failed`,
   `connector_state_write_errors_total`) and a restart forgets the
   verdicts.
4. **Do not add `state_path` to deployed connector configs unless you
   need another path.** The key changes `config_digest`, so a pinned
   `ds_connector_expected_config_digest` would need re-pinning on every MDS;
   without the key the default path
   (`/var/lib/lattice-ds-connector/verdicts.json`) applies. The same holds
   for explicit `critical_hold_ms` / `verdict_hold_ms` in a profile of
   `config.json` (the lab's shortened holds, for one): they change
   `config_digest` (it is over the document, so even the default values
   do) and, with other values, the profile digest. Re-pin
   `ds_connector_expected_config_digest` on every MDS as well as the
   profile digests of step 2, or the MDS drops every batch.
5. **Re-gate scripts on steering, and do not read a dead connector off
   `verify`.** `mode verify` exits 0 for a smart cluster that is not
   steering (`STEERING_OFF`, `CONNECTOR_UNREACHABLE` are warnings). A
   deploy or health script that used its exit code to catch a dead
   connector no longer does. `--require-full-coverage` gates coverage and
   retention only, and `CONNECTOR_UNREACHABLE` stays a warning under it:
   a connector that has just died, with its verdicts still `fresh` inside
   their hold, gives `coverage=full` and `retained_ds=0`, so `verify` exits
   0 even with the flag. It fails only once the 10 or 20 minute hold runs
   out (or when the verdicts were already retained). The reliable
   dead-connector signal is `pnfs_mds_connector_reachable`: alert on it
   (see "Alerts for `smart`").

## `smart` prerequisites

- A `lattice-ds-connector` unit on **every** MDS host to steer (each MDS
  reads only its local socket; an MDS without one reports `coverage=none`
  and places every DS neutrally — `verify` warns
  `STEERING_OFF` and `--require-full-coverage` fails).
- The same connector configuration on every host (same
  `config_digest`); `verify` compares the digests the MDS report. After
  the first successful switch, pin them:
  `--set ds_connector_expected_config_digest=sha256:… --set ds_connector_expected_profiles=xinas-mvp=sha256:…`.
- `lattice-ds-connector preflight --expect-ds <every registered id>` READY
  on every host.

## What neutral means

`smart` steers placement; it does not decide whether a data store is
usable. A DS without a **verdict in force** is *neutral*: it is placed as
in `fill` — multiplier 1 000 000 ppm on the base weight of the
operator's capacity domain (`ds_capacity_domain.<id>`, else `ds:<id>`):
the fill level, or, with `placement_allow_manual_base_weights`, that
domain's `placement_domain_weight.<domain>`. Under manual weights,
declare them for these operator domains too, or a neutral DS falls back
to the fill scale (1..100), which does not compare with manual weights
(1..10 000). The
gates the MDS owns still apply to a neutral DS exactly as in `fill`: the
DS state (`ONLINE`, not admin-excluded), the capacity gate (`statvfs` of
the back-mount, `placement_min_free_bytes`, observation freshness) and
the alias grades. A dead or full DS still drops out through them; a
connector that is down cannot take a healthy DS out of service.

A DS is neutral when the connector never reported on it (it has no
binding for it), when the connector never answered, when the MDS has had
no accepted batch since it started, when its verdict ran out, or when a
rebind (a strictly higher `binding_generation`) cleared it. Removing a
binding does **not** end a verdict the MDS already holds: no record
replaces it, so a deny stays for the rest of its hold (up to 20 minutes)
and an allow for the rest of its own. The same goes for a DS
re-registered under a reused `ds_id` with a new endpoint: the MDS rejects
the old binding's records for it (the endpoint differs — no new data), so
the old DS's verdict applies to the new one until it runs out. To end a
verdict at once, bind the DS with a higher `binding_generation`: the MDS
clears the old verdict on the first record of the new generation, and
what stands then is a fresh observation (or neutral), not the old
verdict. To keep a DS out of placement whatever its verdict, use its
admin state (`mds-admin ds set-state`).

A **verdict** is the placement part of a `VALID` assessment. The
connector holds it for `remaining_ttl_ms`, counted from its observation
(from the fetch for a deny that carries no evidence time, such as
`SHARE_ABSENT`): 20 minutes for a deny (`allowed=false`, "critical"), 10
minutes for any other verdict (profile fields `critical_hold_ms`,
`verdict_hold_ms`). The
MDS keeps a verdict exactly as long as the connector said and never
longer. While it is in force, a source the connector cannot read, a
connector that is down or restarting, a record the MDS rejects and a
dropped batch do **not** revoke it; a newer `VALID` record replaces it at
once. So after a deny the DS stays denied until its hold runs out, and
then it is placed neutrally — losing data never makes a DS unavailable,
but it does end a deny. If a DS must stay out of service longer than the
hold, exclude it by its admin state (`mds-admin ds set-state`), which
neutral placement respects.

`verdict=` in `show` says which case a DS is in (see the table under
"Reading `show`"): `fresh` for a verdict from a collection the connector
just made, `retained` for one it repeats without new data
(`VERDICT_RETAINED`, also right after a connector restart:
`RESTORED_FROM_STATE`), `none` for neutral. With the connector unreachable
the MDS's own copy keeps its label and only `hold_left_ms` runs down.

Leaving a deny is held down: once the source is healthy again the
connector keeps publishing the deny (`RECOVERY_HOLD_DOWN`) until two
distinct source cycles agreed and 10 s passed; an allow, and the first
verdict after a start, is published at once.

**Shared-filesystem aliases.** Neutral keeps the alias grades of `fill`.
Two exports of one filesystem on one host that only the *connector's*
`capacity_domain_id` declares as one domain are undeclared once their
verdicts run out, and the gate excludes them as
`SHARED_FS_ALIAS_UNMAPPED` (an ERROR line names the pair). Declare such
aliases with `ds_capacity_domain.<id>` — the connector's domain id, so a
live verdict does not raise `DOMAIN_MAP_MISMATCH` — to keep them
placeable while they are neutral.

### Alerts for `smart`

A lost connector no longer refuses placements, so it has to be alerted on:

- `pnfs_mds_connector_reachable == 0` on any MDS (no accepted batch within
  `3 × ds_connector_poll_ms`).
- `pnfs_mds_placement_neutral_ds` above its normal value (0 while
  coverage is full): verdicts ran out or never arrived — the cluster
  places, but it no longer steers. It counts the DS that reached the
  verdict step at the last placement decision; `neutral_ds` in the
  readiness row counts every registered DS.
- Both placement gauges (`pnfs_mds_placement_neutral_ds`,
  `pnfs_mds_placement_retained_ds`) move only at a placement decision:
  on an idle cluster they keep the last decision's values. They say how
  the last files were placed, not whether the connector is alive —
  `pnfs_mds_connector_reachable` does.
- `pnfs_mds_placement_retained_ds > 0` for longer than you tolerate: the
  connector is repeating verdicts it cannot re-observe (the source is
  down).
- `pnfs_mds_connector_verdicts_expired_total` counts verdicts that ran
  out without a new one, but only when the next batch is accepted; while
  the connector is unreachable it stays flat. Alert on
  `pnfs_mds_connector_reachable` for that; the placement gauges above
  show the effect once files are placed.
- On the connector host, `connector_state_write_errors_total` (the state
  file cannot be written, so a restart would lose the verdicts).

## A DS in a subdirectory of a share

Bind the share in `export_path` and the `ds[N]` path in `ds_path` — the
stand example: `export_path: /mnt/data`, `ds_path: /mnt/data/pnfs-ds`.
The connector vetoes `DS_PATH_UNDER_NESTED_SHARE` when another share lies
in between. Both paths must be canonical — no `.`, `..` or empty (`//`)
component: the connector config reports `PATH_NOT_CANONICAL`, the MDS
rejects such a record's shape, and `mds-admin ds add` refuses to register
such a `ds[N]` path. The connector checks the path as written; it does not
resolve symlinks or mounts under the share, so keep the DS directory a
plain directory on the share's filesystem. See "Upgrading to `ds_path` bindings" above for the order to
roll this out in on a cluster that is already running.

## Reading `show`

Illustrative output for a `smart` MDS with one retained verdict and one
neutral DS (rendered by the helper from the MDS row format; it is not a
lab capture):

```
192.168.65.225: desired=smart effective=smart generation=faf2d6bbd3bf kernel=58494e01 build=connector=1 prealloc=0 wrr=1
  readiness: mode_active=1 connector_config_valid=1 connector_reachable=1 last_batch_valid=1 coverage=partial registered=2 covered=1 eligible=2 retained=1 neutral=1
  connector: config_digest=sha256:30259fc1… profiles=xinas-mvp=sha256:c9bee5b2… last=accepted 1
  ds 0   ONLINE   domain=e4e9…:/dev/xi_data cap_age=1.4s avail=41.4TB/42.2TB assess=VALID allowed=1 ppm=1000000 ttl=412.0s age=188.0s weight=6422528000000 reason=NONE verdict=retained hold_left=412.0s
  ds 1   ONLINE   domain=ds:1 cap_age=1.4s avail=33.9TB/34.6TB weight=6422528000000 reason=NONE verdict=none hold_left=-
  metrics: eligible_ds=2 rejections=none
```

`covered` is the number of DS with a verdict in force (fresh or
retained), `retained` those held by a retained verdict, `neutral` the
registered DS without one; `eligible` is the neutral DS plus the live
allows with a multiplier above 0 (DS state and capacity are not counted).
A neutral DS has no `assess=` part: it shows its `fill` weight and the
gate's `reason` (`NONE` when it is a candidate, else for example
`CAPACITY_FULL`). The raw `config show` row also carries `quality=NONE
allowed=- ppm=1000000` for it.

| `reason` / field | Meaning | Operator action |
|---|---|---|
| `NONE` | eligible, `weight` is its share | — |
| `verdict=fresh` | a verdict from a collection the connector made; `hold_left` is the time until it runs out unless a newer one replaces it | — |
| `verdict=retained` (connector code `VERDICT_RETAINED`) | the connector repeats the last verdict it observed because its source gave no new one (xiNAS agent stopped, source error, collection failed); a retained deny keeps denying until `hold_left` reaches 0 | fix the source (`lattice-ds-connector show` names the cause after `VERDICT_RETAINED`); this is the state to alert on (`pnfs_mds_placement_retained_ds`) |
| `RESTORED_FROM_STATE` (connector code, after `VERDICT_RETAINED`) | the connector restarted and read the verdict from its state file; same as `retained`, expected for the first collection cycles after a restart | none unless it persists (then the source is not answering) |
| `verdict=none` (neutral) | no verdict in force: the connector has no binding for the DS, never reported on it, the MDS has had no batch yet, the verdict ran out, or a rebind cleared it. The DS is placed like in `fill`; `hold_left` is `-` | add the binding (`lattice-ds-connector discover` for the incarnation), or restore the connector or its source if the DS should be steered; see "What neutral means" |
| `CONNECTOR_DENIED` / `ZERO_MULTIPLIER` | a verdict in force denies new allocations on this DS (fresh or retained) | the DS is unhealthy by profile; fix the DS. It is placed again when the hold runs out without a new verdict |
| `CAPACITY_UNKNOWN` / `CAPACITY_STALE` | no (fresh) `statvfs` of the back-mount on this MDS | mount the DS back-mount on the MDS; check `ds_capacity_poll_ms` |
| `CAPACITY_FULL` | `available <= placement_min_free_bytes` | expected; raise capacity or lower the threshold |
| `DOMAIN_MAP_CONTRADICTION` / `SHARED_FS_ALIAS_UNMAPPED` / `DOMAIN_MAP_MISMATCH` | the declared capacity domains disagree with what `f_fsid` proves (`DOMAIN_MAP_MISMATCH`: the operator's `ds_capacity_domain.<id>` contradicts the domain of a live verdict). `SHARED_FS_ALIAS_UNMAPPED` also appears when an alias declared only by the connector's domain becomes neutral | fix `ds_capacity_domain.<id>` (see `docs/placement-modes.md` in the fork and "What neutral means") |
| `DS_OFFLINE` | admin state | `mds-admin ds set-state` |
| `MODE_NOT_READY`, `NO_BINDING`, `ASSESSMENT_UNKNOWN`, `ASSESSMENT_STALE` | not produced by `smart` any more (a missing verdict is neutral); the names stay in the metric labels. An MDS build without verdict retention still reports them: `smart` without a usable view, no binding, the connector cannot vouch, the last record aged out | upgrade the MDS (see "Upgrading to verdict retention") |

## What the helper will not do (CLI-05)

No rolling restart, no cluster-wide atomic switch, no edits to the
connector profile or to xiNAS. A future `mds-admin placement mode set`
with a cluster-wide change generation, quiesce and prepare/commit is out
of scope for the MVP; the per-MDS health states are never synchronised —
only the configuration is.
