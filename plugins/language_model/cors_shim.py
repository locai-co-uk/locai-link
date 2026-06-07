# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Browser-facing CORS / streaming proxy in front of llama-swap.

Why this exists
---------------
llama-swap doesn't answer CORS preflights and doesn't set
``Access-Control-*`` headers. Browser-direct partner integrations
(SafeChat, Tauri webviews, etc.) require both, plus Chrome 142+'s Local
Network Access permission grant. This shim sits on the public port the
browser already targets and forwards every request to llama-swap on a
loopback-only port, attaching CORS headers and answering preflights.

Port arrangement
----------------
The shim owns the **public** port — browsers and CLI users keep targeting
the port they were always told about. llama-swap is moved to a loopback
internal port one offset away (see ``SwapManager._SHIM_OFFSET``).

    public_port    -> CorsShim (browser-facing, CORS)
    upstream_port  -> llama-swap on 127.0.0.1 only
    upstream_port + 100 + i -> llama-server per model (internal)

CLI / native HTTP clients are unaffected: requests without an ``Origin``
header are forwarded transparently with no CORS headers attached (no need
— they're not subject to browser same-origin enforcement). Browser clients
must be in the allowlist to receive the echoed ``Access-Control-Allow-Origin``.

The allowlist is hard-coded for the two well-known clients (SafeChat prod
+ ``localhost:3000`` for local dev) AND env-overridable via the
``LOCAI_CORS_ALLOWED_ORIGINS`` env var (comma-separated). Env override
exists so partners on staging/feature-flag environments don't need a Link
release to register their origin.

Streaming semantics
-------------------
``POST /v1/chat/completions`` forwards with ``stream=True`` and re-emits
chunks via ``iter_content(chunk_size=None)`` so each upstream chunk is
flushed to the client immediately — no buffering. ``Connection: close``
on responses simplifies the stream lifecycle (each chat completion is
its own TCP connection).

If the client disconnects mid-stream, we catch ``BrokenPipeError`` /
``ConnectionResetError`` and call ``response.close()`` to release
llama-swap's single ``maxConcurrent`` slot — otherwise a cancelled stream
would wedge the swap until model TTL expiry.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

logger = logging.getLogger(__name__)


# Default allowlist — well-known browser clients. Adding here requires a
# Link release; for ad-hoc additions use the LOCAI_CORS_ALLOWED_ORIGINS
# env var.
_DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "https://safechat.locai.co.uk",
    "https://dev.safechat.locai.co.uk",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def _resolve_allowed_origins(extra: list[str] | None = None) -> set[str]:
    """Default allowlist + LOCAI_CORS_ALLOWED_ORIGINS env override + caller extras."""
    origins: set[str] = set(_DEFAULT_ALLOWED_ORIGINS)
    env = os.environ.get("LOCAI_CORS_ALLOWED_ORIGINS", "")
    if env:
        origins.update(o.strip() for o in env.split(",") if o.strip())
    if extra:
        origins.update(extra)
    return origins


class _ShimServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying shim-scoped config to handlers.

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
    ) -> None:
        super().__init__(server_address, _ShimHandler)
        self.upstream_base_url = upstream_base_url
        self.allowed_origins = allowed_origins


class _ShimHandler(BaseHTTPRequestHandler):
    """OPTIONS / GET / POST endpoints — everything else returns 404.

    Surface intentionally minimal: only the three llama-swap endpoints
    SafeChat actually hits. Adding routes here is a deliberate design
    decision, not a "throw a kitchen sink in front" — we don't want this
    to become a general HTTP proxy.
    """

    # Suppress BaseHTTPRequestHandler's default per-request stderr log spam;
    # route through Python logging instead so the shim respects link's
    # logging config.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("[cors_shim] %s - %s", self.address_string(), format % args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _shim_server(self) -> _ShimServer:
        # self.server is typed as `BaseServer` by stdlib; assert for clarity.
        assert isinstance(self.server, _ShimServer)
        return self.server

    def _resolve_echo_origin(self) -> str | None:
        """Origin to echo back in ACAO, or None for non-browser/disallowed callers.

        Requests without an Origin header (curl, Python requests, native
        clients) deliberately return None — they're not subject to CORS,
        so omitting the header is correct AND prevents header pollution.
        """
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return None
        if origin in self._shim_server().allowed_origins:
            return origin
        logger.debug("[cors_shim] rejecting origin %r (not in allowlist)", origin)
        return None

    def _send_cors_response_headers(self, echo_origin: str | None) -> None:
        """Standard ACAO + Vary pair sent on every non-preflight response."""
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
        """send_error that swallows write failures to an already-closed socket.

        A status poll whose client has navigated away (or a poll that arrives
        in the brief window between llama-swap stopping and the shim being
        torn down) leaves us writing the error body to a dead socket.
        BaseHTTPRequestHandler would let the resulting OSError escape the
        worker thread and dump a multi-frame traceback to stderr (the
        ``WinError 10053`` noise). Log one line instead.
        """
        try:
            self.send_error(code, message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            logger.info("[cors_shim] could not send %d to client: %s", code, exc)

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
        """Forward small JSON endpoints used by partner status/discovery."""
        if self.path not in ("/v1/models", "/health"):
            self.send_error(404, "Not Found")
            return

        echo_origin = self._resolve_echo_origin()
        upstream = self._shim_server().upstream_base_url + self.path

        try:
            resp = requests.get(upstream, timeout=5, headers=self._forward_headers())
        except requests.RequestException as exc:
            logger.warning("[cors_shim] GET %s upstream failed: %s", self.path, exc)
            self._safe_send_error(502, "Upstream unavailable")
            return

        try:
            self.send_response(resp.status_code)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(resp.content)))
            self._send_cors_response_headers(echo_origin)
            self.end_headers()
            self.wfile.write(resp.content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.info("[cors_shim] client disconnected during GET %s", self.path)

    def do_POST(self) -> None:  # noqa: N802
        """Streaming chat completions. The hot path."""
        if self.path != "/v1/chat/completions":
            self.send_error(404, "Not Found")
            return

        echo_origin = self._resolve_echo_origin()
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        upstream = self._shim_server().upstream_base_url + self.path

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
            logger.warning("[cors_shim] POST %s upstream failed: %s", self.path, exc)
            self._safe_send_error(502, "Upstream unavailable")
            return

        self.send_response(resp.status_code)
        # Force SSE-shaped headers so any intermediary (proxy, CDN, dev
        # server) treats this as a stream — not all upstream responses set
        # text/event-stream cleanly, so we standardise here.
        self.send_header(
            "Content-Type",
            resp.headers.get("Content-Type", "text/event-stream"),
        )
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        # nginx-specific hint; harmless for other intermediaries.
        self.send_header("X-Accel-Buffering", "no")
        self._send_cors_response_headers(echo_origin)
        self.end_headers()

        # Stream chunks as they arrive. Two non-obvious details:
        #
        # 1. iter_content(chunk_size=None) calls urllib3's stream(amt=None)
        #    which buffers the ENTIRE response before yielding — wrong.
        #
        # 2. iter_content(chunk_size=N>0) calls urllib3's stream(amt=N)
        #    which calls http.client.HTTPResponse.read(N). Per the stdlib
        #    docs that *should* return up to N bytes immediately, but in
        #    practice with chunked transfer encoding it can block until
        #    a full N bytes accumulate. To bypass this we go one layer
        #    deeper and use raw.read1 / raw.stream, which yields as bytes
        #    arrive on the socket regardless of HTTP framing boundaries.
        #
        # resp.raw.read1(size) is what urllib3 exposes for "give me what
        # you have right now"; the read1() semantics match os.read().
        # decode_content=False keeps gzip/etc. transparent (upstream
        # doesn't compress SSE anyway, but no need to decode).
        try:
            while True:
                chunk = resp.raw.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client disconnected mid-stream — common when the user
            # navigates away or hits Stop. (On Windows this surfaces as
            # ConnectionAbortedError / WinError 10053.) Drop the upstream
            # connection so llama-swap's maxConcurrent slot frees immediately
            # rather than at TTL expiry.
            logger.info("[cors_shim] client disconnected mid-stream; closing upstream")
        finally:
            resp.close()


class CorsShim:
    """Lifecycle wrapper around _ShimServer.

    Usage::

        shim = CorsShim(public_port=8100, upstream_port=8150, host="0.0.0.0")
        shim.start()
        ...
        shim.stop()

    Idempotent ``start()`` / ``stop()`` — calling them when already in
    the target state is a no-op. Survives upstream restarts: the shim
    only cares about its own listen socket; upstream availability is
    a per-request concern surfaced as 502s.
    """

    def __init__(
        self,
        public_port: int,
        upstream_port: int,
        host: str = "0.0.0.0",
        extra_allowed_origins: list[str] | None = None,
    ) -> None:
        self.public_port = int(public_port)
        self.upstream_port = int(upstream_port)
        self.host = host
        self._allowed_origins = _resolve_allowed_origins(extra_allowed_origins)
        self._server: _ShimServer | None = None
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
                # If a healthy shim is already on our port, reuse it —
                # mirrors swap_manager's reuse pattern after unclean shutdown.
                # We don't try to verify it's actually our shim (vs some
                # other process); the caller can stop us and we'll exit
                # cleanly without touching whatever else owns the port.
                logger.warning(
                    "[cors_shim] port %d already in use; refusing to start. "
                    "Send STOP_SERVING and retry if you need a clean restart.",
                    self.public_port,
                )
                return

            upstream = f"http://127.0.0.1:{self.upstream_port}"
            self._server = _ShimServer(
                (self.host, self.public_port),
                upstream_base_url=upstream,
                allowed_origins=self._allowed_origins,
            )
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name=f"cors-shim-{self.public_port}",
            )
            self._thread.start()
            logger.info(
                "CORS shim listening on http://%s:%d -> %s (allowlist: %d origins)",
                self.host,
                self.public_port,
                upstream,
                len(self._allowed_origins),
            )

    def stop(self) -> None:
        with self._lock:
            if self._server is None:
                return
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                logger.warning("[cors_shim] shutdown raised: %s", exc)
            self._server = None
            self._thread = None
            logger.info("CORS shim on port %d stopped", self.public_port)

    def is_running(self) -> bool:
        with self._lock:
            return self._server is not None and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _port_in_use(self) -> bool:
        # Mirrors SwapManager._port_in_use — connect_ex returns 0 if the
        # port is accepting connections, anything else if nothing's there.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", self.public_port)) == 0
