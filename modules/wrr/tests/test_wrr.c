/*
 * SPDX-License-Identifier: MIT
 *
 * test_wrr.c -- standalone checks for the wrr kernels (no cmocka
 * dependency so it builds anywhere the module builds).
 *
 *   cc -I include -o test_wrr test_wrr.c src/modules/wrr/wrr.c
 */
#include "wrr.h"

#include <stdio.h>
#include <stdlib.h>

static int fails;
#define CHECK(cond, ...) do { if (!(cond)) { fails++; printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

int main(void)
{
    /* capacity: strict maximum, lowest index on ties, 0 on all-zero */
    {
        uint64_t w[] = { 10, 50, 50, 7 };
        CHECK(mds_wrr_capacity_pick(w, 4) == 1, "capacity tie -> lowest index");
        uint64_t z[] = { 0, 0, 0 };
        CHECK(mds_wrr_capacity_pick(z, 3) == 0, "capacity all-zero -> 0");
        uint64_t m[] = { 0, 0, 9 };
        CHECK(mds_wrr_capacity_pick(m, 3) == 2, "capacity picks the only non-zero");
        CHECK(mds_wrr_capacity_pick(NULL, 0) == 0, "capacity n==0 -> 0");
    }

    /* weighted: never picks a zero-weight slot while a positive exists */
    {
        uint64_t w[] = { 0, 5, 0, 5, 0 };
        for (int i = 0; i < 100000; i++) {
            uint32_t p = mds_wrr_weighted_pick(w, 5);
            CHECK(p == 1 || p == 3, "weighted picked zero-weight slot %u", p);
            if (fails) break;
        }
        uint64_t z[] = { 0, 0 };
        CHECK(mds_wrr_weighted_pick(z, 2) == 0, "weighted all-zero -> 0");
        CHECK(mds_wrr_weighted_pick(NULL, 0) == 0, "weighted n==0 -> 0");
    }

    /* weighted: frequencies follow the weights (3:1 within 5 %) */
    {
        uint64_t w[] = { 3000, 1000 };
        unsigned long c[2] = { 0, 0 };
        const int N = 200000;
        for (int i = 0; i < N; i++) c[mds_wrr_weighted_pick(w, 2)]++;
        double share0 = (double)c[0] / N;
        printf("weighted 3:1 -> %.3f : %.3f\n", share0, (double)c[1] / N);
        CHECK(share0 > 0.70 && share0 < 0.80, "weighted share off: %.3f", share0);
    }

    /* weighted: real-world sized weights (TiB-scale free bytes) */
    {
        uint64_t w[] = { UINT64_C(38400) << 30, UINT64_C(31400) << 30 }; /* 38.4 TiB, 31.4 TiB */
        unsigned long c[2] = { 0, 0 };
        const int N = 200000;
        for (int i = 0; i < N; i++) c[mds_wrr_weighted_pick(w, 2)]++;
        double share0 = (double)c[0] / N, want = 38400.0 / (38400.0 + 31400.0);
        printf("weighted TiB-scale -> %.3f (expected %.3f)\n", share0, want);
        CHECK(share0 > want - 0.02 && share0 < want + 0.02, "TiB-scale share off: %.3f", share0);
    }

    printf(fails ? "FAILED (%d)\n" : "ALL OK\n", fails);
    return fails ? 1 : 0;
}
