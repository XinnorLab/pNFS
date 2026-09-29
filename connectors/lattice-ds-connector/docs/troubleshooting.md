# Troubleshooting

Start with `lattice-ds-connector show` (or `show --json`) on the MDS: every
DS prints its quality, allowed, ppm, evidence age, TTL and reasons. Then
`curl --unix-socket /run/lattice-ds-connector/connector.sock http://c/healthz`
for the runtime and `journalctl -u lattice-ds-connector` for the JSON log
lines (rate-limited per instance and code, with `suppressed` counts).

A verdict holds (contract 1.1): once the connector has published a `VALID`
record for a DS, only a newer `VALID` record replaces it. Where a row below says
`UNKNOWN` and the DS has a verdict in force, the record you see is that
verdict, still `VALID`, with `VERDICT_RETAINED` first and the row's code
second, until its hold runs out (`critical_hold_ms`, 20 min, for
`allowed: false`; `verdict_hold_ms`, 10 min, otherwise; counted from the
observation). The instance's `snapshot_status` is `FAILED` after a
collection error or when the source's own snapshot is `FAILED` (no new data
either way); a `VALID` record in a `FAILED` snapshot is always a retained
one.

| Symptom | Reasons you will see | Cause and fix |
|---|---|---|
| Every DS of an instance `UNKNOWN` / `NO_ASSESSMENT` right after start | `NO_ASSESSMENT` | Normal until the first collect (≤ 5 s + deadline) for a DS without a verdict restored from the state file. Persisting: see the instance's `last_error` in `/healthz`. Every DS `UNKNOWN` after a restart although verdicts were in force: see `state_file_ignored` / `state_write_failed` below. |
| `RESTORED_FROM_STATE` right after start | VALID, `VERDICT_RETAINED` first, `RESTORED_FROM_STATE` second, `NO_ASSESSMENT` third | Expected after a restart: the verdict in force before it, read from the state file (`runtime.state_path`), republished until the first collect replaces it or its hold runs out. Its age and TTL count from the original observation by the wall clock. A restored deny is in force: the first healthy cycles after the restart are `RECOVERY_HOLD_DOWN`. |
| journal `state_file_ignored` with `STATE_FILE_UNREADABLE` | every DS `NO_ASSESSMENT` after the restart | The state file could not be read or parsed, or has another version (a downgrade). It was renamed to `verdicts.json.corrupt-<ts>` next to it (kept for inspection; delete it when done) and the connector started with an empty store. Nothing to fix unless it repeats: then check the disk and that nothing else writes the file. |
| journal `state_restored` with `skipped` counts | those DS `NO_ASSESSMENT` | Entries the state file had but the start did not take: `UNBOUND` (the binding was removed or rebound), `PROFILE_MISMATCH` / `DATASTORE_MISMATCH` (the instance now uses another profile id or names another data store), `EXPIRED` (the hold ran out while the connector was down), anything else (a malformed entry). By design. |
| journal `state_write_failed`, metric `connector_state_write_errors_total` | no change | The state file could not be written (full disk, read-only or missing `/var/lib/lattice-ds-connector`: the unit's `StateDirectory=` and `ReadWritePaths=` create and open it). The connector keeps running on its in-memory store and retries at the next change; only a restart during the failure loses the verdicts. |
| `VERDICT_RETAINED` | VALID, the cause second | The source is not answering; the last verdict holds for its hold time — look at the second reason code (the collection error or the `UNKNOWN` record's first reason) and fix that. `remaining_ttl_ms` is the hold left; `evidence_age_ms` keeps growing from the original observation, a re-published verdict is never refreshed. |
| `EVIDENCE_EXPIRED` | UNKNOWN, `remaining_ttl_ms: 0` | The hold ran out; the MDS now places this data store neutrally (multiplier 1 000 000, the operator's capacity domain). The source has given no new `VALID` record for the whole hold: check `last_error` and `last_success_age_ms` in `/healthz` and the second reason code of the retained record before it expired. |
| `SOURCE_STALE` while the source answers 200 | `SOURCE_STALE` (UNKNOWN) | The xiNAS agent's placement cycle is behind (its own `evidence_age_ms` is large) or the HTTP round trip is slow; the runtime adds the request duration. Check `agent.health` on xiNAS (`PlacementObservations` collector) and the network path. |
| `SOURCE_NOT_READY` | retained verdict or UNKNOWN, instance `FAILED` | The xiNAS api restarted and has not received a push yet (its receipt clock is empty). Clears on the agent's next 5 s cycle; persisting means the agent is down. |
| `SOURCE_AUTH_FAILED` | retained verdict or UNKNOWN, alert line | 401/403 from xiNAS: wrong or rotated token, or the token file is unreadable/empty. The file is read on every collect — replace the content, no restart needed. Also raised when `bearer_token_file` is missing. |
| `SOURCE_TLS_FAILED` | retained verdict or UNKNOWN | CA mismatch or hostname mismatch. Point `tls_ca_file` at the CA that signed the xiNAS `mcp.http.tls` certificate (its SAN must carry the address in `url`). Plain `http://` is accepted only in a `test_mode: true` file. |
| validate-config: `INCARNATION_REQUIRED` | | A xinas binding pins no `expected_target_incarnation`. Run `lattice-ds-connector discover --config <file>`: it fetches each source once and prints every share's current incarnation; copy the one you mean into the binding. A binding is never trusted on first use. |
| `INCARNATION_UNPINNED` | UNKNOWN | Same cause at runtime (a config that bypassed validation). Pin the incarnation. |
| `GRAPH_INCONSISTENT` | UNKNOWN | The source's records contradict each other for this share (`diagnostics.graph_inconsistent` says how: export path, filesystem mountpoint, array volume vs `source_device`/`logdev=`/`rtdev=`, record kind). A source bug or a mid-change snapshot; it clears on the next consistent cycle. Persisting: report the snapshot (`show --json`). |
| `EVIDENCE_AGE_MISSING` | UNKNOWN | A `SUCCESS` record arrived without `observed_at` / `evidence_age_ms`. The source is not the supported xiNAS build (see `docs/compatibility-manifest.json`). |
| `FILESYSTEM_IDENTITY_MISSING` | UNKNOWN | The filesystem has no `uuid`/`incarnation` (blkid failed on xiNAS). No capacity domain can be formed; check `agent.health` on xiNAS. |
| `XIRAID_VERSION_UNSUPPORTED` / `RAID_LEVEL_UNSUPPORTED` | UNKNOWN | The array reports an edition/version or level outside the decision table (Classic 4.4.x; levels 0/1/5/6/7/10/50/60/70/n+m). The table has not been validated for it; do not widen the policy without the vendor page for that version. |
| `COLLECT_IN_FLIGHT` | retained verdict | The previous collect+evaluate helper is still running; this tick was skipped rather than stacking a second one. Repeated occurrences count against the restart budget like a timeout. |
| `SOURCE_REPLAY` in the journal (`source_replay_ignored`) | no change | The source answered with a generation not newer than the last accepted one in the same epoch (a delayed or replayed response). Ignored. Persisting means the xiNAS publisher is frozen — see `SOURCE_STALE`. |
| `SOURCE_UNAVAILABLE` / `SOURCE_TIMEOUT`, DS still allowed | retained verdict | Transport failure: the verdicts in force stay until their hold runs out (10 min for an allow, 20 min for a deny, from the observation), then `EVIDENCE_EXPIRED`. Fix the path; nothing is refreshed until a collect succeeds. |
| `SHARE_ABSENT` | VALID deny, held as critical (20 min from the fetch: the record carries no evidence age) | The source enumerated its shares (`COMPLETE`) and the bound `target_id` is not among them: the share was deleted, or the binding names the wrong id (xiNAS share ids are the desired Share ids, `xinasctl shares list`). |
| `INCARNATION_MISMATCH` | VALID deny | The share was recreated (new fsid) or the binding's `expected_target_incarnation` is stale. Compare with the source's `shares[].incarnation`, then rebind with a new `binding_generation`. |
| `EXPORT_PATH_MISMATCH` / `IDENTITY_MISMATCH` | VALID deny, held as critical (`IDENTITY_MISMATCH` carries no evidence age: 20 min from the fetch) | The endpoint's `export_path` or the instance's `expected_controller_id` does not match what the source publishes. Fix the config; both are deliberate guards against binding the wrong node. |
| `EXPORT_ACCESS_MISSING` | VALID deny | No `/etc/exports` rule covers one of `expected_client_networks`. Add the MDS/client networks to the export on xiNAS. |
| `EXPORT_RULE_UNSUPPORTED` | UNKNOWN | The export carries a netgroup, hostname or wildcard-host rule; the MVP evaluates only IP/CIDR/`*` rules and such a rule may contradict them for some hosts. Rewrite the export with CIDR rules only. |
| MDS: `rejected_binding`, detail "… does not match the registry …" after upgrading the MDS | | The binding lacks `ds_path` (or it differs from the registered `ds[N]` path). Add `ds_path` to the binding equal to the `ds[N]` path; see `docs/placement-modes/operations.md` "Upgrading to `ds_path` bindings". |
| `RECOVERY_HOLD_DOWN` after a fault cleared | VALID deny, `diagnostics.hold_down` | Expected, and only after a deny: two distinct source cycles and 10 s must pass, and the deny keeps counting its hold from the last critical observation. A connector that has just started without a deny restored from its state file, or that had no deny in force, publishes its first verdict at once. `distinct_cycles: 1` for a long time means the source republishes the same snapshot (its `source_generation` is stuck). |
| config: `PATH_NOT_CANONICAL` on `endpoint.export_path` / `endpoint.ds_path` | refused at load | The path has a `.`, `..` or empty (`//`) component. Write the plain absolute path; `..` would let a binding name a directory outside the share the policy assesses. |
| `DS_PATH_UNDER_NESTED_SHARE` | VALID deny | Another observed share or export lies between the bound share (`export_path`) and the binding's `ds_path`. Bind the inner share instead, or move the DS directory out from under it. |
| `CAPABILITY_MISSING` | UNKNOWN | The source's `capabilities`/`coverage` lack a required check — an older xiNAS build. `docs/compatibility-manifest.json` lists the supported source version. |
| `WORKER_STUCK` | retained verdict until its hold runs out, then UNKNOWN, until reload | The module's collect hung past the deadline more than `worker_restart_limit` times in an hour. Usually a black-holed source address. Fix the path, then `SIGHUP`. |
| `BATCH_TOO_LARGE` on `/v1/assessments` (HTTP 500) | | More than `max_batch_bytes` (4 MiB) of assessments. Reduce bound DS or diagnostics; the batch is refused, never truncated. |
| validate-config: `SECRET_PERMISSIONS` | | `bearer_token_file` must be a regular file with mode 0600. |
| validate-config: `FIXTURE_IN_PRODUCTION` | | A `fixture` instance in a file with `test_mode: false`. Remove it. |
| validate-config: `CHECK_NOT_IMPLEMENTED` | | A profile requires `network.path`, `network.performance` or `filesystem.integrity`; the xinas module cannot evaluate them (T-30). |

Nothing here changes a DS's admin state on the MDS; the connector only
publishes assessments (LAT-03). Disabling the feature on the MDS restores
the previous placement semantics and must be visible in its admin status.
