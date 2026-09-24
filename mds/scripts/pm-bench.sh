#!/bin/bash
# pm-bench.sh -- runs on xinas-box: the LAT-25 create benchmark.  N small
# files are created by P parallel workers on the node225 client against
# MDS 2 (shard2); the MDS placement histogram and the OPEN/CREATE phase
# counters are snapshotted before and after, so the run prints client
# throughput and the gate's own latency figures for the same window.
# Usage: pm-bench.sh <label> <nfiles> <parallel> [size_bytes=4096]
set -u
LABEL="$1"; N="$2"; P="$3"; SZ="${4:-4096}"
CL=192.168.65.225; MDS=192.168.65.225; MNT=/mnt/pnfs2; SHARD=shard2
R() { ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$1" "$2"; }
snap() {
    R $MDS "curl -s --retry 3 --retry-connrefused http://127.0.0.1:9090/metrics | grep -E '^pnfs_mds_(placement_admit_seconds_(bucket|sum|count)|open_create_phase_(ns_sum|count)\{phase=\"(ds_prepare|total)\"\}|placement_mode)'"
}
before=$(snap)
out=$(R $CL "install -d $MNT; findmnt $MNT >/dev/null || mount -t nfs4 -o vers=4.2,nconnect=8 $MDS:/ $MNT || exit 1
D=$MNT/$SHARD/pm-bench-$LABEL; rm -rf \$D; mkdir -p \$D; cd \$D
t0=\$(date +%s.%N)
seq 1 $N | xargs -P $P -I{} sh -c 'head -c $SZ /dev/zero > f{}'
sync
t1=\$(date +%s.%N)
echo ELAPSED=\$(echo \"\$t1 - \$t0\" | bc) FILES=\$(ls | wc -l)")
after=$(snap)
python3 - "$LABEL" "$N" "$P" "$out" "$before" "$after" <<'PY'
import re, sys
label, n, p, out, before, after = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6]
def parse(text):
    d = {}
    for line in text.splitlines():
        if " " in line:
            k, v = line.rsplit(" ", 1)
            try: d[k] = float(v)
            except ValueError: pass
    return d
b, a = parse(before), parse(after)
delta = {k: a.get(k, 0) - b.get(k, 0) for k in a}
m = re.search(r"ELAPSED=([0-9.]+) FILES=(\d+)", out)
elapsed, files = float(m.group(1)), int(m.group(2))
mode = [k for k in a if k.startswith("pnfs_mds_placement_mode{")][0].split('"')[1]
cnt = delta.get("pnfs_mds_placement_admit_seconds_count", 0)
ssum = delta.get("pnfs_mds_placement_admit_seconds_sum", 0)
buckets = sorted(((float(k.split('"')[1]) if k.split('"')[1] != "+Inf" else float("inf")), delta[k])
                 for k in delta if k.startswith("pnfs_mds_placement_admit_seconds_bucket{"))
p99 = next((le for le, c in buckets if cnt and c >= 0.99 * cnt), None)
p50 = next((le for le, c in buckets if cnt and c >= 0.50 * cnt), None)
dsp_c = delta.get('pnfs_mds_open_create_phase_count{phase="ds_prepare"}', 0)
dsp_s = delta.get('pnfs_mds_open_create_phase_ns_sum{phase="ds_prepare"}', 0)
tot_c = delta.get('pnfs_mds_open_create_phase_count{phase="total"}', 0)
tot_s = delta.get('pnfs_mds_open_create_phase_ns_sum{phase="total"}', 0)
print("BENCH %s mode=%s files=%d/%d P=%d elapsed=%.2fs throughput=%.1f files/s | gate: n=%d mean=%.2fus p50<=%s p99<=%s | open/create: ds_prepare mean=%.1fus (n=%d) total mean=%.1fus (n=%d)" % (
    label, mode, files, n, p, elapsed, files / elapsed if elapsed else 0, cnt, (ssum / cnt * 1e6) if cnt else 0,
    ("%gs" % p50) if p50 is not None else "-", ("%gs" % p99) if p99 is not None else "-",
    (dsp_s / dsp_c / 1e3) if dsp_c else 0, dsp_c, (tot_s / tot_c / 1e3) if tot_c else 0, tot_c))
PY
