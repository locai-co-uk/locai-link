# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

logger = logging.getLogger(__name__)


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
    ) -> None:
        super().__init__(server_address, _ProxyHandler)
        self.upstream_base_url = upstream_base_url
        self.allowed_origins = allowed_origins


class _ProxyHandler(BaseHTTPRequestHandler):
    """OPTIONS / GET / POST endpoints — everything else returns 404.

    Surface intentionally minimal: only the three llama-swap endpoints
    used by browser clients today (preflight, model list, chat
    completions). Adding routes here is a deliberate design decision,
    not a "throw a kitchen sink in front" — we don't want this to become
    a general HTTP proxy.
    """

    # Suppress BaseHTTPRequestHandler's default per-request stderr log spam;
    # route through Python logging instead so the proxy respects link's
    # logging config.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("[cors_proxy] %s - %s", self.address_string(), format % args)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _proxy_server(self) -> _ProxyServer:
        assert isinstance(self.server, _ProxyServer)
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
        if origin in self._proxy_server().allowed_origins:
            return origin
        logger.debug("[cors_proxy] rejecting origin %r (not in allowlist)", origin)
        return None

    def _send_cors_response_headers(self, echo_origin: str | None) -> None:
        """Standard ACAO + Vary pair sent on every non-preflight response."""
        if echo_origin:
            if "\r" in echo_origin or "\n" in echo_origin:
                logger.warning("[cors_proxy] refused unsafe origin value for ACAO")
            else:
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
            logger.info("[cors_proxy] could not send %d to client: %s", code, exc)

    def _safe_content_type(self, raw: str | None, default: str) -> str:
        """Return a header-safe Content-Type, stripping CR/LF from upstream values."""
        if not raw:
            return default
        sanitized = raw.replace("\r", "").replace("\n", "").strip()
        return sanitized or default

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
            logger.warning("[cors_proxy] GET %s upstream failed: %s", self.path, exc)
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
            logger.info("[cors_proxy] client disconnected during GET %s", self.path)

    def do_POST(self) -> None:  # noqa: N802
        """Streaming chat completions. The hot path."""
        if self.path != "/v1/chat/completions":
            self.send_error(404, "Not Found")
            return

        echo_origin = self._resolve_echo_origin()
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        upstream = self._proxy_server().upstream_base_url + self.path

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
            logger.warning("[cors_proxy] POST %s upstream failed: %s", self.path, exc)
            self._safe_send_error(502, "Upstream unavailable")
            return

        self.send_response(resp.status_code)
        # Force SSE-shaped headers so any intermediary (proxy, CDN, dev
        # server) treats this as a stream — not all upstream responses set
        # text/event-stream cleanly, so we standardise here.
        self.send_header(
            "Content-Type",
            self._safe_content_type(resp.headers.get("Content-Type"), "text/event-stream"),
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
        #    a full N bytes accumulate.
        try:
            while True:
                chunk = resp.raw.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client disconnected mid-stream — common when the user navigates
            # away or hits Stop. (On Windows: ConnectionAbortedError /
            # WinError 10053.) Drop the upstream so llama-swap's
            # maxConcurrent slot frees immediately rather than at TTL expiry.
            logger.info("[cors_proxy] client disconnected mid-stream; closing upstream")
        finally:
            resp.close()


class CorsProxy:
    """Lifecycle wrapper around _ProxyServer.

    Usage::

        proxy = CorsProxy(
            public_port=8100,
            upstream_port=8150,
            allowed_origins=["https://app.example.com"],
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
        allowed_origins: list[str] | set[str],
        host: str = "0.0.0.0",
    ) -> None:
        self.public_port = int(public_port)
        self.upstream_port = int(upstream_port)
        self.host = host
        self._allowed_origins: set[str] = {o for o in allowed_origins if o}
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
                logger.warning(
                    "[cors_proxy] port %d already in use; refusing to start. "
                    "Stop the existing listener and retry if you need a clean restart.",
                    self.public_port,
                )
                return

            upstream = f"http://127.0.0.1:{self.upstream_port}"
            self._server = _ProxyServer(
                (self.host, self.public_port),
                upstream_base_url=upstream,
                allowed_origins=self._allowed_origins,
            )
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name=f"cors-proxy-{self.public_port}",
            )
            self._thread.start()
            logger.info(
                "CORS proxy listening on http://%s:%d -> %s (allowlist: %d origins)",
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
                logger.warning("[cors_proxy] shutdown raised: %s", exc)
            self._server = None
            self._thread = None
            logger.info("CORS proxy on port %d stopped", self.public_port)

    def is_running(self) -> bool:
        with self._lock:
            return self._server is not None and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", self.public_port)) == 0
