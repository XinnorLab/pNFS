# Per-profile digest pins for `smart` (design)

Date: 2026-09-25. Status: approved design, not implemented.
Amends: `2026-09-23-placement-modes-design.md` §4, §7, §10, §11, §13, §15.
Source: implementation review of pNFS `9a37177` / pnfs-lattice `2f1b52c`,
finding P1 "a DS-profile contract that contradicts several connector
instances".

## 1. Problem

The connector config accepts several profiles, and each instance names
its own (`config.py`, `profiles[]` + `instances[].profile`). Every
assessment carries the digest of its instance's profile
(`connector-batch.schema.json`, `assessments[].profile`). Three places
still require one digest for the whole batch:

- connector `preflight` reports `PROFILE_DIGESTS:<n>` when there is more
  than one;
- the MDS client (`src/mds/ds_connector.c`) takes the first record's
  digest and rejects every later record with another one
  (`rejected_binding`, "profile digest differs inside the batch");
- `lattice-placement verify` and `config show` carry a single
  `placement_connector_profile_digest`.

A valid connector config with two xiNAS instances on different profiles,
or a xiNAS and a future ZFS module, therefore leaves the DS of the second
profile unassessed in `smart`, and which DS lose depends only on record
order. `config_digest` already covers every profile of the connector
config; what the profile pin adds is an operator-visible lock on the
placement policy itself, and that lock has to be per profile.

## 2. Decisions

| Question | Decision |
|---|---|
| What the MDS pins | a map `profile.id → digest`, not one digest and not a set of digests (a set would let profile A pass with profile B's digest) |
| The old key `ds_connector_expected_profile_digest` | removed; present in a config it is an error naming the replacement. Nothing outside the stand used it |
| The same `profile.id` with two digests in one batch | the whole batch is dropped — a healthy connector cannot produce it (profile ids are unique in its config, the batch is one snapshot) |
| The digest in the per-DS binding tuple | not part of it (as the code already does): a connector profile reload is not a rebind |

## 3. Contract

### 3.1 Profile id

`profile.id` matches `^[A-Za-z0-9._-]{1,63}$` — in the connector config
(`PROFILE_ID_INVALID`), in `connector-batch.schema.json`
(`pattern`, `maxLength: 63`) and in the MDS record parser (a record with
another id is `rejected_shape`). The id is used verbatim as a key in the
MDS config and in `config show`, so it may not contain `=`, `,` or
whitespace.

At most 8 profiles per connector config (`LIMIT`) and per MDS pin map
(`PM_MAX_PROFILES = 8`).

### 3.2 MDS key

```
ds_connector_expected_profiles = <id>=<digest>[,<id>=<digest>...]
```

- `smart` only, optional, default unset. Whitespace around items is
  ignored. `<digest>` is a non-empty string ≤ `PM_DIGEST_MAX - 1` bytes.
- Config errors: a malformed item, an invalid id, a duplicate id, more
  than 8 items, an empty value.
- `ds_connector_expected_profile_digest` (any value) is a config error:
  "replaced by ds_connector_expected_profiles = <id>=<digest>".

### 3.3 Per-record rule (replaces "identical across every record")

After the existing per-assessment binding checks (§7, layer 2 of the placement-modes design):

- pins set, `profile.id` not in the map → the record is rejected
  (`rejected_binding`, detail `ds <n>: profile <id> not pinned`);
- pins set, digest ≠ the pin for its id → rejected
  (`rejected_binding`, detail `ds <n>: profile <id> digest <d> != pinned`);
- pins unset → any profile is accepted.

### 3.4 Batch consistency

While walking the records the client builds the batch's
`id → digest` map. A second digest for an id already in the map drops
the whole batch (`DC_PROFILE_INCONSISTENT`,
`pnfs_mds_connector_batches_dropped_total{reason="profile_inconsistent"}`,
detail `profile <id>: digests <a> and <b> in one batch`); no DS of that
batch is accepted, as for an envelope failure. More than 8 distinct ids
in a batch drops it too (`reason="profile_limit"`).

The map is built from every syntactically valid record, including
records later rejected for binding reasons, so a pin mismatch on one DS
cannot hide an inconsistency on another.

### 3.5 Readiness and `config show`

The assessment view carries the map of the last accepted batch, sorted by
id. `config show` prints

```
placement_connector_profiles = xinas-mvp=sha256:…,zfs-mvp=sha256:…
```

(`-` when no batch has been accepted) instead of
`placement_connector_profile_digest`. Bounded: 8 × (63 + 1 +
`PM_DIGEST_MAX`) + separators.

## 4. Connector (`connectors/lattice-ds-connector`)

- `config.py`: `PROFILE_ID_INVALID`; `LIMIT` above 8 profiles.
- `contracts/connector-batch.schema.json`: `profile.id` gets the
  pattern and `maxLength`.
- `preflight.py`: `PROFILE_DIGESTS:<n>` is removed;
  `PROFILE_INCONSISTENT:<id>` when one id carries two digests; the report
  has `profiles: {id: digest}` instead of `profile_digest`, and the text
  output prints the map. `--expect-profiles id=digest,...` compares the
  map with the pins the MDS will use (`PROFILE_PIN_MISMATCH:<id>` for a
  differing digest, `PROFILE_NOT_PINNED:<id>` for an id missing from the
  pins while pins are given).
- The runtime is unchanged: each record already carries its instance's
  profile.

## 5. CLI (`tools/lattice-placement`)

- `live.py`: parses `placement_connector_profiles` into
  `Dict[str, str]` (`profiles`), `None` when the row is absent or `-`.
- `verify`: `CONNECTOR_PROFILE_DIGEST_MISMATCH` becomes
  `CONNECTOR_PROFILES_MISMATCH` — the maps must be equal across the MDS
  running `smart`.
- `validate`: the old key is `LEGACY_KEY:ds_connector_expected_profile_digest`
  with the replacement line; the new key is checked for syntax and the
  limit; when the connector preflight runs, the pins are compared with
  the connector's map (same codes as `--expect-profiles`).
- `show`: prints the map.
- `data/contract-manifest.json` (and `docs/placement-modes/contract-manifest.json`):
  the new key; the old key listed under `removed_keys` with its
  replacement; the `config show` row renamed; `profile_digest_rule`
  rewritten to §3.3–§3.4. The two manifest copies stay byte-identical;
  `removed_keys` is a new top-level list (`{key, replaced_by}`).

## 6. Documents

- `2026-09-23-placement-modes-design.md`: §4 key table; §7 layer 2 binding
  rule (§3.3–§3.4 here) and the binding tuple without `profile.digest`;
  §10 verify comparison set; §11 preflight; §13 test rows; §15 a
  decision row pointing here.
- `docs/placement-modes/operations.md`, `examples/mds.conf.smart`: the
  new key.
- `docs/TODO.md`: nothing deferred by this change.

## 7. Tests

pnfs-lattice (cmocka, run by the fork CI — no local CMake here):

- `test_placement_config`: the new key parsed; malformed item, invalid
  id, duplicate id, 9 items, empty value, the old key → errors.
- `test_ds_connector`: two profiles, no pins → both accepted; pins for
  both → both accepted; an unpinned id → only that record rejected; a
  wrong digest for a pinned id → only that record rejected; one id with
  two digests → the batch dropped with `DC_PROFILE_INCONSISTENT`, even
  when one of them fails a binding check; 9 ids → dropped; a reload
  (new digest, same tuple) accepted without pins and rejected with the
  old pin; an invalid id → `rejected_shape`; the view's map sorted.
  `test_profile_digest_pin_and_consistency` is replaced.

pNFS (pytest):

- connector: `PROFILE_ID_INVALID`, the 8-profile limit, the schema
  pattern; preflight without `PROFILE_DIGESTS`, with
  `PROFILE_INCONSISTENT`, `--expect-profiles` both codes; two xiNAS
  instances on two profiles in one batch are `ready`.
- CLI: parsing the new row; `CONNECTOR_PROFILES_MISMATCH`; `LEGACY_KEY`;
  fixtures `tests/fixtures/config-show-*.json` updated.
- manifest/patch consistency check after `scripts/export-patches.sh`.

## 8. Delivery

1. pnfs-lattice: branch `xinnor/profile-map` from `xinnor/placement-modes`,
   PR into `xinnor/placement-modes`; the fork CI is the C gate.
2. pNFS: branch `fix/profile-map` from `main` with this document, the
   spec amendments, the connector, the CLI and the manifests. After the
   fork PR merges, `scripts/export-patches.sh` regenerates
   `mds/patches/` and `mds/manifest.json` from the merged SHA; then the
   PR into `main`.
