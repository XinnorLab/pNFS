# `wrr` — weighted and capacity-based placement for pnfs-lattice

An independent, MIT-licensed implementation of the two placement kernels that
the community edition of [PEAK-AIO/pnfs-lattice](https://github.com/PEAK-AIO/pnfs-lattice)
ships only as no-op stubs (`src/modules/wrr/wrr_stub.c`).

Written against the public contract in `include/wrr.h` (MIT) and the caller in
`src/fsal_obj/placement.c` (MIT). No enterprise code was consulted or copied.

## What it does

`include/wrr.h` declares two functions that `placement_select_ex2()` calls once
per stripe when the operator has selected `placement_policy = wrr` or
`placement_policy = capacity`:

| Kernel | Behaviour in this module | Behaviour of the community stub |
|---|---|---|
| `mds_wrr_weighted_pick(weights, n)` | Roulette-wheel selection: P(i) = w[i] / Σw. Zero-weight slots are never chosen while a positive one exists. Thread-local `rand_r` state, no locks. | Always returns 0 |
| `mds_wrr_capacity_pick(weights, n)` | Strict maximum; lowest index on ties. | Always returns 0 |

Both return 0 for `n == 0` or an all-zero weight vector, which the caller
handles by walking forward to the next free slot.

The weights themselves come from the (open) caller, in this precedence:
operator `ds_weight.<id>` → `auto_weight` derived from statvfs when
`placement_capacity_weighting = proportional` → raw free bytes → 1.

### Why the stub is worse than plain `rr`

The upstream comment says the stub "degrades gracefully to plain RR". That is
true only *within* one file's stripes: the walk-forward logic advances past a
taken slot for stripe 2, 3, … of the same file. Between files the index is
always 0, so with the stub every single-stripe file lands on the first online
data server. Measured on a two-DS stand: 40 of 40 files on DS 0.

> **Canonical copy moved.** Since 2026-09-23 the kernel is vendored in the
> fork [XinnorLab/pnfs-lattice](https://github.com/XinnorLab/pnfs-lattice)
> (`src/modules/wrr/wrr.c` on `xinnor/placement-modes`), where it also
> exports `mds_wrr_kernel_id()`, the refusing `mds_wrr_weighted_pick2()`
> and a test seed hook used by the placement modes.  This directory keeps
> the original standalone copy and the stand scripts; edit the fork first.

## Build

Upstream's module glue already selects `wrr.c` when the CMake gate is on:

```bash
cp modules/wrr/wrr.c <pnfs-lattice>/src/modules/wrr/wrr.c
cd <pnfs-lattice>
cmake -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_RONDB=ON -DRonDB_ROOT=/opt/rondb \
      -DENABLE_EBPF=OFF -DENABLE_TESTS=ON -DENABLE_WRR=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build            # 64/64 on main @ 6b4dcde
```

Standalone kernel test (no cmocka needed):

```bash
cc -O2 -Wall -Wextra -Werror -std=gnu11 -I <pnfs-lattice>/include \
   -o test_wrr modules/wrr/tests/test_wrr.c modules/wrr/wrr.c && ./test_wrr
```

The resulting `pnfs-mds` exports the same symbol names as the stub build;
tell the binaries apart by size or by `ENABLE_WRR` in `CMakeCache.txt`.

## Configure

```ini
placement_policy_enabled = true
placement_policy = wrr                     # or: capacity
placement_capacity_weighting = proportional  # auto_weight from free space (optional)
# ds_weight.0 = 3                          # manual override per DS (optional)
# ds_weight.1 = 1
```

`ds_capacity_poll_ms` (default 60000) controls how often the MDS re-reads free
space through its DS back-mounts.

## Results on the XinnorLab stand

Two active-active MDS (node223, node225), two data servers (xinas-box, DS 0,
41.4 TB free; node 71, DS 1, 33.9 TB free), pnfs-lattice `main` @ `6b4dcde`,
40 single-stripe files per trial written through a pNFS client, placement read
by counting data files on each DS.

| Binary | Policy | Weights DS0:DS1 | Expected | Observed DS0:DS1 |
|---|---|---|---|---|
| community stub | `wrr` | 3:1 | all on DS0 | 40:0 |
| this module | `wrr` | 3:1 | 30:10 | 34:6 |
| this module | `wrr` | 1:3 | 10:30 | 11:29 |
| this module | `wrr` | 1:1 | 20:20 | 23:17 |
| this module | `wrr` + `proportional` | auto_weight 98:98 (both DSes 98 % free) | 20:20 | 17:23 |
| both | `capacity` | free bytes | all on DS0 (strict max) | 20:0 |

Deviations are within sampling noise for 40 draws. `capacity` legitimately
puts everything on the larger DS until the free-space order flips.

`proportional` derives `auto_weight = floor(free / total × 100)` (1..100) per DS,
so it weights by *fill level*, not by absolute free bytes: two DSes that are
both 98 % free get equal weights regardless of their sizes. Use manual
`ds_weight.<id>` values when the intent is "bigger DS gets more files".

`scripts/wrr_deploy.sh` and `scripts/wrr_trial.sh` are the exact scripts used
for the trials; they assume the stand's addresses and are kept for
reproducibility, not as general tools.

## Licence

MIT, matching the header it implements. Building `pnfs-mds` with RonDB links the
GPL-2.0 `catalogue_rondb_shim.cpp`, so a redistributed binary is GPL-2.0-or-later
as a whole; see upstream `LICENSING.md`.
