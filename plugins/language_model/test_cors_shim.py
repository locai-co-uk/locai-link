# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end behaviour tests for the CORS shim.

Each test stands up a fake "upstream" HTTP server (simulating llama-swap)
plus a CorsShim pointing at it, then exercises the shim with real HTTP
requests. Tests are tagged with port offsets so they can run in parallel
without colliding.

What's covered
--------------
- OPTIONS preflight returns 204 with all the headers Chrome 142+ Local
  Network Access requires (Access-Control-Allow-Private-Network).
- Allowlisted Origin gets ACAO echoed back; non-allowlisted Origin does
  not (and Vary: Origin is always present).
- Non-browser callers (no Origin header) get forwarded transparently with
  no CORS pollution.
- GET /v1/models forwards the upstream JSON body.
- POST /v1/chat/completions streams chunks through as they arrive — no
  buffering — and a client disconnect closes the upstream connection.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

try:
    from .cors_shim import CorsShim, _resolve_allowed_origins
except ImportError:
    from cors_shim import CorsShim, _resolve_allowed_origins  # type: ignore


# Each test picks its own port offset so parallel pytest workers don't collide.
# We start from 18000 (well clear of any default link / llama-swap ports a
# developer might be running locally).
_BASE_PORT = 18000


def _free_port() -> int:
    """Grab an OS-assigned free port; release it so we can rebind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fake upstream — a tiny HTTP server we can program per-test
# ---------------------------------------------------------------------------


class _FakeUpstream:
    """Programmable HTTP server mocking llama-swap for the shim's upstream."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.got_close_for_stream = threading.Event()

        outer = self  # capture for the handler

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kwargs):  # noqa: A002
                pass  # silence the default per-request log spam

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/v1/models":
                    body = json.dumps({"data": [{"id": "fake-model"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/health":
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                _body = self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # Emit five SSE chunks with a 200 ms gap between each.
                # The gap has to be larger than urllib3's read coalescing
                # threshold (~50 ms in practice) so honest streaming is
                # observable on the receiving end — if the shim were
                # buffering, the chunks would all arrive within a few
                # milliseconds at the end. Each chunk is padded above
                # urllib3's read-ahead size so it can't fit two together
                # into one yielded chunk on the client side.
                pad = " " * 200
                try:
                    for i in range(5):
                        chunk = f'data: {{"choices":[{{"delta":{{"content":"chunk-{i}{pad}"}}}}]}}\n\n'
                        self.wfile.write(chunk.encode())
                        self.wfile.flush()
                        time.sleep(0.2)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # Shim disconnected — that's exactly the signal we want
                    # to record for the disconnect test. On Windows the
                    # severed connection surfaces as ConnectionAbortedError
                    # (WinError 10053) rather than BrokenPipeError.
                    outer.got_close_for_stream.set()

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake_upstream():
    port = _free_port()
    server = _FakeUpstream(port)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def running_shim(fake_upstream):
    """A CorsShim bound to a free port, forwarding to the fake upstream."""
    public_port = _free_port()
    shim = CorsShim(
        public_port=public_port,
        upstream_port=fake_upstream.port,
        host="127.0.0.1",
    )
    shim.start()
    # Tiny settle so the server thread is accepting before tests hit it.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", public_port)) == 0:
                break
        time.sleep(0.02)
    try:
        yield (shim, public_port)
    finally:
        shim.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_options_preflight_includes_lna_header(running_shim):
    _shim, port = running_shim
    resp = requests.options(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, authorization",
        },
        timeout=5,
    )
    assert resp.status_code == 204
    # The Chrome-142+ Local Network Access header is the load-bearing
    # piece — without it Chrome refuses the actual cross-origin POST.
    assert resp.headers.get("Access-Control-Allow-Private-Network") == "true"
    assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"
    assert "GET" in resp.headers.get("Access-Control-Allow-Methods", "")
    assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")
    assert "content-type" in resp.headers.get("Access-Control-Allow-Headers", "").lower()
    assert resp.headers.get("Vary") == "Origin"


def test_options_disallowed_origin_omits_acao(running_shim):
    _shim, port = running_shim
    resp = requests.options(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        headers={"Origin": "https://evil.example.com"},
        timeout=5,
    )
    assert resp.status_code == 204
    assert "Access-Control-Allow-Origin" not in resp.headers
    # Vary is always sent — caches must vary by Origin even on rejection.
    assert resp.headers.get("Vary") == "Origin"


def test_get_models_passes_through(running_shim):
    _shim, port = running_shim
    resp = requests.get(
        f"http://127.0.0.1:{port}/v1/models",
        headers={"Origin": "http://localhost:3000"},
        timeout=5,
    )
    assert resp.status_code == 200
    assert resp.json() == {"data": [{"id": "fake-model"}]}
    assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


def test_get_no_origin_pass_through_for_cli_callers(running_shim):
    """Non-browser callers (curl, requests, etc.) work without CORS pollution."""
    _shim, port = running_shim
    resp = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"data": [{"id": "fake-model"}]}
    # No Origin header in the request → no ACAO in the response.
    assert "Access-Control-Allow-Origin" not in resp.headers
    # Vary: Origin is still present (correctness for any cache in the chain).
    assert resp.headers.get("Vary") == "Origin"


def test_post_streams_chunks_progressively(running_shim):
    """Total response time must reflect upstream pacing — no shim buffering.

    The fake upstream sends 5 chunks with 200 ms gaps = at least 800 ms of
    inter-chunk waiting. If the shim buffered the whole response, the
    client would see everything in one burst at the end (still 800 ms
    total wall clock, but no progressive arrival). To distinguish:

      - We measure the time from POST until the FIRST chunk arrives.
        Buffered = ~1000 ms (after upstream finishes); streaming = ~200 ms
        (just past the first server gap).
      - We assert the first chunk arrived well before the last chunk would
        have been sent. Anything < 600 ms proves we're streaming.
    """
    _shim, port = running_shim
    payload = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}

    request_start = time.monotonic()
    with requests.post(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        json=payload,
        stream=True,
        timeout=15,
    ) as resp:
        assert resp.status_code == 200
        first_chunk_at: float | None = None
        body = b""
        # Counterintuitive: chunk_size=None makes requests buffer the
        # entire response — we have to pass a positive int to actually
        # stream. 64 is fine; matches typical SSE chunk size.
        for raw in resp.iter_content(chunk_size=64):
            if not raw:
                continue
            if first_chunk_at is None:
                first_chunk_at = time.monotonic()
            body += raw

    assert first_chunk_at is not None, "no chunks received"
    time_to_first_byte = first_chunk_at - request_start
    # Upstream emits 5 chunks at 200 ms intervals (total ~1000 ms). If the
    # shim were buffering, first-chunk arrival would be ~1000 ms. Streaming
    # cleanly puts it at ~200 ms.
    assert time_to_first_byte < 0.6, f"first chunk arrived after {time_to_first_byte:.3f}s — shim is buffering"
    # And we should have received all 5 chunks' worth of content.
    assert body.count(b"chunk-") == 5, f"received {body.count(b'chunk-')} chunks, expected 5"


def test_post_client_disconnect_closes_upstream(running_shim, fake_upstream):
    """Bailing mid-stream releases llama-swap's maxConcurrent=1 slot."""
    _shim, port = running_shim
    payload = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}

    # Open the streaming POST, read the first chunk, abandon the connection.
    # The fake upstream sets its `got_close_for_stream` flag in its except
    # block when it observes the broken pipe.
    with requests.post(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        json=payload,
        stream=True,
        timeout=10,
    ) as resp:
        # chunk_size > 0 — see test_post_streams_chunks_progressively
        # comment on why iter_content(None) is a buffering trap.
        for raw in resp.iter_content(chunk_size=64):
            if raw:
                break  # got at least one chunk, now abandon
        resp.close()

    # Give the shim time to notice the broken pipe on its NEXT write to
    # the client (triggered when the upstream emits the next chunk after
    # its 200 ms sleep). 5 seconds is a generous ceiling — typical
    # detection is well under 500 ms.
    assert fake_upstream.got_close_for_stream.wait(timeout=5.0), "shim did not close upstream when client disconnected"


def test_env_override_allowlist(monkeypatch):
    """LOCAI_CORS_ALLOWED_ORIGINS extends the static defaults."""
    monkeypatch.setenv(
        "LOCAI_CORS_ALLOWED_ORIGINS",
        "https://partner-a.example.com,https://partner-b.example.com",
    )
    origins = _resolve_allowed_origins()
    assert "https://partner-a.example.com" in origins
    assert "https://partner-b.example.com" in origins
    # The static defaults must still be present.
    assert "http://localhost:3000" in origins


def test_double_start_is_idempotent(running_shim):
    shim, _port = running_shim
    # Already started by the fixture; a second start() should no-op,
    # not raise.
    shim.start()
    assert shim.is_running()
