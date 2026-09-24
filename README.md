# pNFS

Work on top of [PEAK-AIO/pnfs-lattice](https://github.com/PEAK-AIO/pnfs-lattice)
(pNFS Flex Files metadata server): replacements for community-edition stubs,
stand scripts and findings from the XinnorLab lab.

Upstream baseline: `main` @ `6b4dcde` (2026-09-04).

| Path | What |
|---|---|
| [`modules/wrr/`](modules/wrr/) | Weighted-round-robin and capacity placement kernels (`ENABLE_WRR=ON`), verified on a 2-MDS / 2-DS stand |
| [`connectors/lattice-ds-connector/`](connectors/lattice-ds-connector/) | Per-MDS placement-assessment daemon (first prototype of the Lattice–xiNAS connector MVP): xinas module, fixture module, contracts, golden fixtures, 274 tests |
| [`mds/`](mds/) | The MDS side of the placement modes (`placement_mode = rr \| fill \| smart`): `manifest.json` (upstream SHA, fork SHA, patch digests, build flags), the exported patch series against `6b4dcde`, and the node225 build loop. The code lives in the fork [XinnorLab/pnfs-lattice](https://github.com/XinnorLab/pnfs-lattice) on `xinnor/placement-modes` |
| [`tools/lattice-placement/`](tools/lattice-placement/) | The operator helper `lattice-placement mode show \| validate \| set \| verify` (Python 3.9 stdlib): read-only preflight with the MDS rules, managed-block config edits with backup/audit, live show/verify over `mds-admin config show`; the switch procedure is [`docs/placement-modes/operations.md`](docs/placement-modes/operations.md) |
| [`docs/placement-modes/`](docs/placement-modes/) | Contract manifest (key names, defaults, ranges, reasons) shared by the MDS tests and the `lattice-placement` helper, the operations runbook, example configs, stand reports; design under `docs/superpowers/specs/`, plans under `docs/superpowers/plans/` |
| [`scripts/check-manifests.py`](scripts/check-manifests.py) | CI: patch digests, `git format-patch` reproducibility against the fork at the recorded SHA, manifest defaults vs `placement_modes.h`, the bundled manifest copy |

Everything here is MIT unless a file's SPDX header says otherwise.
