# pNFS

Work on top of [PEAK-AIO/pnfs-lattice](https://github.com/PEAK-AIO/pnfs-lattice)
(pNFS Flex Files metadata server): replacements for community-edition stubs,
stand scripts and findings from the XinnorLab lab.

Upstream baseline: `main` @ `6b4dcde` (2026-09-04).

| Path | What |
|---|---|
| [`modules/wrr/`](modules/wrr/) | Weighted-round-robin and capacity placement kernels (`ENABLE_WRR=ON`), verified on a 2-MDS / 2-DS stand |
| [`connectors/lattice-ds-connector/`](connectors/lattice-ds-connector/) | Per-MDS placement-assessment daemon (first prototype of the Lattice–xiNAS connector MVP): xinas module, fixture module, contracts, golden fixtures, 198 tests |

Everything here is MIT unless a file's SPDX header says otherwise.
