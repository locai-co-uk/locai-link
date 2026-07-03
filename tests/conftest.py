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

    `_device_flow` calls `_open_in_browser(url)` when
    `_running_detached()` is True. Under pytest stdin isn't a TTY, so
    the detached branch fires — which shells out to `xdg-open` /
    `open` / `cmd start` on the actual URL. Without this guard,
    running the suite spams the user's browser with device-flow URLs.

    Intercept `subprocess.run` at the boundary: any command with an
    argv[0] of `xdg-open` / `open` / `cmd` (the three OS-level
    browser openers) returns a success `CompletedProcess` without
    executing. Everything else runs normally.

    Deliberately does NOT mock `_open_in_browser` or
    `_running_detached` themselves — the four tests in
    `test_onboarding.py` that exercise those helpers directly need
    them to run natively. Tests that mock `subprocess.run` themselves
    (e.g. per-platform browser-opener tests) still work because their
    per-test patch stacks on top of this one.
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
