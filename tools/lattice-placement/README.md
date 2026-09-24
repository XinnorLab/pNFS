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
| `mode validate <mode> --config FILE [--connector-socket S --expect-ds 0,1]` | read-only preflight with the MDS rules (same error codes); `smart` folds in `lattice-ds-connector preflight` | nothing |
| `mode set <mode> --config FILE [--set k=v]… [--apply]` | dry-run diff; `--apply` rewrites the one local file inside a managed block with backup, atomic replace, audit line, re-validation | that file, the audit log |
| `mode show --mds h1,h2 [--mds-admin P --mds-port 50051 --env K=V] [--config FILE | --ssh USER]` | live desired/effective mode, generation, build, readiness, per-DS rows from `mds-admin config show --json` + `/metrics`; warns when MDS differ | nothing |
| `mode verify --mds h1,h2 … [--require-full-coverage]` | exit 0 only when every MDS agrees and is ready | nothing |

Exit codes: 0 ready / same; 1 not ready / a difference / refused; 2 an
argument, file or MDS could not be read. `--json` on every command.

The helper never restarts `pnfs-mds`, never edits the connector or xiNAS,
and never infers "smart is active" from a file: `show`/`verify` ask the
live daemons; the desired mode comes from `--config` or `--ssh`, else it
is printed as `?`.

Tests: `python -m pytest -q` (fixtures under `tests/fixtures/` are real
`config show` texts captured on the lab).
