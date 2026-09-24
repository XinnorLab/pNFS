# SPDX-License-Identifier: MIT
"""lattice-placement -- operator helper for the pnfs-lattice placement modes
(design `docs/superpowers/specs/2026-09-23-placement-modes-design.md`, section 10).

It never restarts a daemon, never edits the connector or xiNAS, and never
infers that a mode is active from a file: `show`/`verify` ask the live MDS.
"""

__version__ = "0.1.0"
