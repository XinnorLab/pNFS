#!/bin/bash
# pm-watch.sh -- runs on xinas-box: poll `config show` on one MDS until a
# placement row matches, print the elapsed time and the matching rows.
# Usage: pm-watch.sh <mds-ip> <grep -E pattern> [timeout_s=90] [row=placement_ds.0]
set -u
H="$1"; PAT="$2"; T="${3:-90}"; ROW="${4:-placement_ds.0}"
t0=$(date +%s)
while :; do
    out=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$H" "LD_LIBRARY_PATH=/opt/rondb/lib:/opt/rondb/lib/mysql /usr/local/bin/mds-admin config show --mds-host $H --mds-port 50051 2>/dev/null | grep -E '^placement_(readiness|connector_last_detail|ds\.)'")
    row=$(echo "$out" | grep -E "^$ROW ")
    el=$(( $(date +%s) - t0 ))
    if echo "$row" | grep -qE -- "$PAT"; then echo "WATCH matched after ${el}s:"; echo "$out"; exit 0; fi
    if [ "$el" -ge "$T" ]; then echo "WATCH timeout after ${el}s, last:"; echo "$out"; exit 1; fi
    sleep 2
done
