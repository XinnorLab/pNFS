# Explicit DS path in the connector endpoint (design)

Date: 2026-09-26. Status: approved design, not implemented.
Amends: `2026-09-23-placement-modes-design.md` §7 (layer 2, the endpoint
rule), `docs/placement-modes/contract-manifest.json` `endpoint_rule`.
Source: implementation review of pNFS `9a37177` / pnfs-lattice `2f1b52c`,
finding P2 "endpoint binding in the MDS accepts a parent export".

## 1. Problem

The MDS registers a data server as `ds[N] = host:/path` and mounts that
path. On the stand the path is a directory inside a xiNAS share:
`ds[0] = 192.168.64.51:/mnt/data/pnfs-ds`, share `/mnt/data`. The
connector reports the **share** path in `endpoint.export_path`, because
it checks that path against the xiNAS observation.

To make the two meet, `ds_connector_endpoint_matches()` accepts an
`endpoint.export_path` that is the registered path or any component-wise
ancestor of it, `/` included (`src/mds/ds_connector.c:378-412`,
`test_endpoint_rule`; `contract-manifest.json` `endpoint_rule`: "or lies
under it"). Consequences:

- with several shares on one xiNAS, a parent match does not prove the
  assessment is about the share the DS actually lives in: DS
  `/mnt/data/sub/pnfs-ds` matches a binding for `/mnt/data` even when
  `/mnt/data/sub` is its own share (another filesystem, other arrays);
- a binding for `/` matches every DS on the host;
- the live spec says "exact strings" while the manifest and the code
  say "or lies under it".

The MDS sees one record and cannot tell which share contains the DS
path. The connector sees every share of the xiNAS it observes.

## 2. Decisions

| Question | Decision |
|---|---|
| How the DS path is tied to a share | explicitly: the binding names the DS path (`ds_path`) next to the share (`export_path`) |
| MDS rule | `ds_path` present → it must equal the registered path exactly; `export_path` equals it or is a component-wise ancestor; absent → `export_path` must equal the registered path exactly. No open-ended parent match |
| `/` as a parent | forbidden: `export_path = /` only with `ds_path = /` (or no `ds_path`) |
| Who checks "no other share in between" | the connector (xiNAS policy), which sees every share |
| Contract version | stays 1.x: `endpoint.ds_path` is an optional, additive field |
| Transition mode | none (the MVP is not released); upgrade order in the runbook |

## 3. Contract

`connector-batch.schema.json`, `assessments[].endpoint`:

```json
"ds_path": { "type": "string", "pattern": "^/" }
```

Optional. The connector emits it only when the binding sets it. Paths
are compared after dropping trailing `/` (`/` stays `/`).

## 4. MDS (`src/mds/ds_connector.c`, pnfs-lattice)

`struct rec` gains `char ds_path[MDS_DS_EXPORT_MAX]` (`""` when absent).
`parse_record` reads `endpoint.ds_path` when present: a non-string, an
empty string, a value not starting with `/` or longer than the buffer
is a shape rejection (`endpoint.ds_path invalid`).

`ds_connector_endpoint_matches(ds, server, export_path, ds_path, port)`
(new `ds_path` parameter, `NULL` or `""` = absent):

1. `server == ds->host`; `port` equals `ds->tcp_port` when both are set
   (unchanged).
2. Normalize `export_path` and `ds_path` (drop trailing `/`, keep `/`).
   Both must start with `/`.
3. `ds_path` absent: `export_path == ds->export_path`.
4. `ds_path` present: `ds_path == ds->export_path`, and
   `export_path == ds_path` or `export_path` is a component-wise
   ancestor of `ds_path` (`ds_path` starts with `export_path` followed
   by `/`); `export_path == "/"` is accepted only when `ds_path == "/"`.

A mismatch stays `rejected_binding`; the detail names the endpoint and
the registry path as today, plus `ds_path` when present.

Tests (`tests/unit/test_ds_connector.c`, `test_endpoint_rule` rewritten):
exact match without `ds_path`; trailing `/` on either side; a parent
without `ds_path` is rejected (was accepted); `ds_path` equal to the
registry path under the share is accepted; `ds_path` equal to the share
is accepted; `ds_path` that differs from the registry path is rejected;
`export_path` that is not an ancestor of `ds_path` is rejected
(`/mnt/dat` vs `/mnt/data/pnfs-ds`, a sibling share); `/` as a parent is
rejected, `/` with `ds_path = /` is accepted; wrong host / port still
rejected. A batch-level test: a record with `endpoint.ds_path` equal to
the registry path is accepted end to end; an invalid `ds_path` is
`rejected_shape`.

## 5. Connector (`connectors/lattice-ds-connector`, pNFS)

**Config** (`config.py`, `bindings[].endpoint`):

- `ds_path`: optional absolute path; trailing `/` dropped.
- `DS_PATH_OUTSIDE_EXPORT` when `ds_path` is neither `export_path` nor
  under it (component-wise, `path_contains`).
- `ROOT_EXPORT_PARENT` when `export_path` is `/` and `ds_path` is set
  and not `/`.
- `DUPLICATE_DS_PATH` when two bindings resolve to the same
  `(server, ds_path or export_path)`. Bindings that both set `alias: true`
  (CON-18 capacity aliases) may share a DS path: the MDS registry is per
  `ds_id` and accepts two `ds[N]` entries with the same `host:path`.

`Endpoint` gets `ds_path: Optional[str] = None`; `as_dict()` adds
`"ds_path"` only when it is set, so the record's endpoint carries it.

**xiNAS policy** (`modules/xinas_policy.py`, `assess_binding`): when the
binding has a `ds_path` strictly under `export_path`, look at every
observed share (`shares`) and every present `EXPORT` resource of the
same result. If any has an export path `P` with
`export_path ⊊ P ⊆ ds_path` (component-wise), veto
`DS_PATH_UNDER_NESTED_SHARE` and put `P` in the diagnostics
(`nested_share_path`). New reason code in `contract.py` with its
description, and in the profile doc's reason table.

**Preflight**: each DS row shows `ds_path` (`-` when absent).

## 6. Documents and manifests

- `contract-manifest.json` (both copies, byte-identical): `endpoint_rule`
  rewritten to §4; version 1.2.
- Live spec §7 layer 2: the endpoint sentence rewritten to §4 (the
  "exact strings" wording now matches the code).
- `docs/placement-modes/operations.md`: how to bind a DS that lives in a
  subdirectory of a share, and the upgrade order — add `ds_path` to the
  connector config first (an MDS without this change still accepts it
  through its parent rule), then upgrade the MDS.
- `connectors/lattice-ds-connector/examples/connector-config.lab-xinas-box.json`:
  DS 0 gets `"ds_path": "/mnt/data/pnfs-ds"`.
- Fork `docs/placement-modes.md` and `docs/config-keys.md`: the endpoint
  rule.
- `docs/TODO.md`: nothing deferred.

## 7. Delivery

1. pnfs-lattice: branch `xinnor/endpoint-ds-path` from
   `xinnor/placement-modes`, PR into it; the fork CI is the C gate.
2. pNFS: branch `fix/endpoint-ds-path` (from `fix/profile-map` until
   XinnorLab/pNFS#1 merges, then rebased onto `main`); after the fork PR
   merges, `scripts/export-patches.sh` and `check-manifests.py --fork`;
   then the PR into `main`.
