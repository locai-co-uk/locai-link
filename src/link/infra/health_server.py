# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tiny loopback HTTP server exposing the agent's live state."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from typing_extensions import override

logger = logging.getLogger(__name__)

HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 20505

# Queued deployment rows time out into `failed` after this many seconds
# if the runtime never advances them (e.g. Control DEPLOY_MODEL was lost).
# 5 minutes accommodates slow first-deploy cold starts while capping how
# long the UI can be stuck on "Queued".
QUEUED_TTL_SECONDS = 300

_MODEL_ACTION_RE = re.compile(r"^/models/([^/]+)/(serve|stop-serving|cancel-deploy)$")
_LOOPBACK_HOSTS = frozenset({HEALTH_HOST, "localhost"})


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

        self._models_provider = models_provider
        self._command_handler = command_handler

        self.transport_type: str | None = None
        self.transport_endpoint: str | None = None
        self.transport_connected: bool = False

        self.deployments: dict[str, dict[str, Any]] = {}
        # Protects `deployments` — mutated by worker threads via
        # `set_deployment_progress`, iterated by the handler thread via
        # `snapshot`; without a lock `dict.values()` iteration can raise
        # `RuntimeError: dictionary changed size during iteration`.
        self._deploy_lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        transport: dict[str, Any] | None = None
        if self.transport_type is not None:
            transport = {
                "type": self.transport_type,
                "endpoint": self.transport_endpoint,
                "connected": self.transport_connected,
            }
        # One time.monotonic() call feeds both the uptime field and the
        # queued-row TTL sweep so the two are consistent with each other
        # (and so tests that patch monotonic with a fixed side-effect list
        # don't need to know how many internal calls we make).
        now = time.monotonic()
        # Queued rows older than QUEUED_TTL_SECONDS are flipped to `failed`
        # so the UI can drop the "Queued" spinner — otherwise a dropped
        # Control dispatch leaves it stuck until the runtime restarts.
        with self._deploy_lock:
            for dep in self.deployments.values():
                if dep.get("stage") == "queued" and now - dep.get("created_at", now) > QUEUED_TTL_SECONDS:
                    dep["stage"] = "failed"
                    dep["progress_pct"] = 0.0
            deployments_snapshot = [
                {k: v for k, v in dep.items() if k != "created_at"} for dep in self.deployments.values()
            ]
        return {
            "version": self.version,
            "uptime_seconds": int(now - self._boot_time),
            "currently_serving": self.currently_serving,
            "model_id": self.model_id,
            "transport": transport,
            "deployments": deployments_snapshot,
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

    def set_deployment_progress(
        self,
        pipeline_id: str,
        stage: str,
        progress_pct: float,
        model_name: str | None = None,
    ) -> None:
        """Record in-flight deployment progress for ``pipeline_id``.

        ``stage`` vocabulary: ``queued`` (SA pre-registered, download not
        started yet), ``downloading``, ``configuring``, ``completed``.
        On ``completed`` the row is removed so the UI can drop it.
        """
        with self._deploy_lock:
            if stage == "completed":
                self.deployments.pop(pipeline_id, None)
                return
            existing = self.deployments.get(pipeline_id)
            if stage == "queued" and existing is not None and existing.get("stage") != "queued":
                return
            existing = existing or {}
            self.deployments[pipeline_id] = {
                "pipeline_id": pipeline_id,
                "model_name": model_name or existing.get("model_name"),
                "stage": stage,
                "progress_pct": progress_pct,
                # Timestamp lets snapshot() age stale queued rows out — a
                # queued row whose Control DEPLOY_MODEL never arrived would
                # otherwise linger until the runtime restarted.
                "created_at": existing.get("created_at", time.monotonic()),
            }

    def set_transport(
        self,
        transport_type: str | None,
        endpoint: str | None,
        connected: bool,
    ) -> None:
        """Record the runtime's transport state for the ``/healthz``
        response's ``transport`` block. Called by whichever component
        owns the transport lifecycle — for Zenoh that's the runtime,
        which sets ``connected=True`` right after ``zenoh.open()``
        returns and ``connected=False`` on close/shutdown."""
        self.transport_type = transport_type
        self.transport_endpoint = endpoint
        self.transport_connected = connected


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        def _check_loopback(self) -> bool:
            """Reject cross-origin browser calls and DNS-rebinding attempts.

            The server binds to 127.0.0.1 so it can't be reached from the
            network, but a malicious page loaded in the local user's
            browser can still POST to loopback with a "simple request"
            (no preflight). Without an Origin/Host guard, such a page
            could trigger START_SERVING / STOP_SERVING remotely.
            """
            host = (self.headers.get("Host") or "").lower().split(":")[0]
            if host not in _LOOPBACK_HOSTS:
                self.send_error(403, "Non-loopback Host header")
                return False
            origin = self.headers.get("Origin")
            if origin is not None:
                try:
                    origin_host = (urllib.parse.urlparse(origin).hostname or "").lower()
                except ValueError:
                    origin_host = ""
                if origin_host not in _LOOPBACK_HOSTS:
                    self.send_error(403, "Non-loopback Origin")
                    return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._check_loopback():
                return
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
            if not self._check_loopback():
                return

            if self.path == "/deployments/pending":
                self._handle_pending_deployment()
                return
            match = _MODEL_ACTION_RE.match(self.path)
            if not match:
                self.send_error(404, "Not Found")
                return
            if not state.has_command_handler():
                self.send_error(503, "Command handler not wired")
                return
            pipeline_id, action = match.group(1), match.group(2)

            command: dict[str, Any]
            if action == "cancel-deploy":
                # An in-flight deploy isn't in state.models() yet, so skip the
                # existence check — the runtime treats a missing worker as a
                # completed-with-note no-op.
                command = {
                    "id": f"loopback-{uuid.uuid4().hex[:8]}",
                    "type": "CANCEL_DEPLOY",
                    "pipeline_id": pipeline_id,
                }
            else:
                model = next((m for m in state.models() if m["id"] == pipeline_id), None)
                if model is None:
                    self.send_error(404, f"Unknown pipeline: {pipeline_id}")
                    return
                if action == "serve":
                    command = {
                        "id": f"loopback-{uuid.uuid4().hex[:8]}",
                        "type": "START_SERVING",
                        "pipeline_id": pipeline_id,
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

            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _handle_pending_deployment(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 4096:
                self.send_error(400, "Empty or oversized body")
                return
            try:
                raw = self.rfile.read(length)
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_error(400, "Malformed JSON")
                return
            pipeline_id = payload.get("pipeline_id")
            model_name = payload.get("model_name")
            if not isinstance(pipeline_id, str) or not pipeline_id:
                self.send_error(400, "pipeline_id required")
                return
            if model_name is not None and not isinstance(model_name, str):
                self.send_error(400, "model_name must be a string or omitted")
                return
            state.set_deployment_progress(pipeline_id, "queued", 0.0, model_name)
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        @override
        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
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
            logger.warning(f"Health server could not bind to {self.host}:{self.port}: {exc}")
            self._server = None
            return

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
