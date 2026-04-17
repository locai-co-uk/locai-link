# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import pytest
import requests

from link.adapters.http_client import HttpClient, HttpError


@pytest.fixture
def client():
    return HttpClient(base_url="http://test.local/api")


# --- GET ---


def test_get_success(client, mocker):
    mock_resp = mocker.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True}
    mocker.patch.object(client.session, "get", return_value=mock_resp)

    assert client.get("/data") == {"ok": True}


def test_get_timeout_returns_none(client, mocker):
    mocker.patch.object(client.session, "get", side_effect=requests.Timeout())
    assert client.get("/data") is None


def test_get_connection_error_returns_none(client, mocker):
    mocker.patch.object(client.session, "get", side_effect=requests.ConnectionError())
    assert client.get("/data") is None


def test_get_500_returns_none(client, mocker):
    resp = mocker.MagicMock()
    resp.status_code = 500
    resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    mocker.patch.object(client.session, "get", return_value=resp)

    assert client.get("/data") is None


def test_get_401_raises_http_error(client, mocker):
    resp = mocker.MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    mocker.patch.object(client.session, "get", return_value=resp)

    with pytest.raises(HttpError) as exc:
        client.get("/data")
    assert exc.value.status == 401
    assert exc.value.retryable is False


def test_get_403_raises_http_error(client, mocker):
    resp = mocker.MagicMock()
    resp.status_code = 403
    resp.text = "Forbidden"
    resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    mocker.patch.object(client.session, "get", return_value=resp)

    with pytest.raises(HttpError) as exc:
        client.get("/data")
    assert exc.value.status == 403


def test_get_json_decode_error_returns_none(client, mocker):
    resp = mocker.MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.side_effect = requests.JSONDecodeError("err", "doc", 0)
    mocker.patch.object(client.session, "get", return_value=resp)

    assert client.get("/data") is None


# --- POST ---


def test_post_success(client, mocker):
    resp = mocker.MagicMock()
    resp.raise_for_status.return_value = None
    mocker.patch.object(client.session, "post", return_value=resp)

    assert client.post("/submit", json_data={"x": 1}) is True


def test_post_timeout_returns_false(client, mocker):
    mocker.patch.object(client.session, "post", side_effect=requests.Timeout())
    assert client.post("/submit", json_data={"x": 1}) is False


def test_post_connection_error_returns_false(client, mocker):
    mocker.patch.object(client.session, "post", side_effect=requests.ConnectionError())
    assert client.post("/submit", json_data={"x": 1}) is False


def test_post_500_returns_false(client, mocker):
    resp = mocker.MagicMock()
    resp.status_code = 500
    resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    mocker.patch.object(client.session, "post", return_value=resp)

    assert client.post("/submit", json_data={"x": 1}) is False


def test_post_400_raises_http_error(client, mocker):
    resp = mocker.MagicMock()
    resp.status_code = 400
    resp.text = "Bad Request"
    resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    mocker.patch.object(client.session, "post", return_value=resp)

    with pytest.raises(HttpError) as exc:
        client.post("/submit", json_data={"x": 1})
    assert exc.value.status == 400
    assert exc.value.retryable is False


# --- URL Building ---


def test_build_url_absolute_override(client):
    assert client._build_url("https://other.com/x") == "https://other.com/x"


def test_build_url_empty_endpoint(client):
    assert client._build_url("") == "http://test.local/api"


def test_build_url_relative(client):
    assert client._build_url("/health") == "http://test.local/api/health"


def test_build_url_no_base():
    c = HttpClient()
    assert c._build_url("http://abs.com") == "http://abs.com"
    assert c._build_url("relative") == "relative"
