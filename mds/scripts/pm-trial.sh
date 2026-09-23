#!/bin/bash
# pm-trial.sh -- runs on xinas-box: create N files through the pNFS client
# on node225 (mounting MDS 1) and count where the data files landed.
# Usage: pm-trial.sh <label> <nfiles> <size_mb>
set -u
LABEL="$1"; N="$2"; MB="$3"
CL=192.168.65.225; MDS=192.168.65.223
R() { ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$1" "$2"; }
echo "TRIAL $LABEL files=$N size=${MB}M $(date '+%T %Z')"
before_box=$(find /mnt/data/pnfs-ds/data -type f | wc -l)
before_71=$(ssh -o BatchMode=yes x1@192.168.64.71 'sudo -n find /mnt/data/pnfs-ds/data -type f | wc -l')
R $CL "install -d /mnt/pnfs; findmnt /mnt/pnfs >/dev/null || mount -t nfs4 -o vers=4.2,nconnect=8 $MDS:/ /mnt/pnfs || exit 1
D=/mnt/pnfs/shard1/pm-$LABEL; rm -rf \$D; mkdir -p \$D
for i in \$(seq 1 $N); do dd if=/dev/zero of=\$D/f\$i bs=1M count=$MB status=none || echo \"write f\$i failed\"; done; sync
echo \"client: \$(ls \$D | wc -l) files written\""
sleep 3
after_box=$(find /mnt/data/pnfs-ds/data -type f | wc -l)
after_71=$(ssh -o BatchMode=yes x1@192.168.64.71 'sudo -n find /mnt/data/pnfs-ds/data -type f | wc -l')
nb=$((after_box - before_box)); n7=$((after_71 - before_71))
echo "RESULT $LABEL: box(DS0)=$nb node71(DS1)=$n7 of $N"
R $MDS "curl -s http://127.0.0.1:9090/metrics | grep -E '^pnfs_mds_placement_(mode|eligible_ds|rejections_total\{reason=\"(CAPACITY_[A-Z]+|NO_ELIGIBLE_DS|DS_OFFLINE)\"\})' | grep -v ' 0$'"
