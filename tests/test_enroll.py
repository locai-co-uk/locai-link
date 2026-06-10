# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for headless fleet enrollment (Step 5).

Covers two surfaces:
1. onboarding.enroll_device — payload/contract, retry/backoff classification.
2. main.run routing — resume-before-enroll, headless gating, interactive precedence.
"""

import argparse

import pytest
import requests

import link.main as mainmod
from link.app.onboarding import (
    _ENROLL_MAX_ATTEMPTS,
    _RETRY_AFTER_HONOR_CAP_SECONDS,
    enroll_device,
)

API_URL = "https://api.test.local/api/v1"


def _resp(mocker, status=200, body=None, headers=None):
    r = mocker.MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.headers = headers or {}
    r.text = str(body)
    return r


@pytest.fixture(autouse=True)
def fast_sleep(mocker):
    """Never actually sleep during backoff; expose the mock for assertions."""
    return mocker.patch("link.app.onboarding.time.sleep")


@pytest.fixture(autouse=True)
def stub_machine_id(mocker):
    """Deterministic machine id so payload assertions are stable."""
    mocker.patch("link.infra.machine_id.get_machine_id_hash", return_value="a" * 64)


@pytest.fixture(autouse=True)
def isolated_fleet_marker(tmp_path, monkeypatch):
    """Redirect the fleet-device marker into tmp so tests never touch the repo's
    configs/ dir and a stale marker can never leak between tests."""
    marker = tmp_path / ".fleet_device"
    monkeypatch.setattr(mainmod, "FLEET_MARKER_PATH", marker)
    return marker


# ---------------------------------------------------------------------------
# enroll_device — contract / payload
# ---------------------------------------------------------------------------


def test_enroll_success_builds_config(mocker):
    post = mocker.patch(
        "link.app.onboarding.requests.post",
        return_value=_resp(mocker, 200, {"device_id": "dev-9", "api_key": "key-9"}),
    )
    config = enroll_device(fleet_key="flk_test", api_url=API_URL)

    assert config.identity.device_id == "dev-9"
    assert config.identity.api_key == "key-9"
    assert config.identity.api_url == API_URL

    # POSTs to the enroll endpoint with the fleet key as a Bearer token.
    assert post.call_args.args[0] == f"{API_URL}/devices/enroll"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer flk_test"


def test_enroll_payload_sends_machine_id_and_hardware_hints(mocker):
    post = mocker.patch(
        "link.app.onboarding.requests.post",
        return_value=_resp(mocker, 200, {"device_id": "d", "api_key": "k"}),
    )
    enroll_device(fleet_key="flk_test", api_url=API_URL)

    body = post.call_args.kwargs["json"]
    assert body["machine_id_hash"] == "a" * 64
    assert "os" in body and "arch" in body and "hostname" in body
    # No user credentials / registration key in a headless enroll.
    assert "registration_key" not in body
    assert "username" not in body
    assert "password" not in body


def test_enroll_missing_device_id_or_api_key_raises(mocker):
    mocker.patch(
        "link.app.onboarding.requests.post",
        return_value=_resp(mocker, 200, {"device_id": "only-id"}),  # no api_key
    )
    with pytest.raises(RuntimeError, match="missing device_id or api_key"):
        enroll_device(fleet_key="flk_test", api_url=API_URL)


def test_enroll_applies_backend_config(mocker):
    server_config = {"version": 2.1, "identity": {"device_id": "x"}, "pipelines": []}
    mocker.patch(
        "link.app.onboarding.requests.post",
        return_value=_resp(
            mocker, 200, {"device_id": "d", "api_key": "k", "config": server_config}
        ),
    )
    config = enroll_device(fleet_key="flk_test", api_url=API_URL)
    # Identity is injected from the enroll response, not whatever the config said.
    assert config.identity.device_id == "d"
    assert config.identity.api_key == "k"


# ---------------------------------------------------------------------------
# enroll_device — retry / backoff classification (fix #1)
# ---------------------------------------------------------------------------


def test_retries_on_429_then_succeeds(mocker):
    post = mocker.patch(
        "link.app.onboarding.requests.post",
        side_effect=[
            _resp(mocker, 429),
            _resp(mocker, 429),
            _resp(mocker, 200, {"device_id": "d", "api_key": "k"}),
        ],
    )
    config = enroll_device(fleet_key="flk_test", api_url=API_URL)
    assert config.identity.device_id == "d"
    assert post.call_count == 3


def test_retries_on_5xx_then_succeeds(mocker):
    post = mocker.patch(
        "link.app.onboarding.requests.post",
        side_effect=[_resp(mocker, 503), _resp(mocker, 200, {"device_id": "d", "api_key": "k"})],
    )
    enroll_device(fleet_key="flk_test", api_url=API_URL)
    assert post.call_count == 2


def test_retries_on_network_error_then_succeeds(mocker):
    post = mocker.patch(
        "link.app.onboarding.requests.post",
        side_effect=[
            requests.ConnectionError("blip"),
            _resp(mocker, 200, {"device_id": "d", "api_key": "k"}),
        ],
    )
    enroll_device(fleet_key="flk_test", api_url=API_URL)
    assert post.call_count == 2


def test_honors_retry_after_header_on_429(mocker, fast_sleep):
    mocker.patch(
        "link.app.onboarding.requests.post",
        side_effect=[
            _resp(mocker, 429, headers={"Retry-After": "7"}),
            _resp(mocker, 200, {"device_id": "d", "api_key": "k"}),
        ],
    )
    enroll_device(fleet_key="flk_test", api_url=API_URL)
    # The first backoff must honour the server's Retry-After verbatim.
    assert fast_sleep.call_args_list[0].args[0] == 7.0


def test_retry_after_above_cap_is_clamped(mocker, fast_sleep):
    """An absurd or hostile Retry-After cannot stall the agent past the cap."""
    mocker.patch(
        "link.app.onboarding.requests.post",
        side_effect=[
            _resp(mocker, 429, headers={"Retry-After": "9999"}),
            _resp(mocker, 200, {"device_id": "d", "api_key": "k"}),
        ],
    )
    enroll_device(fleet_key="flk_test", api_url=API_URL)
    assert fast_sleep.call_args_list[0].args[0] == _RETRY_AFTER_HONOR_CAP_SECONDS


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422])
def test_permanent_errors_are_not_retried(mocker, status):
    """Bad/expired/revoked key, cap full, malformed request → fail fast, no retry."""
    post = mocker.patch(
        "link.app.onboarding.requests.post",
        return_value=_resp(mocker, status, {"detail": "no"}),
    )
    with pytest.raises(RuntimeError):
        enroll_device(fleet_key="flk_test", api_url=API_URL)
    assert post.call_count == 1


def test_gives_up_after_max_attempts(mocker):
    """A persistent 429 storm is bounded — it doesn't retry forever."""
    post = mocker.patch("link.app.onboarding.requests.post", return_value=_resp(mocker, 429))
    with pytest.raises(RuntimeError, match="after"):
        enroll_device(fleet_key="flk_test", api_url=API_URL)
    assert post.call_count == _ENROLL_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# main.run — routing decisions (resume-before-enroll, headless gating)
# ---------------------------------------------------------------------------


def _args(**overrides):
    base = dict(
        config=None,
        registration_key=None,
        device_name=None,
        device_id=None,
        email=None,
        password=None,
        token=None,
        api_url=None,
        fleet_key=None,
        prod=True,  # short-circuit after _deploy_service so the runtime never starts
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_headless_enrolls_when_no_session_and_fleet_key(mocker):
    sm = mocker.MagicMock()
    sm.load_state.return_value = None  # no local session
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device", return_value=mocker.MagicMock())
    deploy = mocker.patch("link.main._deploy_service")

    mainmod.run(_args(fleet_key="flk_abc", api_url=API_URL))

    enroll.assert_called_once()
    assert enroll.call_args.kwargs["fleet_key"] == "flk_abc"
    sm.bootstrap.assert_called_once()
    deploy.assert_called_once()


def test_fleet_key_env_is_ignored(mocker, monkeypatch):
    """The fleet key is CLI-only by design; the environment must never supply it."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main._deploy_service")
    mocker.patch("link.main.load_config", return_value=mocker.MagicMock())  # factory-default path
    monkeypatch.setenv("LOCAI_FLEET_KEY", "flk_from_env")

    mainmod.run(_args(fleet_key=None))

    enroll.assert_not_called()


def test_fleet_key_file_form_reads_key_from_file(mocker, tmp_path):
    """file:<path> resolves to the file's contents; the secret stays off argv."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device", return_value=mocker.MagicMock())
    mocker.patch("link.main._deploy_service")
    key_file = tmp_path / "fleet.key"
    key_file.write_text("flk_from_file\n", encoding="utf-8")

    mainmod.run(_args(fleet_key=f"file:{key_file}"))

    enroll.assert_called_once()
    assert enroll.call_args.kwargs["fleet_key"] == "flk_from_file"


def test_fleet_key_file_form_missing_file_fails(mocker, tmp_path):
    """A missing/unreadable key file is a hard error, not a silent fallthrough."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main._deploy_service")

    with pytest.raises(SystemExit):
        mainmod.run(_args(fleet_key=f"file:{tmp_path / 'missing.key'}"))

    enroll.assert_not_called()


def test_headless_enroll_writes_fleet_marker(mocker, isolated_fleet_marker):
    """A successful fleet enrollment leaves the non-secret marker behind."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    mocker.patch("link.main.enroll_device", return_value=mocker.MagicMock())
    mocker.patch("link.main._deploy_service")

    mainmod.run(_args(fleet_key="flk_abc"))

    assert isolated_fleet_marker.exists()


def test_wiped_fleet_device_fails_loudly(mocker, isolated_fleet_marker):
    """Marker present + no session + no key = hard exit, never factory defaults."""
    isolated_fleet_marker.write_text("{}", encoding="utf-8")
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device")
    load_config = mocker.patch("link.main.load_config")
    mocker.patch("link.main._deploy_service")

    with pytest.raises(SystemExit):
        mainmod.run(_args(fleet_key=None))

    enroll.assert_not_called()
    load_config.assert_not_called()


def test_existing_session_resumes_without_enrolling(mocker):
    """The update-driven case: a session exists → resume, NO backend call."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = {"some": "persisted-state"}
    mocker.patch("link.main.StateManager", return_value=sm)
    mocker.patch("link.main.AgentConfig", return_value=mocker.MagicMock())  # build succeeds
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main._deploy_service")

    mainmod.run(_args(fleet_key="flk_abc"))

    enroll.assert_not_called()


def test_no_fleet_key_does_not_enroll(mocker):
    """No session and no fleet key → fall through to defaults, never enroll."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main._deploy_service")
    mocker.patch("link.main.load_config", return_value=mocker.MagicMock())  # factory-default path

    mainmod.run(_args(fleet_key=None))

    enroll.assert_not_called()


def test_registration_key_takes_interactive_path_over_enroll(mocker):
    """A registration key present → interactive register path wins; enroll is skipped."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    register = mocker.patch("link.main.register_device", return_value=mocker.MagicMock())
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main._deploy_service")

    mainmod.run(_args(registration_key="rk", device_name="edge", token="jwt", fleet_key="flk_abc"))

    register.assert_called_once()
    enroll.assert_not_called()
