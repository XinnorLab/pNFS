#!/bin/bash
# pm-deploy.sh -- runs on xinas-box: install the placement-modes pnfs-mds
# (built on node225 in /home/lattice/pnfs-lattice-pm/build) on both lab MDS,
# rewrite the placement keys with `lattice-placement mode set --apply` on
# each node (backup + audit line on the node), restart the daemons one at a
# time and finish with `lattice-placement mode verify` over both.
#
# Usage: pm-deploy.sh [--no-binary] <mode: legacy|rr|fill|smart> [key=value;key=value…]
#   legacy      -> placement_policy = wrr, ds_weight 55/45 (the pre-Stage-A keys)
#   --no-binary -> keep the installed binary (config change + restart only)
# Rollback: pm-deploy.sh --rollback   (reinstall pnfs-mds.wrr + the newest mds.conf.*.bak)
set -u
M1=192.168.65.223; M2=192.168.65.225
HELPER='PYTHONPATH=/opt/lattice-placement python3 -m lattice_placement'
ADMIN='--mds-admin /usr/local/bin/mds-admin --env LD_LIBRARY_PATH=/opt/rondb/lib:/opt/rondb/lib/mysql'
CONN='--connector-socket /run/lattice-ds-connector/connector.sock --connector-cli /opt/lattice-ds-connector/scripts/lattice-ds-connector --expect-ds 0'
R() { ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$1" "$2"; }
BIN=1
if [ "${1:-}" = "--no-binary" ]; then BIN=0; shift; fi
MODE="${1:-}"; EXTRA="${2:-}"
echo "DEPLOY mode=$MODE extra='$EXTRA' binary=$BIN $(date '+%T %Z')"
if [ "$MODE" = "--rollback" ]; then
    for h in $M1 $M2; do
        R $h "systemctl stop pnfs-mds; install -m 0755 /usr/local/bin/pnfs-mds.wrr /usr/local/bin/pnfs-mds
B=\$(ls -t /etc/pnfs-mds/mds.conf.*.bak 2>/dev/null | head -1); [ -n \"\$B\" ] && cp -p \"\$B\" /etc/pnfs-mds/mds.conf; systemctl start pnfs-mds; sleep 6
echo \"[$h] rolled back: \$(systemctl is-active pnfs-mds) conf=\$B\""
    done
    exit 0
fi
case "$MODE" in legacy|rr|fill|smart) ;; *) echo "usage: $0 [--no-binary] <legacy|rr|fill|smart> [key=value;…] | --rollback"; exit 2;; esac
SETARGS=""
if [ "$MODE" = legacy ]; then
    SETARGS="--ds-weight 0=55 --ds-weight 1=45"
else
    IFS=';' read -ra kvs <<< "$EXTRA"
    for kv in "${kvs[@]}"; do kv=$(echo "$kv" | tr -d ' '); [ -n "$kv" ] && SETARGS="$SETARGS --set $kv"; done
fi
VAL=""; [ "$MODE" = smart ] && VAL="$CONN"
if [ "$BIN" = 1 ]; then
    scp -q -o BatchMode=yes root@$M2:/home/lattice/pnfs-lattice-pm/build/src/mds/pnfs-mds /root/pm/pnfs-mds.pm || exit 1
fi
for h in $M1 $M2; do
    if [ "$BIN" = 1 ]; then
        scp -q -o BatchMode=yes /root/pm/pnfs-mds.pm root@$h:/root/pm/pnfs-mds.pm
        R $h "[ -e /usr/local/bin/pnfs-mds.wrr ] || cp -p /usr/local/bin/pnfs-mds /usr/local/bin/pnfs-mds.wrr"
    fi
    R $h "echo \"[$h] validate $MODE: \$($HELPER mode validate $MODE --config /etc/pnfs-mds/mds.conf $VAL 2>&1 | head -1)\"
$HELPER mode set $MODE --config /etc/pnfs-mds/mds.conf --apply $SETARGS 2>&1 | grep -E 'applied|NO CHANGE|REFUSED|error|WARNING|NOTE|sha256' | sed \"s/^/[$h] /\"
systemctl stop pnfs-mds
$( [ $BIN = 1 ] && echo 'install -m 0755 /root/pm/pnfs-mds.pm /usr/local/bin/pnfs-mds' )
echo \"[$h] conf tail: \$(grep -E '^placement|^ds_weight|^ds_capacity_domain|^ds_connector' /etc/pnfs-mds/mds.conf | tr '\n' ' ')\""
done
for h in $M1 $M2; do
    R $h "systemctl start pnfs-mds"
    for k in $(seq 1 24); do n=$(R $h "ss -tln | grep -c ':2049 '"); [ "$n" -ge 1 ] && break; sleep 5; done
    R $h "echo \"[$h] pnfs-mds=\$(systemctl is-active pnfs-mds) 2049=\$(ss -tln | grep -c ':2049 ')\"; journalctl -u pnfs-mds --since '-3 min' --no-pager | grep -oE 'placement_mode=[a-z]+ generation=[0-9a-f]+ kernel=[0-9a-f]+ shrink=[a-z]+ max_age_ms=[0-9]+ min_free=[0-9]+( connector=[a-z0-9/._-]+ poll_ms=[0-9]+)?|placement_policy=[a-z]+ \(dispatcher active\)|placement gate init failed.*' | tail -1"
done
echo "VERIFY:"
R $M2 "$HELPER mode verify --mds $M1,$M2 $ADMIN --ssh root 2>&1"
echo "VERIFY_RC=$?"
echo DEPLOY_DONE
