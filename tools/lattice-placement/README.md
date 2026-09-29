# lattice-placement

Operator helper for the pnfs-lattice placement modes (`legacy`, `rr`,
`fill`, `smart`): `mode show | validate | set | verify`. Python 3.9,
standard library only. Design: `docs/superpowers/specs/2026-09-23-placement-modes-design.md`
§10; procedure: `docs/placement-modes/operations.md`.

```bash
pip install ./tools/lattice-placement          # or:
PYTHONPATH=tools/lattice-placement python3 -m lattice_placement --help
```

| Command | What it does | Touches |
|---|---|---|
| `mode validate <mode> --config FILE [--connector-socket S --expect-ds 0,1]` | read-only preflight with the MDS rules (same error codes); `smart` folds in `lattice-ds-connector preflight` as warnings (a connector that is not ready does not make the file NOT_READY) | nothing |
| `mode set <mode> --config FILE [--set k=v]… [--apply]` | dry-run diff; `--apply` rewrites the one local file inside a managed block with backup, atomic replace, audit line, re-validation | that file, the audit log |
| `mode show --mds h1,h2 [--mds-admin P --mds-port 50051 --env K=V] [--config FILE | --ssh USER]` | live desired/effective mode, generation, build, readiness (`retained=… neutral=…`), per-DS rows (`verdict=… hold_left=…`: fresh, retained or none) from `mds-admin config show --json` + `/metrics`; warns when MDS differ | nothing |
| `mode verify --mds h1,h2 … [--require-full-coverage]` | exit 0 when every MDS agrees on mode, generation, build and connector pins and no error is reported; warnings do not fail it (a smart cluster without steering is one) | nothing |

Exit codes: 0 ready / same; 1 not ready / a difference / refused; 2 an
argument, file or MDS could not be read. `--json` on every command.

`mode verify` in `smart`. A data store without a verdict in force is placed
neutrally (multiplier 1 on its domain's base weight), so a cluster whose
connector is gone still places: the helper reports that it is not steering,
and fails only when you ask for steering. Two exceptions fail on their own:
an MDS that predates verdict retention (its readiness row has no
`retained_ds`/`neutral_ds`: it refuses a data store without a verdict), and
an MDS that admits no data store at all.

| Finding | Default | With `--require-full-coverage` |
|---|---|---|
| `MDS_UNREADABLE` | exit 2 | exit 2 |
| `MODE_MISMATCH`, `GENERATION_MISMATCH`, `BUILD_MISMATCH`, `CONNECTOR_CONFIG_DIGEST_MISMATCH`, `CONNECTOR_PROFILES_MISMATCH` (MDS differ); `DESIRED_NE_EFFECTIVE`; `CONNECTOR_CONFIG_INVALID`; `CONNECTOR_PROFILES_INVALID` / `_MISSING`; `NO_READINESS` | error, exit 1 | error, exit 1 |
| `NO_ELIGIBLE_DS:<host>` (`registered_ds > 0`, `eligible_ds = 0`: the MDS admits no DS, any build) | error, exit 1 | error, exit 1 |
| `CONNECTOR_UNREACHABLE:<host>` (`connector_reachable=0`) | warning; error against an MDS that predates retention | warning; error against an MDS that predates retention |
| `STEERING_OFF:<host>` (`coverage=none`: no data store has a verdict in force) | warning | error, exit 1 |
| `COVERAGE_NONE:<host>` (`coverage=none` on an MDS that predates retention: it refuses every new file) | error, exit 1 | error, exit 1 |
| `COVERAGE_PARTIAL:<host>` (some data stores are neutral; they are named with the gate's reason) | warning | error, exit 1 |
| `COVERAGE_RETAINED:<host>` (a data store is held only by a retained verdict: the connector is not observing it) | not reported | error, exit 1 |

The helper never restarts `pnfs-mds`, never edits the connector or xiNAS,
and never infers "smart is active" from a file: `show`/`verify` ask the
live daemons; the desired mode comes from `--config` or `--ssh`, else it
is printed as `?`.

Tests: `python -m pytest -q` (fixtures under `tests/fixtures/` are real
`config show` texts captured on the lab, except `config-show-smart-retention.json`
and `config-show-smart-steering-off.json`: those carry the verdict-retention
rows exactly as the MDS renders them, built from the fork's test output until
the lab runs that MDS).
