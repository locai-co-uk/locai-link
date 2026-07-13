# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Serving-proxy request cost and upstream connection reuse (validates 2.2)."""

import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from link.infra.serving_proxy import ServingProxy


class _CountingUpstream(ThreadingHTTPServer):
    """Mock llama-swap upstream that counts accepted TCP connections."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        self.connections = 0

    def get_request(self):
        req = super().get_request()
        self.connections += 1
        return req


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive so pool reuse is observable
    disable_nagle_algorithm = True  # model a real upstream (llama-swap/llama-server set NODELAY)

    def log_message(self, *_a):
        pass

    def do_GET(self):
        body = b'{"data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def proxy():
    upstream = _CountingUpstream(("127.0.0.1", 0), _Handler)
    upstream_port = upstream.server_address[1]
    import threading

    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    public_port = _free_port()
    p = ServingProxy(public_port=public_port, upstream_port=upstream_port, host="127.0.0.1")
    p.start()
    base = f"http://127.0.0.1:{public_port}"
    # Warm one request so first-connection cost isn't in the sample.
    requests.get(base + "/v1/models", timeout=5)
    try:
        yield base, upstream
    finally:
        p.stop()
        upstream.shutdown()


def test_proxy_get_latency(benchmark, proxy):
    base, _ = proxy
    client = requests.Session()

    def _get():
        return client.get(base + "/v1/models", timeout=5)

    resp = benchmark(_get)
    assert resp.status_code == 200


def test_proxy_reuses_upstream_connections(proxy):
    """With a pooled session, N sequential requests must not open N upstream
    connections. Guards the 2.2 fix against regressing to per-request TCP."""
    base, upstream = proxy
    upstream.connections = 0
    client = requests.Session()
    for _ in range(50):
        assert client.get(base + "/v1/models", timeout=5).status_code == 200
    print(f"\nupstream connections for 50 requests: {upstream.connections}")
    assert upstream.connections <= 5, f"expected pooled reuse, got {upstream.connections} connections"
