# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Mocked coverage for the one-shot CLI commands (`status`, `update`) and the
help layout `_CliHelpFormatter` exists to protect. Health endpoint and state
are stubbed: no service, no network."""

import argparse
from types import SimpleNamespace

import pytest
import requests

import link.main as main_mod


class _FakeStateManager:
    def __init__(self, saved):
        self._saved = saved

    def load_state(self):
        return self._saved


class _FakeConfig:
    """Stands in for AgentConfig: status only reads .identity fields."""

    def __init__(self, **kwargs):
        self.identity = SimpleNamespace(
            device_name="dev-1", device_id="d-123", api_url="https://api.example.invalid/api/v1"
        )


@pytest.fixture
def registered(monkeypatch):
    monkeypatch.setattr(main_mod, "StateManager", lambda: _FakeStateManager({"identity": {}}))
    monkeypatch.setattr(main_mod, "AgentConfig", _FakeConfig)


@pytest.fixture
def unregistered(monkeypatch):
    monkeypatch.setattr(main_mod, "StateManager", lambda: _FakeStateManager(None))


@pytest.fixture
def installed_version(monkeypatch):
    import link.utils.version

    monkeypatch.setattr(link.utils.version, "resolve_agent_version", lambda: "1.2.3")


def _health(monkeypatch, result):
    """Stub _health_get: an Exception instance/class raises, anything else returns."""

    def fake(path, timeout=3.0):
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, type) and issubclass(result, BaseException):
            raise result()
        return result

    monkeypatch.setattr(main_mod, "_health_get", fake)


# --- status ------------------------------------------------------------


def test_status_unregistered_idle(monkeypatch, capsys, unregistered, installed_version):
    _health(monkeypatch, requests.exceptions.ConnectionError)

    assert main_mod.status(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "not registered" in out
    assert "idle (awaiting registration)" in out


def test_status_registered_service_down(monkeypatch, capsys, registered, installed_version):
    _health(monkeypatch, requests.exceptions.ConnectionError)

    assert main_mod.status(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "dev-1 (d-123)" in out
    assert "not running" in out


def test_status_service_unreachable(monkeypatch, capsys, registered, installed_version):
    _health(monkeypatch, requests.exceptions.Timeout)

    assert main_mod.status(argparse.Namespace()) == 0
    assert "unreachable (Timeout)" in capsys.readouterr().out


def test_status_running_with_lag_and_update(monkeypatch, capsys, registered, installed_version):
    _health(
        monkeypatch,
        {
            "version": "1.2.9",
            "uptime_seconds": 42,
            "currently_serving": True,
            "model_id": "some-model",
            "transport": {"connected": True, "type": "zenoh"},
            "update_available": True,
            "latest_version": "1.3.0",
        },
    )

    assert main_mod.status(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "running (up 42s, running 1.2.9)" in out  # runtime lags the install
    assert "Serving:      some-model" in out
    assert "connected (zenoh)" in out
    assert "available -> 1.3.0" in out


def test_status_running_up_to_date(monkeypatch, capsys, registered, installed_version):
    _health(monkeypatch, {"version": "1.2.3", "uptime_seconds": 5})

    assert main_mod.status(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "running (up 5s)" in out  # same version: no lag suffix
    assert "up to date" in out


# --- update ------------------------------------------------------------


def _post(monkeypatch, status_code=None, exc=None):
    def fake(url, timeout=10):
        if exc is not None:
            raise exc
        return SimpleNamespace(status_code=status_code)

    monkeypatch.setattr(requests, "post", fake)


def test_update_service_not_running(monkeypatch, capsys):
    _health(monkeypatch, requests.exceptions.ConnectionError)

    assert main_mod.update(argparse.Namespace(force=False)) == 1
    assert "not running" in capsys.readouterr().out


def test_update_service_unreachable(monkeypatch, capsys):
    _health(monkeypatch, requests.exceptions.Timeout)

    assert main_mod.update(argparse.Namespace(force=False)) == 1
    assert "Could not reach" in capsys.readouterr().out


def test_update_already_latest(monkeypatch, capsys):
    _health(monkeypatch, {"update_available": False, "version": "1.2.3"})

    assert main_mod.update(argparse.Namespace(force=False)) == 0
    assert "Already at the latest version (1.2.3)" in capsys.readouterr().out


def test_update_accepted(monkeypatch, capsys):
    _health(monkeypatch, {"update_available": True, "latest_version": "1.3.0"})
    _post(monkeypatch, status_code=202)

    assert main_mod.update(argparse.Namespace(force=False)) == 0
    assert "Update -> 1.3.0 accepted" in capsys.readouterr().out


def test_update_force_hits_endpoint_and_maps_409(monkeypatch, capsys):
    _health(monkeypatch, {"update_available": False})
    _post(monkeypatch, status_code=409)

    assert main_mod.update(argparse.Namespace(force=True)) == 0
    assert "No installable update" in capsys.readouterr().out


def test_update_service_starting_503(monkeypatch, capsys):
    _health(monkeypatch, {"update_available": True})
    _post(monkeypatch, status_code=503)

    assert main_mod.update(argparse.Namespace(force=False)) == 1
    assert "still starting" in capsys.readouterr().out


def test_update_unexpected_http_code(monkeypatch, capsys):
    _health(monkeypatch, {"update_available": True})
    _post(monkeypatch, status_code=500)

    assert main_mod.update(argparse.Namespace(force=False)) == 1
    assert "HTTP 500" in capsys.readouterr().out


def test_update_post_failure(monkeypatch, capsys):
    _health(monkeypatch, {"update_available": True})
    _post(monkeypatch, exc=OSError("boom"))

    assert main_mod.update(argparse.Namespace(force=False)) == 1
    assert "Update request failed (OSError)" in capsys.readouterr().out


# --- help layout ---------------------------------------------------------


def test_help_keeps_each_subcommand_on_one_line():
    """_CliHelpFormatter exists so the longest subcommand's help text stays on
    the same line as its name instead of wrapping (stock argparse under-counts
    the subcommand indent)."""
    help_text = main_mod._build_parser().format_help()
    lines = help_text.splitlines()

    expectations = {
        "register": "Register this device with a key, then exit.",
        "status": "Show registration, service, and update status.",
        "update": "Update the running service to the latest version.",
    }
    for command, blurb in expectations.items():
        matches = [ln for ln in lines if ln.lstrip().startswith(command) and blurb in ln]
        assert matches, f"help for {command!r} not on one line:\n{help_text}"
