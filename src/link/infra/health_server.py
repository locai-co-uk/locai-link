# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tiny loopback HTTP server exposing the agent's live state."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from typing_extensions import override

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

    def __init__(
        self,
        version: str | None,
        models_provider: Callable[[], list[dict[str, Any]]] | None = None,
        command_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.version = version or "unknown"
        self._boot_time = time.monotonic()
        self.currently_serving = False
        self.model_id: str | None = None
        # Runtime hands us a callable that snapshots pipeline state on
        # demand. Kept as a callable (not a stored list) so the /models
        # response always reflects "now" without the runtime having to
        # push updates on every pipeline mutation.
        self._models_provider = models_provider
        # Runtime also hands us its command dispatch entry point. The
        # POST /models/... handlers build a command dict (same schema
        # as commands over the Zenoh wire) and call this — the runtime
        # then routes through its normal validation + dispatch path,
        # so the loopback API and the backend share one code path.
        self._command_handler = command_handler

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "uptime_seconds": int(time.monotonic() - self._boot_time),
            "currently_serving": self.currently_serving,
            "model_id": self.model_id,
        }

    def models(self) -> list[dict[str, Any]]:
        """Fresh snapshot of servable-model pipelines. Empty when no
        provider is wired (e.g. tests that construct HealthState
        standalone)."""
        if self._models_provider is None:
            return []
        return self._models_provider()

    def has_command_handler(self) -> bool:
        """Whether a runtime command handler is wired. `False` in test
        harnesses that construct HealthState without dispatch."""
        return self._command_handler is not None

    def dispatch(self, command: dict[str, Any]) -> None:
        """Route ``command`` to the wired runtime handler.

        Precondition: `has_command_handler()` is True. Callers gate on
        it so the 503 "not wired" vs 500 "handler raised" distinction
        in `do_POST` stays clean.
        """
        assert self._command_handler is not None, "dispatch called without a wired handler"
        self._command_handler(command)

    def set_serving(self, model_id: str | None) -> None:
        """Mark the agent as serving ``model_id``, or idle when ``None``."""
        if model_id is None:
            self.currently_serving = False
            self.model_id = None
        else:
            self.currently_serving = True
            self.model_id = model_id


_MODEL_ACTION_RE = re.compile(r"^/models/([^/]+)/(serve|stop-serving)$")


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                body = state.snapshot()
            elif self.path == "/models":
                body = {"models": state.models()}
            else:
                self.send_error(404, "Not Found")
                return
            payload = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                # Client closed the socket mid-write (common for a poller
                # that reads the response and moves on) — ignore.
                pass

        def do_POST(self) -> None:  # noqa: N802
            match = _MODEL_ACTION_RE.match(self.path)
            if not match:
                self.send_error(404, "Not Found")
                return
            if not state.has_command_handler():
                self.send_error(503, "Command handler not wired")
                return
            pipeline_id, action = match.group(1), match.group(2)
            # We read the pipeline's current args from the models
            # snapshot instead of accepting overrides in the POST
            # body. Rationale: the intent of a loopback toggle is
            # "resume serving this thing with its existing config" —
            # a port/host change is a structural config edit that
            # belongs in Control, not a one-shot local POST.
            model = next((m for m in state.models() if m["id"] == pipeline_id), None)
            if model is None:
                self.send_error(404, f"Unknown pipeline: {pipeline_id}")
                return

            command: dict[str, Any]
            if action == "serve":
                command = {
                    "id": f"loopback-{uuid.uuid4().hex[:8]}",
                    "type": "START_SERVING",
                    "pipeline_id": pipeline_id,
                    # Fall back to the StartServingCommand defaults if
                    # the config hasn't set them yet (freshly-deployed
                    # pipelines without prior serve).
                    "port": model.get("port") or 8100,
                    "host": model.get("host") or "0.0.0.0",
                    "model_display_name": model.get("alias") or pipeline_id,
                }
            else:  # stop-serving
                command = {
                    "id": f"loopback-{uuid.uuid4().hex[:8]}",
                    "type": "STOP_SERVING",
                    "pipeline_id": pipeline_id,
                }

            try:
                state.dispatch(command)
            except Exception as exc:
                logger.warning(f"Command handler raised on POST {self.path}: {exc}")
                self.send_error(500, "Command dispatch failed")
                return

            # 202 Accepted — dispatch fired; the caller polls /models
            # to observe the state change on the next tick. We don't
            # block on the runtime finishing.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        @override
        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            # /healthz and /models are polled every few seconds — the
            # default access log would drown out real events. Only
            # log_request (successful access lines) is silenced; errors
            # still flow through log_error → log_message → stderr, so
            # send_error(404/500/...) responses are still surfaced.
            pass

    return HealthHandler


class HealthServer:
    """Wraps the threaded HTTP server so callers can stop it cleanly."""

    def __init__(self, state: HealthState, host: str = HEALTH_HOST, port: int = HEALTH_PORT) -> None:
        self.state = state
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

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
        # poll_interval bounds the max blocking wait inside stop() —
        # tests that spin the server up and down pay this per teardown.
        # 50ms is still well below any human-perceptible shutdown delay
        # in production and cuts the test suite by several seconds.
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.05) if self._server else None,
            name="health-server",
            daemon=True,
        )
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
