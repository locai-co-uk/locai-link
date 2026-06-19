# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Reverse proxy in front of llama-swap. Always in path.

Two optional features, independent:

- ``allowed_origins`` (non-empty) → emits ACAO + Chrome 142+ Local Network
  Access preflight headers for browser clients.
- ``on_telemetry`` → per ``POST /v1/chat/completions``, parses ``usage`` from
  the response (JSON body or final SSE frame) and fires one record on the
  callback. Falls back to counting ``delta.content`` events when the client
  didn't request ``stream_options.include_usage``.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

logger = logging.getLogger(__name__)


TelemetryRecord = dict
TelemetryCallback = Callable[[TelemetryRecord], None]


class _ProxyServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying proxy-scoped config to handlers.

    Threading is required (not the single-threaded HTTPServer): a long-
    running streaming POST must not block /health and /v1/models polls
    from a partner UI's status panel running in parallel.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        upstream_base_url: str,
        allowed_origins: set[str],
        on_telemetry: TelemetryCallback | None,
    ) -> None:
        super().__init__(server_address, _ProxyHandler)
        self.upstream_base_url = upstream_base_url
        self.allowed_origins = allowed_origins
        self.on_telemetry = on_telemetry


class _ChatTelemetry:
    """Per-request state for one ``/v1/chat/completions`` call.

    Holds the response-side parser for both streaming (SSE) and
    non-streaming (single JSON body) shapes. Token accounting prefers
    the OpenAI ``usage`` block; falls back to counting ``delta.content``
    events when usage isn't emitted.
    """

    def __init__(self, request_body: bytes) -> None:
        self.started_at = datetime.now()
        self._perf_start = time.perf_counter()
        self._sse_buf = bytearray()
        self._content_chunks = 0
        self._usage: dict | None = None
        self._model: str | None = None
        self._stream = False
        try:
            req = json.loads(request_body) if request_body else {}
            if isinstance(req, dict):
                self._stream = bool(req.get("stream"))
                self._model = req.get("model")
        except json.JSONDecodeError:
            # Best-effort metadata only — a malformed request body is the
            # upstream's problem to reject, not ours to crash on.
            pass

    # ------------------------------------------------------------------
    # Streaming SSE — parse frames as bytes arrive
    # ------------------------------------------------------------------

    def ingest_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._sse_buf.extend(chunk)
        # SSE events are separated by a blank line. The spec allows either
        # `\n\n` (LF, what llama-server emits) or `\r\n\r\n` (CRLF, used by
        # other OpenAI-compatible servers and some proxies). Drain every
        # complete event whichever delimiter the upstream picked; whatever's
        # left is a partial event waiting for the next chunk's bytes.
        while True:
            lf = self._sse_buf.find(b"\n\n")
            crlf = self._sse_buf.find(b"\r\n\r\n")
            # Pick the earliest delimiter that's present.
            if lf < 0 and crlf < 0:
                return
            if crlf < 0 or (lf >= 0 and lf < crlf):
                sep, sep_len = lf, 2
            else:
                sep, sep_len = crlf, 4
            event = bytes(self._sse_buf[:sep])
            del self._sse_buf[: sep + sep_len]
            self._consume_event(event)

    def _consume_event(self, event: bytes) -> None:
        for line in event.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            self._absorb(obj)

    # ------------------------------------------------------------------
    # Non-streaming — single JSON body
    # ------------------------------------------------------------------

    def ingest_full_response(self, body: bytes) -> None:
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return
        if isinstance(obj, dict):
            self._absorb(obj)

    # ------------------------------------------------------------------
    # Shared absorber: pulls usage / model / counts from any
    # chat-completions JSON shape we know about.
    # ------------------------------------------------------------------

    def _absorb(self, obj: dict) -> None:
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        # NOTE: we deliberately don't update self._model from the response. The
        # request body's model is the canonical id (what the client asked for —
        # the pipeline_id UUID). The response echoes the llama-server file stem,
        # which doesn't match the adapter's id and breaks per-model attribution.
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and delta.get("content"):
                self._content_chunks += 1

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    def build_record(self) -> TelemetryRecord:
        ended_at = datetime.now()
        duration_seconds = time.perf_counter() - self._perf_start
        if self._usage:
            tokens_generated = int(self._usage.get("completion_tokens") or 0)
            tokens_prompt = int(self._usage.get("prompt_tokens") or 0)
            token_source = "usage"
        else:
            # SSE delta count when the client didn't request include_usage.
            # llama.cpp emits ~one delta event per token but multi-byte
            # tokens may split; treat this as approximate.
            tokens_generated = self._content_chunks
            tokens_prompt = 0
            token_source = "delta_count"
        return {
            "model": self._model,
            "stream": self._stream,
            "start_time": self.started_at,
            "end_time": ended_at,
            "duration_seconds": duration_seconds,
            "tokens_generated": tokens_generated,
            "tokens_prompt": tokens_prompt,
            "token_source": token_source,
            "source": "serving_proxy",
        }


class _ProxyHandler(BaseHTTPRequestHandler):
    """OPTIONS / GET / POST endpoints — everything else returns 404.

    Surface intentionally minimal: only the llama-swap endpoints used by
    browser and native clients (preflight, model list, health, chat
    completions). Adding routes here is a deliberate design decision,
    not a "throw a kitchen sink in front" — we don't want this to become
    a general HTTP proxy.
    """

    # Suppress BaseHTTPRequestHandler's default per-request stderr log spam;
    # route through Python logging instead so the proxy respects link's
    # logging config.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("[serving_proxy] %s - %s", self.address_string(), format % args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _proxy_server(self) -> _ProxyServer:
        assert isinstance(self.server, _ProxyServer)
        return self.server

    @staticmethod
    def _sanitize_header_value(raw: str | None) -> str:
        """Strip CR/LF + surrounding whitespace from a value before it touches a header.

        Header injection (HTTP response splitting) defense: any value that
        ultimately reaches ``send_header`` MUST be free of CR/LF. The
        allowlist check below also prevents the attack — no legitimate
        Origin contains CR/LF, so a tampered one can't match. Sanitizing
        here is belt-and-braces and lets the static analyzer prove safety
        from input to output.
        """
        if not raw:
            return ""
        return raw.replace("\r", "").replace("\n", "").strip()

    def _resolve_echo_origin(self) -> str | None:
        """Origin to echo back in ACAO, or None for non-browser/disallowed callers.

        Requests without an Origin header (curl, Python requests, native
        clients) deliberately return None — they're not subject to CORS,
        so omitting the header is correct AND prevents header pollution.
        """
        origin = self._sanitize_header_value(self.headers.get("Origin"))
        if not origin:
            return None
        if origin in self._proxy_server().allowed_origins:
            return origin
        logger.debug("[serving_proxy] rejecting origin %r (not in allowlist)", origin)
        return None

    def _send_cors_response_headers(self, echo_origin: str | None) -> None:
        """Standard ACAO + Vary pair sent on every non-preflight response.

        ``echo_origin`` is always pre-sanitized via ``_resolve_echo_origin``
        + ``_sanitize_header_value`` so it's safe to pass straight to
        ``send_header`` here.
        """
        if echo_origin:
            self.send_header("Access-Control-Allow-Origin", echo_origin)
        # Always Vary on Origin even when no ACAO is sent — tells caches
        # the response varies depending on the request Origin, so a CDN
        # or shared cache doesn't serve a stale cross-origin response.
        self.send_header("Vary", "Origin")

    def _forward_headers(self) -> dict[str, str]:
        """Subset of request headers to forward upstream.

        Pass Content-Type (llama-swap reads it) and Authorization (for
        forward-compatibility with the deferred per-serving-token work).
        Everything else is hop-by-hop or browser-specific.
        """
        out: dict[str, str] = {}
        ct = self.headers.get("Content-Type")
        if ct:
            out["Content-Type"] = ct
        auth = self.headers.get("Authorization")
        if auth:
            out["Authorization"] = auth
        return out

    def _safe_send_error(self, code: int, message: str) -> None:
        """send_error that swallows write failures to an already-closed socket."""
        try:
            self.send_error(code, message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            logger.info("[serving_proxy] could not send %d to client: %s", code, exc)

    def _safe_content_type(self, raw: str | None, default: str) -> str:
        """Return a header-safe Content-Type, stripping CR/LF from upstream values."""
        if not raw:
            return default
        sanitized = raw.replace("\r", "").replace("\n", "").strip()
        return sanitized or default

    def _fire_telemetry(self, recorder: _ChatTelemetry | None) -> None:
        if recorder is None:
            return
        on_telemetry = self._proxy_server().on_telemetry
        if on_telemetry is None:
            return
        try:
            on_telemetry(recorder.build_record())
        except Exception as exc:  # noqa: BLE001 — never let observability break the request
            logger.debug("[serving_proxy] telemetry callback failed: %s", exc)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802 — stdlib API
        """CORS preflight. Any path; browser doesn't care which.

        Chrome 142+'s Local Network Access requires
        ``Access-Control-Allow-Private-Network: true`` on the preflight
        OR a user-granted permission. Without this header (and the user
        accepting the prompt) the browser drops the actual request.
        """
        echo_origin = self._resolve_echo_origin()
        self.send_response(204)
        self._send_cors_response_headers(echo_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type, authorization")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        # Cache the preflight for a day so subsequent chat messages skip the
        # OPTIONS round-trip. Browsers cap this at their own ceiling (Chrome
        # 2h, Firefox 24h) — sending the upper bound is harmless.
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        """Forward small JSON endpoints used by web clients for status/discovery."""
        if self.path not in ("/v1/models", "/health"):
            self.send_error(404, "Not Found")
            return

        echo_origin = self._resolve_echo_origin()
        upstream = self._proxy_server().upstream_base_url + self.path

        try:
            resp = requests.get(upstream, timeout=5, headers=self._forward_headers())
        except requests.RequestException as exc:
            logger.warning("[serving_proxy] GET %s upstream failed: %s", self.path, exc)
            self._safe_send_error(502, "Upstream unavailable")
            return

        try:
            self.send_response(resp.status_code)
            self.send_header(
                "Content-Type", self._safe_content_type(resp.headers.get("Content-Type"), "application/json")
            )
            self.send_header("Content-Length", str(len(resp.content)))
            self._send_cors_response_headers(echo_origin)
            self.end_headers()
            self.wfile.write(resp.content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.info("[serving_proxy] client disconnected during GET %s", self.path)

    def do_POST(self) -> None:  # noqa: N802
        """Chat completions. The hot path — and the telemetry capture point."""
        if self.path != "/v1/chat/completions":
            self.send_error(404, "Not Found")
            return

        echo_origin = self._resolve_echo_origin()
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        upstream = self._proxy_server().upstream_base_url + self.path

        recorder = _ChatTelemetry(body) if self._proxy_server().on_telemetry else None

        try:
            resp = requests.post(
                upstream,
                data=body,
                headers=self._forward_headers(),
                stream=True,
                # Generous timeout — chat completions on a 14B model can
                # legitimately run for minutes on cold cache. Per-chunk
                # progress is what the stream is for.
                timeout=600,
            )
        except requests.RequestException as exc:
            logger.warning("[serving_proxy] POST %s upstream failed: %s", self.path, exc)
            self._safe_send_error(502, "Upstream unavailable")
            return

        upstream_ct = resp.headers.get("Content-Type", "") or ""
        is_streaming = upstream_ct.startswith("text/event-stream")

        self.send_response(resp.status_code)
        # Force SSE-shaped headers when upstream returned a stream so any
        # intermediary (proxy, CDN, dev server) treats this as a stream.
        # Non-streaming JSON responses preserve upstream's Content-Type.
        self.send_header(
            "Content-Type",
            self._safe_content_type(upstream_ct, "text/event-stream" if is_streaming else "application/json"),
        )
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        # nginx-specific hint; harmless for other intermediaries.
        self.send_header("X-Accel-Buffering", "no")
        self._send_cors_response_headers(echo_origin)
        self.end_headers()

        # For non-streaming responses we buffer for telemetry parsing.
        # The buffer cost is the size of one JSON response (a few KB) and
        # only matters when on_telemetry is set; pure pass-through deployments
        # never allocate it.
        nonstream_buf: bytearray | None = bytearray() if (recorder and not is_streaming) else None

        # Stream chunks as they arrive. Two non-obvious details:
        #
        # 1. iter_content(chunk_size=None) calls urllib3's stream(amt=None)
        #    which buffers the ENTIRE response before yielding — wrong.
        #
        # 2. iter_content(chunk_size=N>0) calls urllib3's stream(amt=N)
        #    which calls http.client.HTTPResponse.read(N). Per the stdlib
        #    docs that *should* return up to N bytes immediately, but in
        #    practice with chunked transfer encoding it can block until
        #    a full N bytes accumulate.
        try:
            while True:
                chunk = resp.raw.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                if recorder is not None:
                    if is_streaming:
                        recorder.ingest_chunk(chunk)
                    elif nonstream_buf is not None:
                        nonstream_buf.extend(chunk)
            if recorder is not None and nonstream_buf is not None:
                recorder.ingest_full_response(bytes(nonstream_buf))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client disconnected mid-stream — common when the user navigates
            # away or hits Stop. (On Windows: ConnectionAbortedError /
            # WinError 10053.) Drop the upstream so llama-swap's
            # maxConcurrent slot frees immediately rather than at TTL expiry.
            logger.info("[serving_proxy] client disconnected mid-stream; closing upstream")
        finally:
            resp.close()
            # Fire telemetry whether or not the client hung up — a
            # cancelled stream still represents a real inference for
            # whatever was generated before the disconnect.
            self._fire_telemetry(recorder)


class ServingProxy:
    """Lifecycle wrapper around _ProxyServer.

    Usage::

        proxy = ServingProxy(
            public_port=8100,
            upstream_port=8150,
            allowed_origins=["https://app.example.com"],  # optional
            on_telemetry=my_callback,  # optional
            host="0.0.0.0",
        )
        proxy.start()
        ...
        proxy.stop()

    Idempotent ``start()`` / ``stop()`` — calling them when already in the
    target state is a no-op. Survives upstream restarts: the proxy only
    cares about its own listen socket; upstream availability is a
    per-request concern surfaced as 502s.
    """

    def __init__(
        self,
        public_port: int,
        upstream_port: int,
        allowed_origins: list[str] | set[str] | None = None,
        on_telemetry: TelemetryCallback | None = None,
        host: str = "0.0.0.0",
    ) -> None:
        self.public_port = int(public_port)
        self.upstream_port = int(upstream_port)
        self.host = host
        self._allowed_origins: set[str] = {o for o in (allowed_origins or []) if o}
        self._on_telemetry = on_telemetry
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return  # already started
            if self._port_in_use():
                # Fail loudly: callers like SwapManager.ensure_proxy() can't
                # detect a silent return, and readiness probes against the
                # internal upstream port pass independently — so swallowing
                # this would let serving look healthy with no public proxy
                # actually listening.
                raise RuntimeError(
                    f"[serving_proxy] cannot start: port {self.public_port} already in use. "
                    "Stop the existing listener and retry."
                )

            upstream = f"http://127.0.0.1:{self.upstream_port}"
            self._server = _ProxyServer(
                (self.host, self.public_port),
                upstream_base_url=upstream,
                allowed_origins=self._allowed_origins,
                on_telemetry=self._on_telemetry,
            )
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name=f"serving-proxy-{self.public_port}",
            )
            self._thread.start()
            logger.info(
                "Serving proxy on http://%s:%d -> %s (cors=%s, telemetry=%s)",
                self.host,
                self.public_port,
                upstream,
                len(self._allowed_origins) or "off",
                "on" if self._on_telemetry else "off",
            )

    def stop(self) -> None:
        with self._lock:
            if self._server is None:
                return
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                logger.warning("[serving_proxy] shutdown raised: %s", exc)
            self._server = None
            self._thread = None
            logger.info("Serving proxy on port %d stopped", self.public_port)

    def is_running(self) -> bool:
        with self._lock:
            return self._server is not None and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", self.public_port)) == 0
