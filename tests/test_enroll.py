# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for main.run routing decisions around key/fleet onboarding."""

import argparse

import pytest

import link.main as mainmod

API_URL = "https://api.test.local/api/v1"


@pytest.fixture(autouse=True)
def isolated_fleet_marker(tmp_path, monkeypatch):
    """Redirect the fleet-device marker into tmp so tests never touch configs/."""
    marker = tmp_path / ".fleet_device"
    monkeypatch.setattr(mainmod, "FLEET_MARKER_PATH", marker)
    return marker


@pytest.fixture(autouse=True)
def _no_runtime(mocker):
    """These tests exercise the onboarding ladder only; never start the runtime."""
    mocker.patch("link.main.AgentRuntime")
    mocker.patch("link.main.setup_logging")
    mocker.patch("link.main.get_or_create_zenoh_session")


def _args(**overrides):
    base = dict(
        config=None,
        registration_key=None,
        api_url=None,
        fleet_key=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_enrolls_when_no_session_and_fleet_key(mocker):
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device", return_value=mocker.MagicMock())

    mainmod.run(_args(fleet_key="flk_abc", api_url=API_URL))

    enroll.assert_called_once()
    assert enroll.call_args.kwargs["fleet_key"] == "flk_abc"
    sm.bootstrap.assert_called_once()


def test_fleet_key_env_is_ignored(mocker, monkeypatch):
    """The fleet key is CLI-only; the environment must never supply it."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main.load_config", return_value=mocker.MagicMock())
    monkeypatch.setenv("LOCAI_FLEET_KEY", "flk_from_env")

    mainmod.run(_args(fleet_key=None))

    enroll.assert_not_called()


def test_wiped_fleet_device_fails_loudly(mocker, isolated_fleet_marker):
    """Marker present + no session + no key = hard exit, never factory defaults."""
    isolated_fleet_marker.write_text("{}", encoding="utf-8")
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    load_config = mocker.patch("link.main.load_config")
    enroll = mocker.patch("link.main.enroll_device")

    with pytest.raises(SystemExit):
        mainmod.run(_args(fleet_key=None))

    enroll.assert_not_called()
    load_config.assert_not_called()


def test_existing_session_resumes_without_onboarding(mocker):
    """A session exists -> resume, no backend call."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = {"some": "persisted-state"}
    mocker.patch("link.main.StateManager", return_value=sm)
    mocker.patch("link.main.AgentConfig", return_value=mocker.MagicMock())
    enroll = mocker.patch("link.main.enroll_device")
    register = mocker.patch("link.main.register_with_key")

    mainmod.run(_args(fleet_key="flk_abc"))

    enroll.assert_not_called()
    register.assert_not_called()


def test_no_key_falls_back_to_defaults(mocker):
    """No session and no key -> fall through to defaults, never onboard."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device")
    register = mocker.patch("link.main.register_with_key")
    mocker.patch("link.main.load_config", return_value=mocker.MagicMock())

    mainmod.run(_args(fleet_key=None))

    enroll.assert_not_called()
    register.assert_not_called()


def test_registration_key_routes_to_register_with_key(mocker):
    """A registration key present -> register_with_key wins; enroll is skipped."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    register = mocker.patch("link.main.register_with_key", return_value=mocker.MagicMock())
    enroll = mocker.patch("link.main.enroll_device")

    mainmod.run(_args(registration_key="rk", fleet_key="flk_abc"))

    register.assert_called_once()
    assert register.call_args.kwargs["reg_key"] == "rk"
    enroll.assert_not_called()
