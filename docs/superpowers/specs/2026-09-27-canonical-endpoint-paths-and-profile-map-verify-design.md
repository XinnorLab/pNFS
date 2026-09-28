# Canonical endpoint paths; an unreadable profile map fails verify (design)

Date: 2026-09-27. Status: approved design, not implemented.
Amends: `2026-09-26-endpoint-ds-path-design.md` §4–§5,
`2026-09-25-per-profile-digest-design.md` (verify),
`docs/placement-modes/contract-manifest.json` `endpoint_rule`,
`contracts/connector-batch.schema.json` (`endpoint.export_path`,
`endpoint.ds_path`).
Source: re-review of pNFS `0b581c1` / pnfs-lattice `639b6c5` (2026-09-27),
findings P1 "`ds_path` can leave the assessed share" and P2 "CLI `verify`
accepts a corrupt profile map".

## 1. Problem

**Paths.** The connector and the MDS compare endpoint paths as strings:
they drop a trailing `/` and test a component-wise prefix. Neither
rejects `.` or `..` components or repeated separators. A binding with

```text
export_path = /mnt/data/training-a
ds_path     = /mnt/data/training-a/../../data2/training-c/pnfs-ds
```

passes the config (`DS_PATH_OUTSIDE_EXPORT` does not fire), the xiNAS
policy assesses the healthy `training-a` filesystem (`VALID`, allowed,
`NORMAL`) although the path resolves into `training-c` on another array,
and the MDS matches it against a registry entry registered with the same
string. Both sides agree on a wrong string, so the MDS check adds nothing.

**Verify.** `state_from_show` turns an unparsable
`placement_connector_profiles` into `{"<unparsable>": raw}` and keeps
`ok=True`; `verdict` only compares maps across smart MDS. One MDS, or two
with the same corrupt value, pass with exit 0. A smart MDS whose
`config show` has no `placement_connector_profiles` row at all also
passes.

## 2. Decisions

| Question | Decision |
|---|---|
| What is a canonical endpoint path | absolute; `/` alone, or `/`-separated non-empty components, none of them `.` or `..`; one trailing `/` is tolerated and dropped as today |
| Where it is enforced | connector config (`export_path` and `ds_path`), batch schema, MDS record parser, MDS endpoint match, `mds-admin ds add` / `--export-path` |
| How it fails | fail-closed: config error `PATH_NOT_CANONICAL`; MDS record shape rejection (`endpoint.export_path invalid` / `endpoint.ds_path invalid`); `ds_connector_endpoint_matches` returns false; `mds-admin` refuses to register |
| Symlinks and nested mounts | out of scope here. A canonical string is necessary, not sufficient: physical ownership of the DS directory needs xiNAS to report the resolved path and its filesystem identity on the node (follow-up, §5) |
| Verify: unparsable map | error `CONNECTOR_PROFILES_INVALID:<host>: <raw>`, exit `EXIT_DIFFER`, in every mode where the row is present |
| Verify: missing map | smart MDS with a `placement_readiness` row but no `placement_connector_profiles` row → error `CONNECTOR_PROFILES_MISSING:<host>`. `-` stays "no pins yet" and is not an error by itself |
| Contract version | stays 1.x: the schema only rejects paths that never named a distinct directory |

## 3. MDS (pnfs-lattice)

A shared helper `bool ds_connector_path_is_canonical(const char *p)`
(next to the existing path helpers in `ds_connector.c`, exported in
`ds_connector.h`; `mds-admin` links `pnfs_mds_core`, which holds it): true for `/`, and for `/c1/.../cn` with an
optional single trailing `/`, where every `ci` is non-empty and is not
`.` or `..`.

- `parse_record`: `endpoint.export_path` and `endpoint.ds_path` (when
  present) must be canonical, else the record is a shape rejection.
- `ds_connector_endpoint_matches`: returns false when `export_path`,
  `ds_path` (when present) or the registry path is not canonical, before
  any comparison.
- `mds-admin ds add host:/path` and `--export-path`: a non-canonical path
  is an error (`Error: export path must be canonical (no '.', '..' or
  empty components)`), exit 1.

Tests (`tests/unit/`): the helper table (`/`, `/a`, `/a/`, `/a/b`,
accepted; `""`, `a`, `//`, `/a//b`, `/a/./b`, `/a/../b`, `/..`, `/.`,
`/a/..`, `/a//` rejected); `endpoint_matches` false for a `..` ds_path
equal to the registry string; a batch with a `..` ds_path is rejected
end to end.

## 4. Connector and CLI (pNFS)

- `paths.py`: `is_canonical(path) -> bool`, same rule as §3.
- `config.py` `_parse_endpoint`: `PATH_NOT_CANONICAL` on
  `<path>.export_path` / `<path>.ds_path` for an absolute but
  non-canonical value; containment checks run only on canonical paths.
- `connector-batch.schema.json`: both fields get the pattern
  `^/$|^(/(?!\.\.?(/|$))[^/]+)+/?$`.
- `contract-manifest.json` `endpoint_rule`: append "every path is
  canonical: no empty, '.' or '..' component".
- `lattice-placement` `live.py`: `MdsState.profiles_error: Optional[str]`
  holds the raw value when `parse_pins` fails (`profiles` stays `None`);
  `profiles_row: bool` records whether the row was present.
- `verify.py` `verdict`: the two errors of §2 per MDS; they make the exit
  code `EXIT_DIFFER`. `render_show` prints `profiles=INVALID(<raw>)`.

Tests: config rejects `..`, `.`, `//` in either path; the reported
`training-c` case is rejected; policy tests unchanged. CLI: one MDS
with a corrupt map, two MDS with the same corrupt map, a smart MDS
without the row → exit `EXIT_DIFFER` with the named error; `-` still
passes.

## 5. Not in this change

- Resolving symlinks / nested mounts and proving the DS directory's
  filesystem identity on the xiNAS node (xiNAS must observe it; until then
  a canonical `ds_path` is trusted lexically).
- The other re-review findings (LAT-25, TTL, `config show` size).
