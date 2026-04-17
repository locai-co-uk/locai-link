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
