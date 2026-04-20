# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import pytest

from link.app.onboarding import (
    _resolve_token,
    login_and_get_token,
    register_device,
)

API_URL = "https://api.test.local/api/v1"


# --- login_and_get_token ---


def test_login_success(mocker):
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"access_token": "jwt-123"}
    mocker.patch("link.app.onboarding.requests.post", return_value=resp)

    token = login_and_get_token("user@test.com", "pass", API_URL)
    assert token == "jwt-123"


def test_login_bad_credentials(mocker):
    resp = mocker.MagicMock()
    resp.status_code = 401
    resp.json.return_value = {"detail": "Invalid credentials"}
    resp.text = "Unauthorized"
    mocker.patch("link.app.onboarding.requests.post", return_value=resp)

    with pytest.raises(RuntimeError, match="Authentication failed"):
        login_and_get_token("user@test.com", "wrong", API_URL)


def test_login_no_token_in_response(mocker):
    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.json.return_value = {}
    mocker.patch("link.app.onboarding.requests.post", return_value=resp)

    with pytest.raises(RuntimeError, match="no access token"):
        login_and_get_token("user@test.com", "pass", API_URL)


def test_login_network_error(mocker):
    import requests

    mocker.patch("link.app.onboarding.requests.post", side_effect=requests.ConnectionError("down"))

    with pytest.raises(RuntimeError, match="Network error"):
        login_and_get_token("user@test.com", "pass", API_URL)


# --- _resolve_token ---


def test_resolve_token_prefers_explicit_token():
    assert _resolve_token("ignored@test.com", "ignored", "tok-abc", API_URL) == "tok-abc"


def test_resolve_token_from_email(mocker):
    mocker.patch("link.app.onboarding.login_and_get_token", return_value="jwt-from-email")
    mocker.patch("link.app.onboarding.getpass.getpass", return_value="prompted-pass")

    token = _resolve_token("user@test.com", None, None, API_URL)
    assert token == "jwt-from-email"


def test_resolve_token_uses_provided_password(mocker):
    mock_login = mocker.patch("link.app.onboarding.login_and_get_token", return_value="jwt")

    _resolve_token("user@test.com", "explicit-pass", None, API_URL)
    mock_login.assert_called_once_with("user@test.com", "explicit-pass", API_URL)


def test_resolve_token_no_credentials():
    with pytest.raises(ValueError, match="--token or --email"):
        _resolve_token(None, None, None, API_URL)


# --- register_device ---


def test_register_device_sends_auth_header(mocker):
    mocker.patch("link.app.onboarding._resolve_token", return_value="jwt-xyz")

    resp = mocker.MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"device_id": "dev-1", "api_key": "key-1"}
    resp.raise_for_status.return_value = None
    mock_post = mocker.patch("link.app.onboarding.requests.post", return_value=resp)

    config = register_device(name="edge-01", reg_key="rk-1", api_url=API_URL, token="jwt-xyz")

    # Verify auth header was sent
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer jwt-xyz"

    # Verify config was built correctly
    assert config.identity.device_id == "dev-1"
    assert config.identity.api_key == "key-1"
    assert config.identity.device_name == "edge-01"


def test_register_device_payload_no_username(mocker):
    """Verify the registration payload does NOT include username (migrated to token auth)."""
    mocker.patch("link.app.onboarding._resolve_token", return_value="jwt")

    resp = mocker.MagicMock()
    resp.json.return_value = {"device_id": "d", "api_key": "k"}
    resp.raise_for_status.return_value = None
    mock_post = mocker.patch("link.app.onboarding.requests.post", return_value=resp)

    register_device(name="dev", reg_key="rk", api_url=API_URL, token="jwt")

    payload = mock_post.call_args.kwargs["json"]
    assert "username" not in payload
    assert payload["registration_key"] == "rk"
    assert payload["name"] == "dev"


# --- Backend-provided config ---


def _mock_register_response(mocker, response_body):
    """Helper: patch requests.post for register_device to return response_body."""
    mocker.patch("link.app.onboarding._resolve_token", return_value="jwt")
    resp = mocker.MagicMock()
    resp.json.return_value = response_body
    resp.raise_for_status.return_value = None
    return mocker.patch("link.app.onboarding.requests.post", return_value=resp)


def test_register_device_applies_backend_config(mocker):
    """When the backend returns a config, the client uses it and resolves templates."""
    server_config = {
        "version": 2.1,
        "identity": {
            "device_id": "will-be-overridden",
            "device_name": "placeholder",
        },
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
    _mock_register_response(mocker, {"device_id": "dev-1", "api_key": "key-1", "config": server_config})

    config = register_device(name="edge-01", reg_key="rk", api_url=API_URL, token="jwt")

    # Identity is injected from the registration response, not whatever the template said.
    assert config.identity.device_id == "dev-1"
    assert config.identity.api_key == "key-1"
    assert config.identity.device_name == "edge-01"
    assert config.identity.api_url == API_URL

    # Server-specified fields survived
    assert config.logging.level == "DEBUG"
    assert config.reporting.interval == 60
    assert len(config.pipelines) == 1
    assert config.pipelines[0].id == "custom_pipeline"
    assert config.pipelines[0].active is True

    # Identity templates were resolved in handler args
    log_handler = config.logging.handlers[0]
    assert log_handler.args["url"] == f"{API_URL}/agent/dev-1/logs"
    assert log_handler.args["api_key"] == "key-1"


def test_register_device_falls_back_when_no_config(mocker):
    """Omitting `config` from the response triggers the built-in defaults."""
    _mock_register_response(mocker, {"device_id": "dev-1", "api_key": "key-1"})

    config = register_device(name="edge-01", reg_key="rk", api_url=API_URL, token="jwt")

    # Default: 2 pipelines (command_center, system_metrics)
    pipeline_ids = {p.id for p in config.pipelines}
    assert pipeline_ids == {"command_center", "system_metrics"}


def test_register_device_falls_back_on_wrong_version(mocker, caplog):
    """Unknown schema version triggers a loud error and fallback."""
    import logging as stdlib_logging

    _mock_register_response(
        mocker,
        {
            "device_id": "dev-1",
            "api_key": "key-1",
            "config": {"version": 9.9, "identity": {"device_id": "x"}, "pipelines": []},
        },
    )

    with caplog.at_level(stdlib_logging.CRITICAL):
        config = register_device(name="edge-01", reg_key="rk", api_url=API_URL, token="jwt")

    # Loud error logged
    assert any("Unsupported config schema version" in r.message for r in caplog.records)

    # But registration succeeded with defaults
    pipeline_ids = {p.id for p in config.pipelines}
    assert pipeline_ids == {"command_center", "system_metrics"}


def test_register_device_falls_back_on_invalid_config(mocker, caplog):
    """Malformed config (pydantic validation error) triggers fallback, not crash."""
    import logging as stdlib_logging

    _mock_register_response(
        mocker,
        {
            "device_id": "dev-1",
            "api_key": "key-1",
            # Missing required `source` field on the pipeline
            "config": {
                "version": 2.1,
                "identity": {"device_id": "x"},
                "pipelines": [{"id": "broken", "sink": {"type": "console"}}],
            },
        },
    )

    with caplog.at_level(stdlib_logging.CRITICAL):
        config = register_device(name="edge-01", reg_key="rk", api_url=API_URL, token="jwt")

    assert any("Backend config rejected" in r.message for r in caplog.records)
    # Fallback defaults kicked in
    assert {p.id for p in config.pipelines} == {"command_center", "system_metrics"}


def test_register_device_identity_always_overridden(mocker):
    """Even if the backend sends wrong identity values, the client's real identity wins."""
    _mock_register_response(
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

    config = register_device(name="real-name", reg_key="rk", api_url=API_URL, token="jwt")

    assert config.identity.device_id == "real-id"
    assert config.identity.api_key == "real-key"
    assert config.identity.device_name == "real-name"


def test_activate_device_applies_backend_config(mocker):
    """Activation path also honours a server-provided config."""
    from link.app.onboarding import activate_device

    server_config = {
        "version": 2.1,
        "identity": {"device_id": "x"},
        "pipelines": [{"id": "activated", "active": True, "source": {"type": "clock_tick"}}],
    }
    resp = mocker.MagicMock()
    resp.json.return_value = {"api_key": "new-key", "config": server_config}
    resp.raise_for_status.return_value = None
    mocker.patch("link.app.onboarding.requests.post", return_value=resp)

    config = activate_device(device_id="existing-dev", reg_key="rk", api_url=API_URL)

    assert config.identity.device_id == "existing-dev"
    assert config.identity.api_key == "new-key"
    assert len(config.pipelines) == 1
    assert config.pipelines[0].id == "activated"
