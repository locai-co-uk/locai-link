# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for main.run routing decisions around fleet enrollment."""

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


def _install_args(**overrides):
    base = dict(
        repo_url="https://example.invalid/repo.git",
        branch="main",
        device_name=None,
        email=None,
        password=None,
        token=None,
        registration_key=None,
        fleet_key=None,
        device_type="other",
        start_running=False,
        api_url=None,
        dev=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_install_fleet_key_skips_interactive_flow(mocker, tmp_path, monkeypatch):
    """install --fleet-key must not prompt and must forward the key to run."""
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    run_mock = mocker.patch("link.main.subprocess.run")
    mocker.patch("builtins.input", side_effect=AssertionError("must not prompt"))

    mainmod.install(_install_args(fleet_key="flk_abc", api_url=API_URL))

    cmds = [c.args[0] for c in run_mock.call_args_list]
    reg = [c for c in cmds if "--fleet-key" in c]
    assert reg == [["uv", "run", "main.py", "run", "--fleet-key", "flk_abc", "--api-url", API_URL]]


def test_headless_enrolls_when_no_session_and_fleet_key(mocker):
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device", return_value=mocker.MagicMock())
    mocker.patch("link.main._deploy_service")

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
    mocker.patch("link.main._deploy_service")
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
    enroll = mocker.patch("link.main.enroll_device")
    load_config = mocker.patch("link.main.load_config")
    mocker.patch("link.main._deploy_service")

    with pytest.raises(SystemExit):
        mainmod.run(_args(fleet_key=None))

    enroll.assert_not_called()
    load_config.assert_not_called()


def test_existing_session_resumes_without_enrolling(mocker):
    """A session exists -> resume, no backend call."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = {"some": "persisted-state"}
    mocker.patch("link.main.StateManager", return_value=sm)
    mocker.patch("link.main.AgentConfig", return_value=mocker.MagicMock())
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main._deploy_service")

    mainmod.run(_args(fleet_key="flk_abc"))

    enroll.assert_not_called()


def test_no_fleet_key_does_not_enroll(mocker):
    """No session and no fleet key -> fall through to defaults, never enroll."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main._deploy_service")
    mocker.patch("link.main.load_config", return_value=mocker.MagicMock())

    mainmod.run(_args(fleet_key=None))

    enroll.assert_not_called()


def test_registration_key_takes_interactive_path_over_enroll(mocker):
    """A registration key present -> interactive register path wins; enroll is skipped."""
    sm = mocker.MagicMock()
    sm.load_state.return_value = None
    mocker.patch("link.main.StateManager", return_value=sm)
    register = mocker.patch("link.main.register_device", return_value=mocker.MagicMock())
    enroll = mocker.patch("link.main.enroll_device")
    mocker.patch("link.main._deploy_service")

    mainmod.run(_args(registration_key="rk", device_name="edge", token="jwt", fleet_key="flk_abc"))

    register.assert_called_once()
    enroll.assert_not_called()
