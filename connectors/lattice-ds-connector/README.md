# lattice-ds-connector

Per-MDS placement-assessment daemon for
[PEAK-AIO/pnfs-lattice](https://github.com/PEAK-AIO/pnfs-lattice): it polls a
storage backend's *observations* (for xiNAS, `GET /api/v1/placement/observations`),
evaluates them against a placement profile and publishes one local batch of
per-DS assessments over a Unix socket that the MDS reads before every new
allocation.

Status: **first prototype** of the MVP requirements package
(*Lattice–xiNAS Connector MVP*, 2026-09-22). P0 (contracts, fixtures,
profile) and P2 (connector runtime + xinas module) are implemented here; P1
(the xiNAS source) lives in the xiNAS repository on `release/3.15`
(`docs/control-path/s20-placement-observations-spec.md`); P3 (the MDS
allocation gate, `LAT-*`) is **not** part of this prototype.

Python 3.9+, standard library only at runtime (Oracle/RHEL 9 ships 3.9).
`jsonschema` and `pytest` are used by the tests only.

## What it publishes

`GET /v1/assessments` on `/run/lattice-ds-connector/connector.sock` returns the
batch defined by [`contracts/connector-batch.schema.json`](contracts/connector-batch.schema.json)
(Appendix Г of the package): for every configured DS a `quality`
(`VALID`/`UNKNOWN`), `placement.allowed`, `placement.multiplier_ppm`,
`reason_codes`, the generic identity (`datastore_id`, `target_id`,
`target_incarnation`), the shared capacity domain, coverage rows,
`evidence_age_ms` and `remaining_ttl_ms`. A record is a hard veto unless it
is `VALID`, `allowed: true` and its TTL is above zero — anything the runtime
cannot prove is `UNKNOWN`/`false`/`0` with a reason.

Freshness is the runtime's arithmetic (CON-10/11): the source's own evidence
age, plus the whole request duration, plus the local monotonic time since the
fetch. A GET never refreshes anything; `remaining_ttl_ms` only shrinks until
the next accepted collection. A restarted daemon starts with every DS
`UNKNOWN` (`NO_ASSESSMENT`).

Re-entry hold-down (CON-16): after start or any deny/UNKNOWN, an allow is
withheld (`RECOVERY_HOLD_DOWN`) until two *distinct* source cycles agreed
and 10 s passed; a repeated identical source snapshot is one sample.

## Layout

| Path | What |
|---|---|
| `lattice_ds_connector/config.py` | JSON config model + typed validation, config and profile digests |
| `lattice_ds_connector/modules/base.py` | the module interface (`describe/validate/collect/evaluate/close`) and the CON-06 invariants |
| `lattice_ds_connector/modules/xinas.py`, `xinas_policy.py` | the production module: bounded HTTPS client (no redirects, capped body, bearer from a 0600 file) and the xinas-mvp v1 decision policy |
| `lattice_ds_connector/modules/fixture.py` | the test-only module (`test_mode: true` required) |
| `lattice_ds_connector/runtime.py` | instances, one bounded worker each, freshness/TTL, hold-down, epochs/sequences, batch limits |
| `lattice_ds_connector/server.py` | the Unix-socket HTTP server: `/v1/assessments`, `/healthz`, `/metrics` |
| `lattice_ds_connector/cli.py` | `run`, `validate-config`, `show`, `describe` |
| `contracts/` | Appendix Г (connector batch) and Д (xiNAS observations) schemas |
| `examples/connector-config.json` | Appendix А, adapted to the xiNAS prototype's identities |
| `fixtures/xinas/` | golden xiNAS source responses (healthy, degraded, log fault, broken shapes, missing dependencies, export access, prerequisites, mount ambiguity) |
| `fixtures/fixture-module/` | a ZFS-shaped fixture for the generic module |
| `docs/profile-xinas-mvp.md` | the decision table, reason catalogue, coverage, parameters |
| `docs/troubleshooting.md` | stale collector, wrong binding, auth, source unavailable, hold-down |
| `docs/compatibility-manifest.json` | supported source schema, xiRAID and profile versions |
| `systemd/lattice-ds-connector.service` | the unit |
| `tests/` | 198 tests over the acceptance rows T-02/03/06/07/08/09/10/18/19/20/21/22/29/30/33/35 |

## Run

```bash
python3 -m lattice_ds_connector validate-config --config /etc/lattice-ds-connector/config.json
python3 -m lattice_ds_connector run --config /etc/lattice-ds-connector/config.json
python3 -m lattice_ds_connector show          # human view of the batch
python3 -m lattice_ds_connector show --json   # the raw batch
```

Configuration is the JSON of Appendix А (`examples/connector-config.json`).
For the xiNAS prototype, which has no HTTPS ingress yet, the source URL may
be `http://…` **only** with `"allow_insecure_http": true` (validate-config
warns; the viewer token then travels in clear on the management network).
The bearer token is read from `bearer_token_file` (mode 0600) on every
collect, so it rotates without a restart. `test_mode: true` is what allows a
`fixture` instance; production configs must set it to `false`.

Reload: `SIGHUP`; an invalid file is rejected and the active configuration
stays (the rejection is logged with every issue). Stop: `SIGTERM`, bounded
by 5 s.

## Tests

```bash
python3 -m pip install pytest jsonschema     # test dependencies only
python3 -m pytest tests -q
python3 scripts/gen_fixtures.py               # regenerate fixtures/xinas/*.json
```

## Decision summary (xinas-mvp v1)

Details in `docs/profile-xinas-mvp.md`. A DS is `VALID`/allowed only when,
in one consistent source snapshot: the controller id matches the binding;
the bound share is present, `SUCCESS`, at the expected export path and
incarnation; its filesystem is XFS, mounted on the expected device and
writable, with the DATA array resolved and any `logdev=`/`rtdev=` resolved to
arrays; every referenced array has a valid state shape, is `online`, proves
initialization where the level needs it, and carries no veto word; every
member state is valid and not reconstructing; the export is present with a
writable rule covering every configured client network and offering the
expected security flavors; nfsd is running with NFSv3; and the oldest
evidence is younger than 20 s. `degraded`/`need_restripe`/member `offline`
give the 250000 ppm multiplier (minimum across data/log/rt, never a
product); `sdc_scanning` is allowed at full weight with `SCAN_ACTIVE`.

## Licence

MIT (SPDX headers in every source file).
