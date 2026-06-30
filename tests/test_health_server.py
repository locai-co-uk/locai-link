# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the loopback /healthz HTTP server."""

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from link.infra.health_server import HEALTH_HOST, HealthServer, HealthState


def _free_port() -> int:
    """Reserve an OS-assigned free port for parallel test isolation."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HEALTH_HOST, 0))
        return s.getsockname()[1]


def _get_health(port: int) -> dict:
    with urllib.request.urlopen(f"http://{HEALTH_HOST}:{port}/healthz", timeout=2) as resp:
        return json.loads(resp.read())


@pytest.fixture
def server():
    """Yield a fresh, started HealthServer on an ephemeral port."""
    port = _free_port()
    state = HealthState(version="1.2.3")
    srv = HealthServer(state, port=port)
    srv.start()
    yield srv, state, port
    srv.stop()


def test_idle_state_reports_not_serving(server):
    _, _, port = server
    body = _get_health(port)
    assert body["version"] == "1.2.3"
    assert body["currently_serving"] is False
    assert body["model_id"] is None
    assert body["uptime_seconds"] >= 0


def test_set_serving_surfaces_in_response(server):
    _, state, port = server
    state.set_serving("model-abc")
    body = _get_health(port)
    assert body["currently_serving"] is True
    assert body["model_id"] == "model-abc"


def test_clearing_serving_returns_to_idle(server):
    _, state, port = server
    state.set_serving("model-abc")
    state.set_serving(None)
    body = _get_health(port)
    assert body["currently_serving"] is False
    assert body["model_id"] is None


def test_uptime_advances(server):
    _, _, port = server
    first = _get_health(port)["uptime_seconds"]
    time.sleep(1.1)
    second = _get_health(port)["uptime_seconds"]
    assert second >= first + 1


def test_other_paths_return_404(server):
    _, _, port = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://{HEALTH_HOST}:{port}/random", timeout=2)
    assert exc.value.code == 404


def test_unresolved_version_falls_back_to_unknown():
    state = HealthState(version=None)
    assert state.snapshot()["version"] == "unknown"


def test_stop_releases_port():
    """A clean stop() should free the port for the next start()."""
    port = _free_port()
    srv = HealthServer(HealthState(version="x"), port=port)
    srv.start()
    srv.stop()
    # Re-binding the same port should succeed immediately if stop() released it.
    srv2 = HealthServer(HealthState(version="y"), port=port)
    srv2.start()
    try:
        assert _get_health(port)["version"] == "y"
    finally:
        srv2.stop()


def test_start_is_idempotent(server):
    srv, _, port = server
    srv.start()  # second call should no-op
    # Server still responds — second start didn't break anything.
    _get_health(port)
