# SPDX-License-Identifier: MIT
"""lattice-ds-connector: per-MDS placement-assessment daemon for pnfs-lattice.

The runtime loads registered module types (``xinas`` for production, ``fixture``
for tests), polls each configured source on a bounded schedule, evaluates the
source's observations against a placement profile and publishes one local
batch of per-DS assessments over a Unix socket (``GET /v1/assessments``).

Python 3.9+, standard library only at runtime.
"""

__version__ = "0.1.0"
