#!/bin/bash
# pm-deploy.sh -- runs on xinas-box: install the placement-modes pnfs-mds
# (built on node225 in /home/lattice/pnfs-lattice-pm/build) on both lab MDS,
# rewrite the managed placement keys in /etc/pnfs-mds/mds.conf (backup kept),
# restart the daemons one at a time and print the startup line + config show.
#
# Usage: pm-deploy.sh <mode: legacy|rr|fill> [extra mds.conf lines, ';'-separated]
#   legacy -> restore the pre-Stage-A keys (placement_policy = wrr, ds_weight 55/45)
# Rollback: pm-deploy.sh --rollback   (reinstall pnfs-mds.wrr + the newest mds.conf.*.bak)
set -u
M1=192.168.65.223; M2=192.168.65.225
R() { ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$1" "$2"; }
MODE="${1:-}"; EXTRA="${2:-}"
TS=$(date +%Y%m%d-%H%M%S)
echo "DEPLOY mode=$MODE extra='$EXTRA' $(date '+%T %Z')"
if [ "$MODE" = "--rollback" ]; then
    for h in $M1 $M2; do
        R $h "systemctl stop pnfs-mds; install -m 0755 /usr/local/bin/pnfs-mds.wrr /usr/local/bin/pnfs-mds
B=\$(ls -t /etc/pnfs-mds/mds.conf.*.bak 2>/dev/null | head -1); [ -n \"\$B\" ] && cp -p \"\$B\" /etc/pnfs-mds/mds.conf; systemctl start pnfs-mds; sleep 6
echo \"[$h] rolled back: \$(systemctl is-active pnfs-mds) conf=\$B\""
    done
    exit 0
fi
case "$MODE" in legacy|rr|fill) ;; *) echo "usage: $0 <legacy|rr|fill> [extra] | --rollback"; exit 2;; esac
# binary from the node225 build tree
scp -q -o BatchMode=yes root@$M2:/home/lattice/pnfs-lattice-pm/build/src/mds/pnfs-mds /root/pm/pnfs-mds.pm || exit 1
for h in $M1 $M2; do
    scp -q -o BatchMode=yes /root/pm/pnfs-mds.pm root@$h:/root/pm/pnfs-mds.pm
    R $h "[ -e /usr/local/bin/pnfs-mds.wrr ] || cp -p /usr/local/bin/pnfs-mds /usr/local/bin/pnfs-mds.wrr
cp -p /etc/pnfs-mds/mds.conf /etc/pnfs-mds/mds.conf.$TS.bak
sed -i -e '/^placement_/d' -e '/^ds_weight\./d' -e '/^ds_capacity_domain\./d' -e '/^# lattice-placement/d' /etc/pnfs-mds/mds.conf
if [ '$MODE' = legacy ]; then printf 'placement_policy_enabled = true\nplacement_policy = wrr\nds_weight.0 = 55\nds_weight.1 = 45\n' >> /etc/pnfs-mds/mds.conf
else printf '# lattice-placement managed block\nplacement_mode = %s\n' '$MODE' >> /etc/pnfs-mds/mds.conf; fi
[ -n '$EXTRA' ] && echo '$EXTRA' | tr ';' '\n' >> /etc/pnfs-mds/mds.conf
systemctl stop pnfs-mds
install -m 0755 /root/pm/pnfs-mds.pm /usr/local/bin/pnfs-mds
echo \"[$h] binary kernel_id symbol: \$(nm /usr/local/bin/pnfs-mds | grep -c ' T mds_wrr_kernel_id'); conf tail:\"; grep -E '^placement|^ds_weight|^ds_capacity' /etc/pnfs-mds/mds.conf"
done
for h in $M1 $M2; do
    R $h "systemctl start pnfs-mds"
    for k in $(seq 1 24); do n=$(R $h "ss -tln | grep -c ':2049 '"); [ "$n" -ge 1 ] && break; sleep 5; done
    R $h "echo \"[$h] pnfs-mds=\$(systemctl is-active pnfs-mds) 2049=\$(ss -tln | grep -c ':2049 ')\"; journalctl -u pnfs-mds --since '-3 min' --no-pager | grep -oE 'placement_mode=[a-z]+ generation=[0-9a-f]+ kernel=[0-9a-f]+ shrink=[a-z]+ max_age_ms=[0-9]+ min_free=[0-9]+|placement_policy=[a-z]+ \(dispatcher active\)|placement gate init failed.*' | tail -1
LD_LIBRARY_PATH=/opt/rondb/lib:/opt/rondb/lib/mysql /usr/local/bin/mds-admin config show --mds-host $h --mds-port 50051 2>/dev/null | grep -E '^placement_(mode|mode_effective|config_generation|kernel_id|ds\.)' || echo '[$h] config show unavailable'"
done
echo DEPLOY_DONE
