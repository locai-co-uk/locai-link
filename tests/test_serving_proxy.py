# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end behaviour tests for ServingProxy.

Each test stands up a fake "upstream" HTTP server (simulating llama-swap)
plus a ServingProxy pointing at it, then exercises the proxy with real
HTTP requests. Each fixture obtains OS-assigned free ports via
_free_port() so tests can run in parallel without colliding.

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
- Telemetry: on_telemetry fires exactly once per chat-completion request,
  with token counts pulled from response.usage (streaming + non-streaming)
  AND fallback to delta-chunk counting when usage isn't present. These
  are the same metrics the old log-parse path produced (model_id,
  start_time/end_time, duration_seconds, tokens_generated).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import requests
from typing_extensions import override

from link.infra.serving_proxy import ServingProxy


def _free_port() -> int:
    """Grab an OS-assigned free port; release it so we can rebind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fake upstream — a tiny HTTP server we can program per-test
# ---------------------------------------------------------------------------


class _FakeUpstream:
    """Programmable HTTP server mocking llama-swap for the proxy's upstream."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.got_close_for_stream = threading.Event()

        outer = self  # capture for the handler

        class Handler(BaseHTTPRequestHandler):
            @override
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
                self.rfile.read(length)  # drain request body; result unused
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # Five SSE chunks with a 200 ms gap — larger than urllib3's
                # ~50 ms read-coalescing threshold so honest streaming is
                # observable (buffering would deliver everything at the end).
                # Each chunk is padded above urllib3's read-ahead size so two
                # can't coalesce into one client-side chunk.
                pad = " " * 200
                try:
                    for i in range(5):
                        chunk = f'data: {{"choices":[{{"delta":{{"content":"chunk-{i}{pad}"}}}}]}}\n\n'
                        self.wfile.write(chunk.encode())
                        self.wfile.flush()
                        time.sleep(0.2)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # Proxy disconnected — that's exactly the signal we want
                    # to record for the disconnect test. On Windows the
                    # severed connection surfaces as ConnectionAbortedError
                    # (WinError 10053) rather than BrokenPipeError.
                    outer.got_close_for_stream.set()

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        # poll_interval=0.05 keeps teardown fast; the default 0.5 makes
        # every fixture stop() block ~500ms and dominates the suite.
        self._thread = threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.05), daemon=True)
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
def running_proxy(fake_upstream):
    """A ServingProxy bound to a free port, forwarding to the fake upstream.

    Origins are passed via the constructor — the surface the agent config
    plumbs through. The proxy ships with no baked-in origins; callers
    have to supply their own.
    """
    public_port = _free_port()
    proxy = ServingProxy(
        public_port=public_port,
        upstream_port=fake_upstream.port,
        allowed_origins=["http://localhost:3000"],
        host="127.0.0.1",
    )
    proxy.start()
    # Tiny settle so the server thread is accepting before tests hit it.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", public_port)) == 0:
                break
        time.sleep(0.02)
    try:
        yield (proxy, public_port)
    finally:
        proxy.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_options_preflight_includes_lna_header(running_proxy):
    _proxy, port = running_proxy
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


def test_options_disallowed_origin_omits_acao(running_proxy):
    _proxy, port = running_proxy
    resp = requests.options(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        headers={"Origin": "https://evil.example.com"},
        timeout=5,
    )
    assert resp.status_code == 204
    assert "Access-Control-Allow-Origin" not in resp.headers
    # Vary is always sent — caches must vary by Origin even on rejection.
    assert resp.headers.get("Vary") == "Origin"


def test_get_models_passes_through(running_proxy):
    _proxy, port = running_proxy
    resp = requests.get(
        f"http://127.0.0.1:{port}/v1/models",
        headers={"Origin": "http://localhost:3000"},
        timeout=5,
    )
    assert resp.status_code == 200
    assert resp.json() == {"data": [{"id": "fake-model"}]}
    assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"


def test_get_no_origin_pass_through_for_cli_callers(running_proxy):
    """Non-browser callers (curl, requests, etc.) work without CORS pollution."""
    _proxy, port = running_proxy
    resp = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"data": [{"id": "fake-model"}]}
    # No Origin header in the request → no ACAO in the response.
    assert "Access-Control-Allow-Origin" not in resp.headers
    # Vary: Origin is still present (correctness for any cache in the chain).
    assert resp.headers.get("Vary") == "Origin"


def test_post_streams_chunks_progressively(running_proxy):
    """Total response time must reflect upstream pacing — no proxy buffering.

    The fake upstream sends 5 chunks with 200 ms gaps = at least 800 ms of
    inter-chunk waiting. If the proxy buffered the whole response, the
    client would see everything in one burst at the end.

      - We measure the time from POST until the FIRST chunk arrives.
        Buffered = ~1000 ms (after upstream finishes); streaming = ~200 ms
        (just past the first server gap).
      - We assert the first chunk arrived well before the last chunk would
        have been sent. Anything < 600 ms proves we're streaming.
    """
    _proxy, port = running_proxy
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
    assert time_to_first_byte < 0.6, f"first chunk arrived after {time_to_first_byte:.3f}s — proxy is buffering"
    assert body.count(b"chunk-") == 5, f"received {body.count(b'chunk-')} chunks, expected 5"


def test_post_client_disconnect_closes_upstream(running_proxy, fake_upstream):
    """Bailing mid-stream releases llama-swap's maxConcurrent=1 slot."""
    _proxy, port = running_proxy
    payload = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}

    with requests.post(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        json=payload,
        stream=True,
        timeout=10,
    ) as resp:
        for raw in resp.iter_content(chunk_size=64):
            if raw:
                break  # got at least one chunk, now abandon
        resp.close()

    # Give the proxy time to notice the broken pipe on its NEXT write to
    # the client (triggered when the upstream emits the next chunk after
    # its 200 ms sleep). 5 seconds is a generous ceiling — typical
    # detection is well under 500 ms.
    assert fake_upstream.got_close_for_stream.wait(timeout=5.0), "proxy did not close upstream when client disconnected"


def test_empty_allowlist_rejects_browser_callers(fake_upstream):
    """A proxy constructed with no origins echoes no ACAO (secure default)."""
    public_port = _free_port()
    proxy = ServingProxy(
        public_port=public_port,
        upstream_port=fake_upstream.port,
        allowed_origins=[],
        host="127.0.0.1",
    )
    proxy.start()
    try:
        resp = requests.options(
            f"http://127.0.0.1:{public_port}/v1/chat/completions",
            headers={"Origin": "http://localhost:3000"},
            timeout=5,
        )
        # Preflight still returns 204 (the proxy doesn't 4xx unknown origins)
        # but with no ACAO — Chrome will fail the actual request, which is
        # the secure-by-default outcome.
        assert resp.status_code == 204
        assert "Access-Control-Allow-Origin" not in resp.headers
        assert resp.headers.get("Vary") == "Origin"
    finally:
        proxy.stop()


def test_double_start_is_idempotent(running_proxy):
    proxy, _port = running_proxy
    # Already started by the fixture; a second start() should no-op, not raise.
    proxy.start()
    assert proxy.is_running()


# ---------------------------------------------------------------------------
# Telemetry — one record per /v1/chat/completions, matching the log-parse
# strategy's payload (model, start/end time, duration_seconds, tokens_generated).
# ---------------------------------------------------------------------------
#
# Three modes are exercised: non-streaming JSON, streaming with usage in
# the final SSE frame (OpenAI's stream_options.include_usage shape), and
# streaming without usage (falls back to delta-event counting).


class _ProgrammableUpstream:
    """Fake llama-swap that replays a scripted response."""

    def __init__(self, port: int, *, mode: str, model: str = "fake-model"):
        self.port = port
        self.mode = mode
        self.model = model

        outer = self

        class Handler(BaseHTTPRequestHandler):
            @override
            def log_message(self, *_a, **_k):  # noqa: A002
                pass

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if outer.mode == "nonstream":
                    outer._reply_nonstream(self)
                elif outer.mode == "stream_with_usage":
                    outer._reply_stream(self, include_usage=True)
                elif outer.mode == "stream_no_usage":
                    outer._reply_stream(self, include_usage=False)
                else:
                    self.send_error(500)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        # poll_interval=0.05 keeps teardown fast; the default 0.5 makes
        # every fixture stop() block ~500ms and dominates the suite.
        self._thread = threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.05), daemon=True)
        self._thread.start()

    def _reply_nonstream(self, handler: BaseHTTPRequestHandler) -> None:
        body = json.dumps(
            {
                "model": self.model,
                "choices": [{"message": {"content": "hello world"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 42, "total_tokens": 49},
            }
        ).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _reply_stream(self, handler: BaseHTTPRequestHandler, *, include_usage: bool) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        # Emit three delta-content frames, then either a usage frame or nothing.
        for i in range(3):
            evt = {"model": self.model, "choices": [{"delta": {"content": f"tok{i}"}}]}
            handler.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
            handler.wfile.flush()
        if include_usage:
            usage_evt = {
                "model": self.model,
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 42, "total_tokens": 47},
            }
            handler.wfile.write(f"data: {json.dumps(usage_evt)}\n\n".encode())
            handler.wfile.flush()
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _run_chat_request(public_port: int, *, stream: bool) -> None:
    """Fire one /v1/chat/completions request and drain it (proxy emits telemetry on finish)."""
    payload = {
        "model": "fake-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }
    with requests.post(
        f"http://127.0.0.1:{public_port}/v1/chat/completions",
        json=payload,
        stream=stream,
        timeout=15,
    ) as resp:
        assert resp.status_code == 200
        if stream:
            for _ in resp.iter_content(chunk_size=64):
                pass
        else:
            _ = resp.content


def _make_proxy(upstream_port: int, captured: list[Any]) -> tuple[ServingProxy, int]:
    public_port = _free_port()
    proxy = ServingProxy(
        public_port=public_port,
        upstream_port=upstream_port,
        allowed_origins=[],
        on_telemetry=captured.append,
        host="127.0.0.1",
    )
    proxy.start()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", public_port)) == 0:
                break
        time.sleep(0.02)
    return proxy, public_port


def test_telemetry_non_streaming_uses_usage_block():
    """Non-streaming JSON response: tokens_generated comes from usage.completion_tokens."""
    upstream = _ProgrammableUpstream(_free_port(), mode="nonstream")
    captured: list[dict[str, Any]] = []
    proxy, public_port = _make_proxy(upstream.port, captured)
    try:
        _run_chat_request(public_port, stream=False)
        # Telemetry fires in the finally block of do_POST — after the
        # response is closed. Wait briefly for it to land.
        deadline = time.time() + 2.0
        while time.time() < deadline and not captured:
            time.sleep(0.02)
        assert len(captured) == 1, f"expected exactly one telemetry record; got {len(captured)}"
        rec = captured[0]
        # Same metrics shape the log-parse strategy used to emit:
        assert rec["model"] == "fake-model"
        assert rec["tokens_generated"] == 42  # ← from usage.completion_tokens
        assert rec["tokens_prompt"] == 7
        assert rec["token_source"] == "usage"
        assert rec["stream"] is False
        assert rec["duration_seconds"] > 0
        assert rec["source"] == "serving_proxy"
    finally:
        proxy.stop()
        upstream.stop()


def test_telemetry_streaming_with_usage_pulls_from_final_frame():
    """Streaming + include_usage: tokens_generated pulled from the final SSE usage frame."""
    upstream = _ProgrammableUpstream(_free_port(), mode="stream_with_usage")
    captured: list[dict[str, Any]] = []
    proxy, public_port = _make_proxy(upstream.port, captured)
    try:
        _run_chat_request(public_port, stream=True)
        deadline = time.time() + 2.0
        while time.time() < deadline and not captured:
            time.sleep(0.02)
        assert len(captured) == 1
        rec = captured[0]
        assert rec["model"] == "fake-model"
        # Usage frame is authoritative — should override the delta-chunk count of 3.
        assert rec["tokens_generated"] == 42
        assert rec["tokens_prompt"] == 5
        assert rec["token_source"] == "usage"
        assert rec["stream"] is True
        assert rec["duration_seconds"] > 0
    finally:
        proxy.stop()
        upstream.stop()


def test_telemetry_streaming_without_usage_falls_back_to_delta_count():
    """Streaming + no include_usage: tokens_generated = number of delta.content events."""
    upstream = _ProgrammableUpstream(_free_port(), mode="stream_no_usage")
    captured: list[dict[str, Any]] = []
    proxy, public_port = _make_proxy(upstream.port, captured)
    try:
        _run_chat_request(public_port, stream=True)
        deadline = time.time() + 2.0
        while time.time() < deadline and not captured:
            time.sleep(0.02)
        assert len(captured) == 1
        rec = captured[0]
        assert rec["model"] == "fake-model"
        # Three delta-content frames → approximate token count of 3.
        assert rec["tokens_generated"] == 3
        assert rec["token_source"] == "delta_count"
        assert rec["stream"] is True
    finally:
        proxy.stop()
        upstream.stop()


def test_telemetry_off_means_no_buffering_and_no_callback(fake_upstream):
    """on_telemetry=None: pass-through, no per-response parsing, no callback."""
    public_port = _free_port()
    captured: list[dict[str, Any]] = []
    proxy = ServingProxy(
        public_port=public_port,
        upstream_port=fake_upstream.port,
        allowed_origins=[],
        on_telemetry=None,
        host="127.0.0.1",
    )
    proxy.start()
    try:
        # The existing _FakeUpstream serves a streaming chat completions
        # response; we just need to drive a request through and see no
        # callback fires.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", public_port)) == 0:
                    break
            time.sleep(0.02)
        with requests.post(
            f"http://127.0.0.1:{public_port}/v1/chat/completions",
            json={"model": "x", "messages": []},
            stream=True,
            timeout=10,
        ) as resp:
            for _ in resp.iter_content(chunk_size=64):
                pass
        # Wait briefly to confirm nothing fires.
        time.sleep(0.2)
        assert captured == []
    finally:
        proxy.stop()
