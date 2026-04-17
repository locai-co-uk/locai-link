# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import pytest


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
