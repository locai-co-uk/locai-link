# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the loopback /healthz HTTP server."""

import json
import socket
import urllib.error
import urllib.request
from typing import Any

import pytest

from link.infra.health_server import HEALTH_HOST, HealthServer, HealthState


def _free_port() -> int:
    """Reserve an OS-assigned free port for parallel test isolation."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HEALTH_HOST, 0))
        return s.getsockname()[1]


def _get_health(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://{HEALTH_HOST}:{port}/healthz", timeout=2) as resp:
        return json.loads(resp.read())


def _get_models(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://{HEALTH_HOST}:{port}/models", timeout=2) as resp:
        return json.loads(resp.read())


def _post_no_body(port: int, path: str) -> Any:
    req = urllib.request.Request(f"http://{HEALTH_HOST}:{port}{path}", method="POST")
    return urllib.request.urlopen(req, timeout=2)


def _post_json(port: int, path: str, body: dict[str, Any]) -> Any:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://{HEALTH_HOST}:{port}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        return urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as e:
        return e


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
    # transport is null until `set_transport` is called (unit tests
    # construct HealthState standalone without a Zenoh session).
    assert body["transport"] is None


def test_set_transport_surfaces_in_response(server):
    _, state, port = server
    state.set_transport(
        transport_type="zenoh",
        endpoint="tls/zenoh.example.com:7448",
        connected=True,
    )
    body = _get_health(port)
    assert body["transport"] == {
        "type": "zenoh",
        "endpoint": "tls/zenoh.example.com:7448",
        "connected": True,
    }


def test_set_transport_disconnect_flips_connected_only(server):
    _, state, port = server
    state.set_transport("zenoh", "tls/zenoh.example.com:7448", connected=True)
    state.set_transport("zenoh", "tls/zenoh.example.com:7448", connected=False)
    body = _get_health(port)
    assert body["transport"] == {
        "type": "zenoh",
        "endpoint": "tls/zenoh.example.com:7448",
        "connected": False,
    }


def test_deployments_empty_when_none_in_flight(server):
    _, _, port = server
    body = _get_health(port)
    assert body["deployments"] == []


def test_set_deployment_progress_surfaces_row(server):
    _, state, port = server
    state.set_deployment_progress("p-1", "downloading", 42.0, model_name="foo.gguf")
    body = _get_health(port)
    assert body["deployments"] == [
        {
            "pipeline_id": "p-1",
            "model_name": "foo.gguf",
            "stage": "downloading",
            "progress_pct": 42.0,
        }
    ]


def test_queued_does_not_overwrite_active_progress(server):
    # Setup Assistant fires pre-registration POSTs in parallel with real
    # deploys — a late "queued" write must not stomp active progress.
    _, state, port = server
    state.set_deployment_progress("p-1", "downloading", 30.0, model_name="foo.gguf")
    state.set_deployment_progress("p-1", "queued", 0.0, model_name="foo.gguf")
    body = _get_health(port)
    assert body["deployments"][0]["stage"] == "downloading"
    assert body["deployments"][0]["progress_pct"] == 30.0


def test_pending_endpoint_registers_queued_row(server):
    _, state, port = server
    resp = _post_json(
        port,
        "/deployments/pending",
        {"pipeline_id": "p-1", "model_name": "foo.gguf"},
    )
    assert resp.status == 202
    # `created_at` is set by set_deployment_progress; check the stable
    # fields only.
    row = {k: v for k, v in state.deployments["p-1"].items() if k != "created_at"}
    assert row == {
        "pipeline_id": "p-1",
        "model_name": "foo.gguf",
        "stage": "queued",
        "progress_pct": 0.0,
    }
    assert "created_at" in state.deployments["p-1"]


def test_pending_endpoint_rejects_missing_pipeline_id(server):
    _, _, port = server
    resp = _post_json(port, "/deployments/pending", {"model_name": "foo.gguf"})
    assert resp.status == 400


def test_completed_deployment_clears_row(server):
    _, state, port = server
    state.set_deployment_progress("p-1", "downloading", 42.0, model_name="foo.gguf")
    state.set_deployment_progress("p-1", "completed", 100.0)
    body = _get_health(port)
    assert body["deployments"] == []


def test_deployment_progress_carries_model_name_across_ticks(server):
    # First tick sets model_name; later ticks (throttled 5%-step deltas)
    # only pass pct + stage. Row should still carry the original name.
    _, state, port = server
    state.set_deployment_progress("p-1", "downloading", 0.0, model_name="foo.gguf")
    state.set_deployment_progress("p-1", "downloading", 50.0)
    body = _get_health(port)
    assert body["deployments"][0]["model_name"] == "foo.gguf"
    assert body["deployments"][0]["progress_pct"] == 50.0


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


def test_uptime_advances(mocker):
    """Uptime is derived from time.monotonic() deltas — mock it rather
    than sleep for real seconds. Same guarantee, ~0ms wall clock."""
    # HealthState calls monotonic() once at init and once per snapshot.
    # side_effect gives us controlled values without stubbing globally.
    mocker.patch(
        "link.infra.health_server.time.monotonic",
        side_effect=[100.0, 100.0, 102.5],
    )
    state = HealthState(version="1.2.3")
    first = state.snapshot()["uptime_seconds"]
    second = state.snapshot()["uptime_seconds"]
    assert first == 0
    assert second == 2


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


def test_models_returns_empty_when_no_provider(server):
    """HealthState constructed without a provider (default in tests)
    returns an empty models list — GET /models still succeeds."""
    _, _, port = server
    body = _get_models(port)
    assert body == {"models": []}


def test_models_calls_provider_on_every_request():
    """Provider is called lazily on each GET so /models never serves
    stale data — even if the runtime never pushes an update."""
    port = _free_port()
    seq = [
        [{"id": "a", "alias": "A", "port": 8080, "host": "127.0.0.1", "is_serving": False}],
        [
            {"id": "a", "alias": "A", "port": 8080, "host": "127.0.0.1", "is_serving": True},
            {"id": "b", "alias": "B", "port": 8080, "host": "127.0.0.1", "is_serving": False},
        ],
    ]
    calls = {"n": 0}

    def provider() -> list[dict[str, Any]]:
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    state = HealthState(version="1", models_provider=provider)
    srv = HealthServer(state, port=port)
    srv.start()
    try:
        first = _get_models(port)["models"]
        second = _get_models(port)["models"]
        assert len(first) == 1 and first[0]["id"] == "a" and first[0]["is_serving"] is False
        assert len(second) == 2 and second[1]["id"] == "b"
        assert calls["n"] == 2, "provider must be called once per request"
    finally:
        srv.stop()


# --- POST /models/{id}/serve and /stop-serving -------------------------------


_DEFAULT_HANDLER = object()


def _server_with_handlers(
    models: list[dict[str, Any]],
    command_handler: Any = _DEFAULT_HANDLER,
):
    """Boot a HealthServer with a provider that returns `models` and,
    by default, a dispatch handler that records commands.

    Pass ``command_handler=None`` to skip wiring a dispatcher (503
    case), or a callable to inject a specific handler (e.g. one that
    raises). The third tuple element is the received-commands list —
    empty when a non-default handler is provided."""
    port = _free_port()
    received: list[dict[str, Any]] = []

    if command_handler is _DEFAULT_HANDLER:

        def dispatch(cmd: dict[str, Any]) -> None:
            received.append(cmd)

        wired = dispatch
    else:
        wired = command_handler

    state = HealthState(
        version="1.0.18-test",
        models_provider=lambda: models,
        command_handler=wired,
    )
    srv = HealthServer(state, port=port)
    srv.start()
    return srv, port, received


def test_post_serve_dispatches_start_serving_command():
    srv, port, received = _server_with_handlers(
        [
            {
                "id": "llm_server",
                "alias": "smollm-135m",
                "port": 8123,
                "host": "127.0.0.1",
                "is_serving": False,
            }
        ]
    )
    try:
        resp = _post_no_body(port, "/models/llm_server/serve")
        assert resp.status == 202
    finally:
        srv.stop()

    assert len(received) == 1
    cmd = received[0]
    assert cmd["type"] == "START_SERVING"
    assert cmd["pipeline_id"] == "llm_server"
    assert cmd["port"] == 8123
    assert cmd["host"] == "127.0.0.1"
    assert cmd["model_display_name"] == "smollm-135m"
    # Every dispatched command carries a unique id so the runtime's
    # dedup doesn't collapse repeated toggles.
    assert cmd["id"].startswith("loopback-")


def test_post_stop_serving_dispatches_stop_serving_command():
    srv, port, received = _server_with_handlers(
        [
            {
                "id": "llm_server",
                "alias": "smollm-135m",
                "port": 8123,
                "host": "127.0.0.1",
                "is_serving": True,
            }
        ]
    )
    try:
        resp = _post_no_body(port, "/models/llm_server/stop-serving")
        assert resp.status == 202
    finally:
        srv.stop()

    assert len(received) == 1
    cmd = received[0]
    assert cmd["type"] == "STOP_SERVING"
    assert cmd["pipeline_id"] == "llm_server"
    # No port/host/alias on stop — they're not part of the command
    # schema and can't be sent without triggering pydantic's
    # extra=forbid.
    assert "port" not in cmd
    assert "host" not in cmd


def test_post_uninstall_dispatches_uninstall_command():
    srv, port, received = _server_with_handlers(
        [
            {
                "id": "llm_server",
                "alias": "smollm-135m",
                "port": 8123,
                "host": "127.0.0.1",
                "is_serving": False,
            }
        ]
    )
    try:
        resp = _post_no_body(port, "/models/llm_server/uninstall")
        assert resp.status == 202
    finally:
        srv.stop()

    assert len(received) == 1
    cmd = received[0]
    assert cmd["type"] == "UNINSTALL_MODEL"
    assert cmd["pipeline_id"] == "llm_server"
    # force_stop omitted -> schema default (False); no extras that would trip
    # pydantic's extra=forbid.
    assert "force_stop" not in cmd
    assert cmd["id"].startswith("loopback-")


def test_post_uninstall_unknown_pipeline_returns_404():
    srv, port, received = _server_with_handlers([])  # no pipelines
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_no_body(port, "/models/ghost/uninstall")
        assert exc.value.code == 404
    finally:
        srv.stop()
    assert received == []


def test_post_unknown_pipeline_returns_404():
    srv, port, received = _server_with_handlers([])  # no pipelines
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_no_body(port, "/models/nonexistent/serve")
        assert exc.value.code == 404
    finally:
        srv.stop()
    assert received == [], "no command should dispatch for an unknown pipeline"


def test_post_cancel_deploy_dispatches_cancel_command():
    # cancel-deploy targets an in-flight deploy that isn't in state.models()
    # yet, so no existence check is required.
    srv, port, received = _server_with_handlers([])
    try:
        resp = _post_no_body(port, "/models/llm_server/cancel-deploy")
        assert resp.status == 202
    finally:
        srv.stop()

    assert len(received) == 1
    cmd = received[0]
    assert cmd["type"] == "CANCEL_DEPLOY"
    assert cmd["pipeline_id"] == "llm_server"
    assert cmd["id"].startswith("loopback-")


def test_post_serve_falls_back_to_defaults_when_config_is_bare():
    """A pipeline that's never served yet may have port/host unset in
    args. The endpoint should fill in the same defaults StartServingCommand
    would use, not fail."""
    srv, port, received = _server_with_handlers(
        [{"id": "fresh", "alias": "fresh", "port": None, "host": None, "is_serving": False}]
    )
    try:
        resp = _post_no_body(port, "/models/fresh/serve")
        assert resp.status == 202
    finally:
        srv.stop()

    cmd = received[0]
    assert cmd["port"] == 8100  # StartServingCommand default
    assert cmd["host"] == "0.0.0.0"  # StartServingCommand default


def test_post_returns_503_when_no_command_handler():
    """If the runtime hasn't wired a command_handler, the POST path
    must degrade rather than crash — GET /models still works."""
    srv, port, _ = _server_with_handlers([], command_handler=None)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_no_body(port, "/models/anything/serve")
        assert exc.value.code == 503
    finally:
        srv.stop()


def test_post_500_when_handler_raises():
    """A handler that blows up shouldn't bring the whole server down —
    the POST returns 500 and subsequent requests still work."""

    def bad_dispatch(_cmd: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    srv, port, _ = _server_with_handlers(
        [{"id": "x", "alias": "x", "port": 8080, "host": "127.0.0.1", "is_serving": False}],
        command_handler=bad_dispatch,
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_no_body(port, "/models/x/serve")
        assert exc.value.code == 500
        # And /models still responds.
        body = _get_models(port)
        assert body["models"][0]["id"] == "x"
    finally:
        srv.stop()


def test_post_invalid_action_returns_404():
    srv, port, _ = _server_with_handlers(
        [{"id": "x", "alias": "x", "port": 8080, "host": "127.0.0.1", "is_serving": False}]
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_no_body(port, "/models/x/pause")  # not a valid action
        assert exc.value.code == 404
    finally:
        srv.stop()
