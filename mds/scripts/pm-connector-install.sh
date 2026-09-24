#!/bin/bash
# pm-connector-install.sh -- runs on the operator's machine: install (or
# refresh) the lattice-ds-connector tree, its unit and the lattice-placement
# helper on one lab MDS node, copying the connector configuration and the
# source token from a reference node so both MDS run the same connector
# config (same config_digest).  Token values are never printed.
#
# Usage: pm-connector-install.sh <node-ip> [reference-node-ip=192.168.65.225]
# Env:   PNFS (default ~/Documents/GitHub/pNFS)
set -u
NODE="$1"; REF="${2:-192.168.65.225}"
PNFS=${PNFS:-$HOME/Documents/GitHub/pNFS}
TAR=/tmp/pm-connector.tgz
( cd "$PNFS/connectors" && COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' --exclude='.pytest_cache' \
    -czf "$TAR" lattice-ds-connector ) || exit 1
( cd "$PNFS/tools" && COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' --exclude='.pytest_cache' \
    -czf /tmp/pm-helper.tgz lattice-placement ) || exit 1
for i in 1 2 3 4; do
    scp -q -o ConnectTimeout=25 -o BatchMode=yes "$TAR" /tmp/pm-helper.tgz xinas-box:/root/ 2>/dev/null && break
    sleep 8
done
cat > /tmp/pm-node.sh <<'NODE_EOF'
set -e
echo "[$(hostname)] python: $(python3 --version 2>&1)"
getent group pnfs >/dev/null || groupadd -r pnfs
id lattice-ds-connector >/dev/null 2>&1 || useradd -r -g pnfs -s /sbin/nologin -d /opt/lattice-ds-connector -M lattice-ds-connector
# connector tree (same code on every node)
rm -rf /opt/lattice-ds-connector.new && mkdir -p /opt/lattice-ds-connector.new
tar xzf /root/pm-connector.tgz -C /opt/lattice-ds-connector.new --strip-components=1
rm -rf /opt/lattice-ds-connector.old
[ -d /opt/lattice-ds-connector ] && mv /opt/lattice-ds-connector /opt/lattice-ds-connector.old
mv /opt/lattice-ds-connector.new /opt/lattice-ds-connector
chown -R root:root /opt/lattice-ds-connector
# helper
rm -rf /opt/lattice-placement && mkdir -p /opt/lattice-placement
tar xzf /root/pm-helper.tgz -C /opt/lattice-placement --strip-components=1
# configuration copied from the reference node (staged under /root/pm-etc by the caller)
install -d -o root -g pnfs -m 0750 /etc/lattice-ds-connector
install -d -o lattice-ds-connector -g pnfs -m 0750 /etc/lattice-ds-connector/secrets
install -o root -g pnfs -m 0640 /root/pm-etc/config.json /etc/lattice-ds-connector/config.json
install -o root -g pnfs -m 0644 /root/pm-etc/xinas-box-ca.pem /etc/lattice-ds-connector/xinas-box-ca.pem
install -o lattice-ds-connector -g pnfs -m 0600 /root/pm-etc/xi-01.token /etc/lattice-ds-connector/secrets/xi-01.token
rm -rf /root/pm-etc
install -m 0644 /opt/lattice-ds-connector/systemd/lattice-ds-connector.service /etc/systemd/system/lattice-ds-connector.service
systemctl daemon-reload
systemctl enable -q lattice-ds-connector
systemctl restart lattice-ds-connector
for i in $(seq 1 40); do
    code=$(curl -s --unix-socket /run/lattice-ds-connector/connector.sock -o /dev/null -w '%{http_code}' http://c/healthz 2>/dev/null || true)
    [ "$code" = 200 ] && break
    sleep 2
done
echo "[$(hostname)] connector: $(systemctl is-active lattice-ds-connector) healthz=$code code_md5=$(cat /opt/lattice-ds-connector/lattice_ds_connector/*.py /opt/lattice-ds-connector/lattice_ds_connector/modules/*.py | md5sum | cut -c1-12) config_sha=$(sha256sum /etc/lattice-ds-connector/config.json | cut -c1-12)"
cd /opt/lattice-ds-connector && PYTHONPATH=/opt/lattice-ds-connector python3 -m lattice_ds_connector preflight --socket /run/lattice-ds-connector/connector.sock --expect-ds 0 2>&1 | head -6
echo "[$(hostname)] helper: $(PYTHONPATH=/opt/lattice-placement python3 -m lattice_placement --version)"
NODE_EOF
for i in 1 2 3; do
    scp -q -o ConnectTimeout=25 -o BatchMode=yes /tmp/pm-node.sh xinas-box:/root/pm-node.sh 2>/dev/null && break
    sleep 8
done
for i in 1 2 3 4; do
    ssh -o ConnectTimeout=25 -o BatchMode=yes xinas-box "
set -e
rm -rf /root/pm-etc && mkdir -p /root/pm-etc && chmod 700 /root/pm-etc
scp -q -o BatchMode=yes root@$REF:/etc/lattice-ds-connector/config.json root@$REF:/etc/lattice-ds-connector/xinas-box-ca.pem /root/pm-etc/
scp -q -o BatchMode=yes root@$REF:/etc/lattice-ds-connector/secrets/xi-01.token /root/pm-etc/xi-01.token
ssh -o BatchMode=yes root@$NODE 'rm -rf /root/pm-etc && mkdir -p /root/pm-etc && chmod 700 /root/pm-etc'
scp -q -o BatchMode=yes /root/pm-etc/* /root/pm-connector.tgz /root/pm-helper.tgz /root/pm-node.sh root@$NODE:/root/
scp -q -o BatchMode=yes /root/pm-etc/config.json /root/pm-etc/xinas-box-ca.pem /root/pm-etc/xi-01.token root@$NODE:/root/pm-etc/
rm -rf /root/pm-etc
ssh -o BatchMode=yes root@$NODE 'rm -f /root/config.json /root/xinas-box-ca.pem /root/xi-01.token; bash /root/pm-node.sh'"
    rc=$?
    [ $rc -ne 255 ] && exit $rc
    echo "pm-connector-install: ssh hop dropped (rc 255), retry $i" >&2
    sleep 10
done
exit 255
