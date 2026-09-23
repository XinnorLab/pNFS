# Troubleshooting

Start with `lattice-ds-connector show` (or `show --json`) on the MDS: every
DS prints its quality, allowed, ppm, evidence age, TTL and reasons. Then
`curl --unix-socket /run/lattice-ds-connector/connector.sock http://c/healthz`
for the runtime and `journalctl -u lattice-ds-connector` for the JSON log
lines (rate-limited per instance and code, with `suppressed` counts).

| Symptom | Reasons you will see | Cause and fix |
|---|---|---|
| Every DS of an instance `UNKNOWN` / `NO_ASSESSMENT` right after start | `NO_ASSESSMENT` | Normal until the first collect (≤ 5 s + deadline). Persisting: see the instance's `last_error` in `/healthz`. |
| DS allowed in `show`, MDS still denies | `remaining_ttl_ms: 0`, `EVIDENCE_EXPIRED` | The MDS ages the record on its own clock; a stale collector or a paused daemon lets the TTL run out. Check `last_success_age_ms` in `/healthz` and the source's `collection_period_ms`. |
| `SOURCE_STALE` while the source answers 200 | `SOURCE_STALE` (UNKNOWN) | The xiNAS agent's placement cycle is behind (its own `evidence_age_ms` is large) or the HTTP round trip is slow; the runtime adds the request duration. Check `agent.health` on xiNAS (`PlacementObservations` collector) and the network path. |
| `SOURCE_NOT_READY` | UNKNOWN, instance `FAILED` | The xiNAS api restarted and has not received a push yet (its receipt clock is empty). Clears on the agent's next 5 s cycle; persisting means the agent is down. |
| `SOURCE_AUTH_FAILED` | UNKNOWN, alert line | 401/403 from xiNAS: wrong or rotated token, or the token file is unreadable/empty. The file is read on every collect — replace the content, no restart needed. Also raised when `bearer_token_file` is missing. |
| `SOURCE_TLS_FAILED` | UNKNOWN | CA mismatch or hostname mismatch. Point `tls_ca_file` at the CA that signed the xiNAS `mcp.http.tls` certificate (its SAN must carry the address in `url`). Plain `http://` is accepted only in a `test_mode: true` file. |
| validate-config: `INCARNATION_REQUIRED` | | A xinas binding pins no `expected_target_incarnation`. Run `lattice-ds-connector discover --config <file>`: it fetches each source once and prints every share's current incarnation; copy the one you mean into the binding. A binding is never trusted on first use. |
| `INCARNATION_UNPINNED` | UNKNOWN | Same cause at runtime (a config that bypassed validation). Pin the incarnation. |
| `GRAPH_INCONSISTENT` | UNKNOWN | The source's records contradict each other for this share (`diagnostics.graph_inconsistent` says how: export path, filesystem mountpoint, array volume vs `source_device`/`logdev=`/`rtdev=`, record kind). A source bug or a mid-change snapshot; it clears on the next consistent cycle. Persisting: report the snapshot (`show --json`). |
| `EVIDENCE_AGE_MISSING` | UNKNOWN | A `SUCCESS` record arrived without `observed_at` / `evidence_age_ms`. The source is not the supported xiNAS build (see `docs/compatibility-manifest.json`). |
| `FILESYSTEM_IDENTITY_MISSING` | UNKNOWN | The filesystem has no `uuid`/`incarnation` (blkid failed on xiNAS). No capacity domain can be formed; check `agent.health` on xiNAS. |
| `XIRAID_VERSION_UNSUPPORTED` / `RAID_LEVEL_UNSUPPORTED` | UNKNOWN | The array reports an edition/version or level outside the decision table (Classic 4.4.x; levels 0/1/5/6/7/10/50/60/70/n+m). The table has not been validated for it; do not widen the policy without the vendor page for that version. |
| `COLLECT_IN_FLIGHT` | retained lease | The previous collect+evaluate helper is still running; this tick was skipped rather than stacking a second one. Repeated occurrences count against the restart budget like a timeout. |
| `SOURCE_REPLAY` in the journal (`source_replay_ignored`) | no change | The source answered with a generation not newer than the last accepted one in the same epoch (a delayed or replayed response). Ignored. Persisting means the xiNAS publisher is frozen — see `SOURCE_STALE`. |
| `SOURCE_UNAVAILABLE` / `SOURCE_TIMEOUT`, DS still allowed | retained lease | Transport failure: the last VALID decisions stay until their original expiry (≤ 20 s), then `EVIDENCE_EXPIRED`. Fix the path; nothing is refreshed until a collect succeeds. |
| `SHARE_ABSENT` (VALID deny) | | The source enumerated its shares (`COMPLETE`) and the bound `target_id` is not among them: the share was deleted, or the binding names the wrong id (xiNAS share ids are the desired Share ids, `xinasctl shares list`). |
| `INCARNATION_MISMATCH` | VALID deny | The share was recreated (new fsid) or the binding's `expected_target_incarnation` is stale. Compare with the source's `shares[].incarnation`, then rebind with a new `binding_generation`. |
| `EXPORT_PATH_MISMATCH` / `IDENTITY_MISMATCH` | VALID deny | The endpoint's `export_path` or the instance's `expected_controller_id` does not match what the source publishes. Fix the config; both are deliberate guards against binding the wrong node. |
| `EXPORT_ACCESS_MISSING` | VALID deny | No `/etc/exports` rule covers one of `expected_client_networks`. Add the MDS/client networks to the export on xiNAS. |
| `EXPORT_RULE_UNSUPPORTED` | UNKNOWN | The export carries a netgroup, hostname or wildcard-host rule; the MVP evaluates only IP/CIDR/`*` rules and such a rule may contradict them for some hosts. Rewrite the export with CIDR rules only. |
| `RECOVERY_HOLD_DOWN` after a fault cleared | VALID deny, `diagnostics.hold_down` | Expected: two distinct source cycles and 10 s must pass. `distinct_cycles: 1` for a long time means the source republishes the same snapshot (its `source_generation` is stuck). |
| `CAPABILITY_MISSING` | UNKNOWN | The source's `capabilities`/`coverage` lack a required check — an older xiNAS build. `docs/compatibility-manifest.json` lists the supported source version. |
| `WORKER_STUCK` | UNKNOWN until reload | The module's collect hung past the deadline more than `worker_restart_limit` times in an hour. Usually a black-holed source address. Fix the path, then `SIGHUP`. |
| `BATCH_TOO_LARGE` on `/v1/assessments` (HTTP 500) | | More than `max_batch_bytes` (4 MiB) of assessments. Reduce bound DS or diagnostics; the batch is refused, never truncated. |
| validate-config: `SECRET_PERMISSIONS` | | `bearer_token_file` must be a regular file with mode 0600. |
| validate-config: `FIXTURE_IN_PRODUCTION` | | A `fixture` instance in a file with `test_mode: false`. Remove it. |
| validate-config: `CHECK_NOT_IMPLEMENTED` | | A profile requires `network.path`, `network.performance` or `filesystem.integrity`; the xinas module cannot evaluate them (T-30). |

Nothing here changes a DS's admin state on the MDS; the connector only
publishes assessments (LAT-03). Disabling the feature on the MDS restores
the previous placement semantics and must be visible in its admin status.
