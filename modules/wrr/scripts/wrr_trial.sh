#!/bin/bash
# Runs on the box: create N files through a pNFS client (node225 mounting MDS 1),
# then count where the data files landed on the two data servers.
# Usage: wrr_trial.sh <label> <nfiles> <size_mb>
set -u
LABEL="$1"; N="$2"; MB="$3"
CL=192.168.65.225; MDS=192.168.65.223
R() { ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$1" "$2"; }
echo "TRIAL $LABEL files=$N size=${MB}M $(date '+%T %Z')"
before_box=$(find /mnt/data/pnfs-ds/data -type f | wc -l)
before_71=$(ssh -o BatchMode=yes x1@192.168.64.71 'sudo -n find /mnt/data/pnfs-ds/data -type f | wc -l')
R $CL "install -d /mnt/pnfs; findmnt /mnt/pnfs >/dev/null || mount -t nfs4 -o vers=4.2,nconnect=8 $MDS:/ /mnt/pnfs || exit 1
D=/mnt/pnfs/shard1/wrr-$LABEL; rm -rf \$D; mkdir -p \$D
for i in \$(seq 1 $N); do dd if=/dev/zero of=\$D/f\$i bs=1M count=$MB status=none; done; sync
echo \"client: \$(ls \$D | wc -l) files written\""
sleep 3
after_box=$(find /mnt/data/pnfs-ds/data -type f | wc -l)
after_71=$(ssh -o BatchMode=yes x1@192.168.64.71 'sudo -n find /mnt/data/pnfs-ds/data -type f | wc -l')
nb=$((after_box - before_box)); n7=$((after_71 - before_71))
echo "RESULT $LABEL: box(DS0)=$nb node71(DS1)=$n7 of $N"
R $MDS "LD_LIBRARY_PATH=/opt/rondb/lib:/opt/rondb/lib/mysql /usr/local/bin/mds-admin ds capacity show 2>&1 | head -6"
