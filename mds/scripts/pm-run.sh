#!/bin/bash
# pm-run.sh -- build the placement-modes fork on node225 and run tests.
#
# Syncs the LOCAL fork working tree (tracked-modified + untracked files) to
# the node225 clone, which must sit at the same base commit, builds with the
# Stage A flags and runs the ctest targets matching the regex.
#
# Usage: pm-run.sh [--no-sync] <ctest-regex|all|build>
# Env:   FORK (default ~/Documents/GitHub/pnfs-lattice)
set -u
FORK=${FORK:-$HOME/Documents/GitHub/pnfs-lattice}
REMOTE=/home/lattice/pnfs-lattice-pm
SYNC=1
if [ "${1:-}" = "--no-sync" ]; then SYNC=0; shift; fi
WHAT=${1:-all}
BASE=$(git -C "$FORK" rev-parse HEAD)
TAR=/tmp/pm-sync.tgz
# node225 fetches the base commit from the fork: make sure it is there.
if ! git -C "$FORK" merge-base --is-ancestor "$BASE" "origin/$(git -C "$FORK" rev-parse --abbrev-ref HEAD)" 2>/dev/null; then
    for i in 1 2 3; do git -C "$FORK" push -q origin HEAD 2>/dev/null && break; sleep 8; done
fi
if [ "$SYNC" = 1 ]; then
    ( cd "$FORK" && git ls-files -m -o --exclude-standard -z | COPYFILE_DISABLE=1 tar --null --no-xattrs -T - -czf "$TAR" ) || exit 1
    ok=0
    for i in 1 2 3 4; do
        scp -q -o ConnectTimeout=25 -o BatchMode=yes "$TAR" xinas-box:/root/pm-sync.tgz 2>/dev/null && { ok=1; break; }
        sleep 8
    done
    [ "$ok" = 1 ] || { echo "PM_RESULT SYNC_FAILED"; exit 1; }
fi
cat > /tmp/pm-remote.sh <<REMOTE_EOF
set -e
cd $REMOTE
if [ "\$(git rev-parse HEAD)" != "$BASE" ]; then git fetch -q origin && git checkout -q -f $BASE; fi
git checkout -q -- . && git clean -qfd -e build
if [ "$SYNC" = 1 ]; then tar xzf /root/pm-sync.tgz -C $REMOTE; fi
# (re)configure every time: cheap, and new options reach an existing build dir
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DENABLE_RONDB=ON -DRonDB_ROOT=/opt/rondb \
    -DENABLE_EBPF=OFF -DENABLE_TESTS=ON -DENABLE_WRR=ON -DENABLE_DS_PREALLOC=OFF \
    -DENABLE_DS_CONNECTOR=ON >/dev/null
if ! cmake --build build -j32 > /tmp/pm-build.log 2>&1; then
    grep -E "error|Error|エラー" /tmp/pm-build.log | grep -v "error\.c" | head -40
    echo "PM_RESULT BUILD_FAILED"; exit 1
fi
grep -E "warning: " /tmp/pm-build.log | head -20 || true
case "$WHAT" in
    build) echo "PM_RESULT BUILD_OK" ;;
    all)   ctest --test-dir build -j16 2>&1 | tail -8 ;;
    *)     ctest --test-dir build -R "$WHAT" --output-on-failure 2>&1 | tail -60 ;;
esac
REMOTE_EOF
ok=0
for i in 1 2 3; do
    scp -q -o ConnectTimeout=25 -o BatchMode=yes /tmp/pm-remote.sh xinas-box:/root/pm-remote.sh 2>/dev/null && { ok=1; break; }
    sleep 8
done
[ "$ok" = 1 ] || { echo "PM_RESULT SYNC_FAILED"; exit 1; }
# The relay tunnel drops pre-auth now and then (rc 255): retry the hop.
for i in 1 2 3 4; do
    ssh -o ConnectTimeout=25 -o BatchMode=yes xinas-box \
        "scp -q -o BatchMode=yes /root/pm-remote.sh root@192.168.65.225:/root/pm-remote.sh && \
         ( [ $SYNC = 1 ] && scp -q -o BatchMode=yes /root/pm-sync.tgz root@192.168.65.225:/root/pm-sync.tgz || true ) && \
         ssh -o BatchMode=yes root@192.168.65.225 'bash /root/pm-remote.sh'"
    rc=$?
    [ $rc -ne 255 ] && exit $rc
    echo "pm-run: ssh hop dropped (rc 255), retry $i" >&2
    sleep 10
done
exit 255
