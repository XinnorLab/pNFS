#!/bin/bash
# Runs on the box: install the WRR-enabled pnfs-mds on both MDS hosts (keeping the
# community binary as a backup), switch placement policy, restart the daemons.
# Usage: wrr_deploy.sh <policy: rr|wrr|capacity> [extra mds.conf lines, ';'-separated]
set -u
M1=192.168.65.223; M2=192.168.65.225
POLICY="$1"; EXTRA="${2:-}"
R() { ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$1" "$2"; }
echo "DEPLOY policy=$POLICY extra='$EXTRA' $(date '+%T %Z')"
# binary from the build tree on node225
scp -q -o BatchMode=yes root@$M2:/home/lattice/pnfs-lattice/build-wrr/src/mds/pnfs-mds /root/wrr/pnfs-mds.wrr
for h in $M1 $M2; do
    scp -q -o BatchMode=yes /root/wrr/pnfs-mds.wrr root@$h:/root/wrr/pnfs-mds.wrr
    R $h "[ -e /usr/local/bin/pnfs-mds.community ] || cp -p /usr/local/bin/pnfs-mds /usr/local/bin/pnfs-mds.community
systemctl stop pnfs-mds
install -m 0755 /root/wrr/pnfs-mds.wrr /usr/local/bin/pnfs-mds
sed -i -e '/^placement_policy/d' -e '/^ds_weight\./d' -e '/^placement_capacity_weighting/d' /etc/pnfs-mds/mds.conf
printf 'placement_policy_enabled = true\nplacement_policy = %s\n' '$POLICY' >> /etc/pnfs-mds/mds.conf
[ -n '$EXTRA' ] && echo '$EXTRA' | tr ';' '\n' >> /etc/pnfs-mds/mds.conf
echo \"[$h] binary=\$(nm /usr/local/bin/pnfs-mds | grep -c ' T mds_wrr_') wrr symbols; conf tail:\"; tail -4 /etc/pnfs-mds/mds.conf"
done
for h in $M1 $M2; do
    R $h "systemctl start pnfs-mds"
    for k in $(seq 1 24); do n=$(R $h "ss -tln | grep -c ':2049 '"); [ "$n" -ge 1 ] && break; sleep 5; done
    R $h "echo \"[$h] pnfs-mds=\$(systemctl is-active pnfs-mds) 2049=\$(ss -tln | grep -c ':2049 ') \$(journalctl -u pnfs-mds --since '-3 min' --no-pager | grep -oE 'placement_policy=[a-z]+ \(dispatcher active\)|placement_policy_enabled=false[^\"]*' | tail -1)\""
done
echo DEPLOY_DONE
