# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import threading

from link.app.runtime import AgentRuntime
from link.config.models import AgentConfig


def test_agent_init(mock_zenoh_session, mock_state_manager, valid_config_dict):
    """Test runtime initialization with a session."""
    config = AgentConfig(**valid_config_dict)

    # Updated signature: pass session directly
    agent = AgentRuntime(config, mock_zenoh_session, mock_state_manager)
    assert "p1" in agent.pipeline_configs


def test_handle_start_command(mock_zenoh_session, mock_state_manager):
    """Test dynamic pipeline start with single sink."""
    config = AgentConfig(version=2.1, identity={"device_id": "d"}, pipelines=[])

    # Updated signature
    agent = AgentRuntime(config, mock_zenoh_session, mock_state_manager)

    cmd = {
        "id": "1",
        "command": "START_MODEL",
        "payload": {
            "id": "dynamic",
            "source": {"type": "clock_tick", "args": {}},
            "sink": {"type": "console", "args": {}},
        },
    }

    agent.handle_command(cmd)
    assert "dynamic" in agent.pipelines
    assert agent.pipelines["dynamic"].is_alive()


def test_graceful_shutdown(mock_zenoh_session, mock_state_manager):
    """Test that the run loop exits on signal."""
    config = AgentConfig(version=2.1, identity={"device_id": "d"}, pipelines=[])
    agent = AgentRuntime(config, mock_zenoh_session, mock_state_manager)

    t = threading.Thread(target=agent.run, daemon=True)
    t.start()

    agent._signal_handler(15, None)
    t.join(timeout=1.0)
    assert not t.is_alive()


def test_update_agent_command_sets_flag_and_shuts_down(mock_zenoh_session, mock_state_manager):
    """UPDATE_AGENT sets the update_requested flag and triggers shutdown."""
    config = AgentConfig(version=2.1, identity={"device_id": "d"}, pipelines=[])
    agent = AgentRuntime(config, mock_zenoh_session, mock_state_manager)

    assert agent.update_requested is False

    agent.handle_command({"id": "cmd-1", "command": "UPDATE_AGENT", "payload": {}})

    assert agent.update_requested is True
    assert agent.running is False
    assert agent.shutdown_event.is_set()


def test_update_agent_flag_default_false(mock_zenoh_session, mock_state_manager):
    """update_requested defaults to False when no update command is issued."""
    config = AgentConfig(version=2.1, identity={"device_id": "d"}, pipelines=[])
    agent = AgentRuntime(config, mock_zenoh_session, mock_state_manager)

    # Some other command
    agent.handle_command({"id": "cmd-1", "command": "STATUS", "payload": {}})

    assert agent.update_requested is False
