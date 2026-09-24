#!/bin/bash
# pm-trial.sh -- runs on xinas-box: create N files through the pNFS client
# on node225 (mounting MDS 1) and count where the data files landed.
# Usage: pm-trial.sh <label> <nfiles> <size_mb>
# Env:   MDS (default 192.168.65.223 = MDS 1 / shard1; 192.168.65.225 = MDS 2 / shard2)
#        the mount point and the shard follow the MDS; CLIENT is node225.
set -u
LABEL="$1"; N="$2"; MB="$3"
CL=192.168.65.225; MDS=${MDS:-192.168.65.223}
if [ "$MDS" = 192.168.65.225 ]; then MNT=/mnt/pnfs2; SHARD=shard2; else MNT=/mnt/pnfs; SHARD=shard1; fi
R() { ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$1" "$2"; }
echo "TRIAL $LABEL files=$N size=${MB}M $(date '+%T %Z')"
before_box=$(find /mnt/data/pnfs-ds/data -type f | wc -l)
before_71=$(ssh -o BatchMode=yes x1@192.168.64.71 'sudo -n find /mnt/data/pnfs-ds/data -type f | wc -l')
R $CL "install -d $MNT; findmnt $MNT >/dev/null || mount -t nfs4 -o vers=4.2,nconnect=8 $MDS:/ $MNT || exit 1
D=$MNT/$SHARD/pm-$LABEL; rm -rf \$D; mkdir -p \$D
fail=0
for i in \$(seq 1 $N); do dd if=/dev/zero of=\$D/f\$i bs=1M count=$MB status=none 2>/tmp/pm-dd.err || { fail=\$((fail+1)); [ \$fail -le 2 ] && echo \"write f\$i failed: \$(tr -d '\n' </tmp/pm-dd.err | cut -c1-120)\"; }; done; sync
echo \"client: \$(ls \$D | wc -l) files present, \$fail writes failed\""
sleep 3
after_box=$(find /mnt/data/pnfs-ds/data -type f | wc -l)
after_71=$(ssh -o BatchMode=yes x1@192.168.64.71 'sudo -n find /mnt/data/pnfs-ds/data -type f | wc -l')
nb=$((after_box - before_box)); n7=$((after_71 - before_71))
echo "RESULT $LABEL: box(DS0)=$nb node71(DS1)=$n7 of $N"
R $MDS "curl -s http://127.0.0.1:9090/metrics | grep -E '^pnfs_mds_(placement_(mode|eligible_ds|rejections_total\{reason=\"(CAPACITY_[A-Z]+|NO_ELIGIBLE_DS|DS_OFFLINE|NO_BINDING|ASSESSMENT_[A-Z]+|CONNECTOR_DENIED|MODE_NOT_READY)\"\})|connector_(reachable|covered_ds|batches_accepted_total|poll_errors_total|batches_dropped_total))' | grep -v ' 0$'"
