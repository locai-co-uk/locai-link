# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import subprocess

import pytest


@pytest.fixture(autouse=True)
def _block_real_browser_opens(  # pyright: ignore[reportUnusedFunction]
    mocker,
):
    # (pytest discovers autouse fixtures by decorator, not by import —
    # static analysis can't see the callsite.)
    """Guard rail: no test may launch the system browser for real.

    Under pytest stdin isn't a TTY, so ``_device_flow`` takes the detached
    branch and shells out to ``xdg-open``/``open``/``cmd`` on a real URL,
    spamming the user's browser. This intercepts ``subprocess.run`` and no-ops
    those three openers (returning a success ``CompletedProcess``); everything
    else runs normally. Deliberately does NOT mock ``_open_in_browser`` or
    ``_running_detached`` — the onboarding tests exercise those natively, and
    per-test ``subprocess.run`` patches still stack on top of this one.
    """
    real_run = subprocess.run

    def _safe_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] in {"xdg-open", "open", "cmd"}:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        return real_run(cmd, *args, **kwargs)

    mocker.patch("subprocess.run", side_effect=_safe_run)


@pytest.fixture
def mock_zenoh_session(mocker):
    """Mocks the Zenoh session directly."""
    session = mocker.MagicMock()
    # Mock the info.zid() call used in health checks
    session.info.zid.return_value = "mock-id"
    return session


@pytest.fixture
def mock_state_manager(mocker):
    manager = mocker.MagicMock()
    manager.load_state.return_value = None  # Default: No previous state
    return manager


@pytest.fixture
def valid_config_dict():
    """Valid config with Single Sink."""
    return {
        "version": 2.1,
        "identity": {"device_id": "123", "device_name": "last_one"},
        "pipelines": [
            {
                "id": "p1",
                "source": {"type": "clock_tick", "args": {"interval": 0.1}},
                "sink": {"type": "console", "args": {"prefix": "TEST"}},
            }
        ],
    }
