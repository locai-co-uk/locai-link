# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import json
import types

import pytest

from link.app.onboarding import (
    _RETRY_AFTER_HONOR_CAP_SECONDS,
    _RETRY_BACKOFF_CAP_SECONDS,
    _RETRY_MAX_ATTEMPTS,
    FLEET_MARKER_PATH,
    _redeem_device_key,
    _retry_after_seconds,
    _retry_backoff_seconds,
    enroll_device,
    register_with_key,
)

API_URL = "https://api.test.local/api/v1"
REG_ENDPOINT = f"{API_URL}/devices/headless/register-with-reg-key"
ENROLL_ENDPOINT = f"{API_URL}/devices/enroll"


def _mock_reg_response(mocker, response_body):
    """Patch the machine-id hash + requests.post for register_with_key."""
    mocker.patch("link.infra.utils.get_machine_id_hash", return_value="mach-hash-123")
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.json.return_value = response_body
    resp.raise_for_status.return_value = None
    return mocker.patch("link.app.onboarding.requests.post", return_value=resp)


# --- register_with_key: endpoint, auth, and machine-bound payload ---


def test_register_with_key_posts_to_headless_endpoint_with_bearer(mocker):
    mock_post = _mock_reg_response(mocker, {"device_id": "dev-1", "api_key": "key-1"})

    config = register_with_key(reg_key="rk-1", api_url=API_URL)

    # The registration key is the bearer credential (no user JWT).
    assert mock_post.call_args.args[0] == REG_ENDPOINT
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer rk-1"

    # Machine-bound flat payload; the key is NOT echoed in the body.
    payload = mock_post.call_args.kwargs["json"]
    assert payload["machine_id_hash"] == "mach-hash-123"
    assert payload["device_type"] == "other"
    assert payload["device_name"]  # <os-user>@<hostname>, non-empty
    for field in ("os", "arch", "hostname"):
        assert field in payload
    assert "registration_key" not in payload

    assert config.identity.device_id == "dev-1"
    assert config.identity.api_key == "key-1"


def test_register_with_key_persists_server_final_device_name(mocker):
    """The server name is authoritative (it may suffix on collision)."""
    _mock_reg_response(mocker, {"device_id": "d", "api_key": "k", "device_name": "pi@garage-pi-2"})

    config = register_with_key(reg_key="rk", api_url=API_URL)

    assert config.identity.device_name == "pi@garage-pi-2"


def test_register_with_key_raises_on_missing_fields(mocker):
    _mock_reg_response(mocker, {"device_id": "d"})  # no api_key

    with pytest.raises(RuntimeError, match="missing device_id or api_key"):
        register_with_key(reg_key="rk", api_url=API_URL)


# --- enroll_device: endpoint, auth, and fleet marker ---


def test_enroll_device_posts_to_enroll_endpoint_with_bearer(mocker, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # FLEET_MARKER_PATH is cwd-relative
    mock_post = _mock_reg_response(mocker, {"device_id": "dev-9", "api_key": "key-9"})

    config = enroll_device(fleet_key="fk-1", api_url=API_URL)

    # The fleet key is the bearer credential; same machine-bound payload shape.
    assert mock_post.call_args.args[0] == ENROLL_ENDPOINT
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer fk-1"
    assert mock_post.call_args.kwargs["json"]["machine_id_hash"] == "mach-hash-123"
    assert config.identity.device_id == "dev-9"
    assert config.identity.api_key == "key-9"

    marker = json.loads(FLEET_MARKER_PATH.read_text(encoding="utf-8"))
    assert marker["device_id"] == "dev-9"


# --- _redeem_device_key: response validation + retry (shared by both flows) ---


def _redeem():
    return _redeem_device_key(endpoint=REG_ENDPOINT, bearer_key="rk", op_name="Test redeem")


def test_redeem_raises_on_invalid_json(mocker):
    mocker.patch("link.infra.utils.get_machine_id_hash", return_value="m")
    resp = mocker.MagicMock(status_code=200)
    resp.json.side_effect = ValueError("no JSON")
    mocker.patch("link.app.onboarding.requests.post", return_value=resp)

    with pytest.raises(RuntimeError, match="not valid JSON"):
        _redeem()


def test_redeem_raises_on_non_object_json(mocker):
    _mock_reg_response(mocker, ["not", "an", "object"])

    with pytest.raises(RuntimeError, match="not a JSON object"):
        _redeem()


def test_redeem_error_never_contains_credential_values(mocker):
    """A missing-field error lists key names only; a live api_key must not leak."""
    _mock_reg_response(mocker, {"api_key": "sk-live-secret"})  # no device_id

    with pytest.raises(RuntimeError, match="missing device_id or api_key") as exc_info:
        _redeem()

    assert "sk-live-secret" not in str(exc_info.value)
    assert "api_key" in str(exc_info.value)


def test_redeem_retries_transient_5xx_then_succeeds(mocker):
    mocker.patch("link.infra.utils.get_machine_id_hash", return_value="m")
    mocker.patch("link.app.onboarding.time.sleep")
    fail = mocker.MagicMock(status_code=503, headers={})
    ok = mocker.MagicMock(status_code=200)
    ok.json.return_value = {"device_id": "d", "api_key": "k"}
    mock_post = mocker.patch("link.app.onboarding.requests.post", side_effect=[fail, ok])

    data, device_id, api_key = _redeem()

    assert (device_id, api_key) == ("d", "k")
    assert data == {"device_id": "d", "api_key": "k"}
    assert mock_post.call_count == 2


def test_redeem_permanent_4xx_fails_without_retry(mocker):
    mocker.patch("link.infra.utils.get_machine_id_hash", return_value="m")
    sleep = mocker.patch("link.app.onboarding.time.sleep")
    resp = mocker.MagicMock(status_code=403)
    resp.json.return_value = {"detail": "bad key"}
    mock_post = mocker.patch("link.app.onboarding.requests.post", return_value=resp)

    with pytest.raises(RuntimeError, match=r"rejected \(HTTP 403\): bad key"):
        _redeem()

    assert mock_post.call_count == 1
    sleep.assert_not_called()


# --- Backend-provided config (resolved via _resolve_agent_config) ---


def test_register_applies_backend_config(mocker):
    """When the backend returns a config, the client uses it and resolves templates."""
    server_config = {
        "version": 2.1,
        "identity": {"device_id": "will-be-overridden", "device_name": "placeholder"},
        "transport": {"type": "http"},
        "logging": {
            "level": "DEBUG",  # non-default, to prove we used the server config
            "handlers": [
                {
                    "type": "http",
                    "args": {
                        "url": "${identity.api_url}/agent/${identity.device_id}/logs",
                        "api_key": "${identity.api_key}",
                    },
                }
            ],
        },
        "reporting": {"interval": 60, "handlers": []},
        "pipelines": [
            {
                "id": "custom_pipeline",
                "active": True,
                "source": {"type": "clock_tick", "args": {"interval": 5.0}},
                "sink": {"type": "console"},
            }
        ],
    }
    _mock_reg_response(mocker, {"device_id": "dev-1", "api_key": "key-1", "config": server_config})

    config = register_with_key(reg_key="rk", api_url=API_URL)

    # Identity is injected from the registration response, not the template.
    assert config.identity.device_id == "dev-1"
    assert config.identity.api_key == "key-1"
    assert config.identity.api_url == API_URL

    # Server-specified fields survived.
    assert config.logging.level == "DEBUG"
    assert config.reporting.interval == 60
    assert len(config.pipelines) == 1
    assert config.pipelines[0].id == "custom_pipeline"

    # Identity templates were resolved in handler args.
    log_handler = config.logging.handlers[0]
    assert log_handler.args["url"] == f"{API_URL}/agent/dev-1/logs"
    assert log_handler.args["api_key"] == "key-1"


def test_register_falls_back_when_no_config(mocker):
    """Omitting `config` from the response triggers the built-in defaults."""
    _mock_reg_response(mocker, {"device_id": "dev-1", "api_key": "key-1"})

    config = register_with_key(reg_key="rk", api_url=API_URL)

    pipeline_ids = {p.id for p in config.pipelines}
    assert pipeline_ids == {"command_center", "system_metrics"}


def test_register_falls_back_on_wrong_version(mocker, caplog):
    """Unknown schema version triggers a loud error and fallback."""
    import logging as stdlib_logging

    _mock_reg_response(
        mocker,
        {
            "device_id": "dev-1",
            "api_key": "key-1",
            "config": {"version": 9.9, "identity": {"device_id": "x"}, "pipelines": []},
        },
    )

    with caplog.at_level(stdlib_logging.CRITICAL):
        config = register_with_key(reg_key="rk", api_url=API_URL)

    assert any("Unsupported config schema version" in r.message for r in caplog.records)
    assert {p.id for p in config.pipelines} == {"command_center", "system_metrics"}


def test_register_falls_back_on_invalid_config(mocker, caplog):
    """Malformed config (pydantic validation error) triggers fallback, not crash."""
    import logging as stdlib_logging

    _mock_reg_response(
        mocker,
        {
            "device_id": "dev-1",
            "api_key": "key-1",
            # Missing required `source` field on the pipeline.
            "config": {
                "version": 2.1,
                "identity": {"device_id": "x"},
                "pipelines": [{"id": "broken", "sink": {"type": "console"}}],
            },
        },
    )

    with caplog.at_level(stdlib_logging.CRITICAL):
        config = register_with_key(reg_key="rk", api_url=API_URL)

    assert any("Backend config rejected" in r.message for r in caplog.records)
    assert {p.id for p in config.pipelines} == {"command_center", "system_metrics"}


def test_register_identity_always_overridden(mocker):
    """Even if the backend sends wrong identity values, the real identity wins."""
    _mock_reg_response(
        mocker,
        {
            "device_id": "real-id",
            "api_key": "real-key",
            "config": {
                "version": 2.1,
                "identity": {
                    "device_id": "backend-lied",
                    "api_key": "backend-lied",
                    "device_name": "backend-lied",
                },
                "pipelines": [],
            },
        },
    )

    config = register_with_key(reg_key="rk", api_url=API_URL)

    assert config.identity.device_id == "real-id"
    assert config.identity.api_key == "real-key"


# --- Retry-policy helpers (pure logic; no transport, no HTTP status codes) ---


def test_retry_backoff_full_jitter_ceiling_grows_then_caps(mocker):
    """Ceiling doubles per attempt (base * 2^(n-1)) and never exceeds the cap; the
    jitter draw is always full-range [0, ceiling]."""
    # Pin the draw to the upper bound so the computed ceiling is observable.
    uniform = mocker.patch("link.app.onboarding.random.uniform", side_effect=lambda low, high: high)

    assert _retry_backoff_seconds(1) == 2.0  # 2.0 * 2^0
    assert _retry_backoff_seconds(2) == 4.0  # 2.0 * 2^1
    assert _retry_backoff_seconds(3) == 8.0  # 2.0 * 2^2
    assert _retry_backoff_seconds(6) == _RETRY_BACKOFF_CAP_SECONDS  # 2.0 * 2^5 = 64 -> capped to 60
    assert _retry_backoff_seconds(50) == _RETRY_BACKOFF_CAP_SECONDS  # stays capped

    # Every draw started at 0 (full jitter), never a narrowed floor.
    assert all(call.args[0] == 0 for call in uniform.call_args_list)


def test_retry_backoff_stays_within_bounds():
    """Unmocked, the jittered delay is always within [0, cap] for every attempt."""
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        for _ in range(50):
            delay = _retry_backoff_seconds(attempt)
            assert 0.0 <= delay <= _RETRY_BACKOFF_CAP_SECONDS


def _resp_with_retry_after(value):
    """A minimal response-like object carrying just a Retry-After header."""
    headers = {} if value is None else {"Retry-After": value}
    return types.SimpleNamespace(headers=headers)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),  # Retry-After absent (empty headers)
        ("7", 7.0),
        ("soon", None),  # unparseable
        ("-5", None),  # negative
        (str(_RETRY_AFTER_HONOR_CAP_SECONDS + 1000), _RETRY_AFTER_HONOR_CAP_SECONDS),  # clamped to cap
    ],
)
def test_retry_after_seconds(value, expected):
    assert _retry_after_seconds(_resp_with_retry_after(value)) == expected


def test_retry_after_none_headers_returns_none():
    assert _retry_after_seconds(types.SimpleNamespace(headers=None)) is None
