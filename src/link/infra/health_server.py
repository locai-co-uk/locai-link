# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tiny loopback HTTP server exposing the agent's live state.

Polled by the menu-bar companion app every few seconds to drive the
green/grey indicator and report which model (if any) is being served.

Deliberately separate from `ServingProxy`:
    * `ServingProxy` only exists while a model is being served — it
      can't answer "is the agent up" when no model is loaded.
    * The agent state machine and the proxy live in different layers
      (agent runtime vs. language_model plugin); coupling them would
      drag plugin state into the runtime or vice versa.

Bound to ``127.0.0.1`` only. The companion app runs as the same user
and reaches the loopback interface; nothing off-host has any business
hitting this endpoint.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

logger = logging.getLogger(__name__)

HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 8101


class HealthState:
    """Mutable agent state snapshotted by the HTTP handler on each GET.

    Held as a single object so :class:`AgentRuntime` can mutate it from
    any thread (command dispatch, shutdown) without coordinating with
    the handler. The handler reads each field independently — a minor
    race during a serve-start/stop is harmless because the next poll
    catches up within seconds.
    """

    def __init__(self, version: str | None) -> None:
        self.version = version or "unknown"
        self._boot_time = time.monotonic()
        self.currently_serving = False
        self.model_id: Optional[str] = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "uptime_seconds": int(time.monotonic() - self._boot_time),
            "currently_serving": self.currently_serving,
            "model_id": self.model_id,
        }

    def set_serving(self, model_id: Optional[str]) -> None:
        """Mark the agent as serving ``model_id``, or idle when ``None``."""
        if model_id is None:
            self.currently_serving = False
            self.model_id = None
        else:
            self.currently_serving = True
            self.model_id = model_id


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/healthz":
                self.send_error(404, "Not Found")
                return
            payload = json.dumps(state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                # Companion polled and closed its socket — fine, ignore.
                pass

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # /healthz is polled every few seconds — the default access
            # log would drown out real events. Errors still surface via
            # log_error (which we don't override).
            pass

    return HealthHandler


class HealthServer:
    """Wraps the threaded HTTP server so callers can stop it cleanly."""

    def __init__(self, state: HealthState, host: str = HEALTH_HOST, port: int = HEALTH_PORT) -> None:
        self.state = state
        self.host = host
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._server is not None:
            return
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), _make_handler(self.state))
        except OSError as exc:
            # Port already in use — most likely a stale agent. We don't
            # raise because the agent's own startup shouldn't fail over
            # a non-critical telemetry endpoint.
            logger.warning(f"Health server could not bind to {self.host}:{self.port}: {exc}")
            self._server = None
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="health-server", daemon=True)
        self._thread.start()
        logger.info(f"Health server listening on http://{self.host}:{self.port}/healthz")

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None
