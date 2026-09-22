# SPDX-License-Identifier: MIT
"""The local read-only HTTP server on a Unix socket (CON-01, CON-22).

Routes:
  GET /v1/assessments  the full local batch (no pagination, no backend call)
  GET /healthz         runtime readiness (not DS health); 503 until every
                       instance has collected once
  GET /metrics         Prometheus text with bounded labels

Access is the socket's file mode: 0660, owned by the daemon user and the
MDS group (``runtime.socket_group``).
"""

from __future__ import annotations

import grp
import http.server
import json
import os
import socket
import socketserver
import threading
from typing import Any, Optional

from .runtime import BatchTooLarge, Runtime


class UnixHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, handler, group: Optional[str] = None):
        self.socket_path = path
        self.socket_group = group
        socketserver.TCPServer.__init__(self, path, handler, bind_and_activate=False)
        self.server_bind()
        self.server_activate()

    def server_bind(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        os.makedirs(os.path.dirname(self.socket_path) or ".", mode=0o750, exist_ok=True)
        old_umask = os.umask(0o117)
        try:
            self.socket.bind(self.socket_path)
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, 0o660)
        if self.socket_group:
            try:
                gid = grp.getgrnam(self.socket_group).gr_gid
                os.chown(self.socket_path, -1, gid)
            except (KeyError, PermissionError):
                pass
        self.server_name = "lattice-ds-connector"
        self.server_port = 0

    def server_close(self) -> None:
        super().server_close()
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass


def make_handler(runtime: Runtime):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "lattice-ds-connector"
        sys_version = ""

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0]
            if path == "/v1/assessments":
                try:
                    body = runtime.batch_bytes()
                except BatchTooLarge as exc:
                    runtime.log.counters.inc("connector_batch_rejected_total", {"code": "BATCH_TOO_LARGE"})
                    self._send(500, json.dumps({"error": {"code": "BATCH_TOO_LARGE", "bytes": exc.size, "limit": exc.limit}}).encode())
                    return
                runtime.log.counters.inc("connector_assessments_served_total")
                self._send(200, body)
            elif path == "/healthz":
                health = runtime.health()
                self._send(200 if health["ready"] else 503, json.dumps(health).encode())
            elif path == "/metrics":
                self._send(200, runtime.metrics_text().encode(), "text/plain; version=0.0.4")
            else:
                self._send(404, json.dumps({"error": {"code": "NOT_FOUND"}}).encode())

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def log_message(self, fmt: str, *args: Any) -> None:
            # Access logging is a counter, not a journal line per poll.
            return

        def address_string(self) -> str:
            return "unix"

    return Handler


class Server:
    def __init__(self, runtime: Runtime, socket_path: str, group: Optional[str] = None):
        self.runtime = runtime
        self.socket_path = socket_path
        self._server = UnixHTTPServer(socket_path, make_handler(runtime), group)
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, name="uds-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(2.0)


def get_json(socket_path: str, path: str = "/v1/assessments", timeout_s: float = 0.5):
    """Client helper (CLI ``show`` and tests): one GET over the Unix socket."""
    import http.client

    class UnixConnection(http.client.HTTPConnection):
        def __init__(self, sock_path: str, timeout: float):
            super().__init__("localhost", timeout=timeout)
            self._sock_path = sock_path

        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(self._sock_path)

    conn = UnixConnection(socket_path, timeout_s)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, json.loads(body.decode("utf-8")) if body else None
    finally:
        conn.close()
