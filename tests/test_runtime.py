# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import threading

import pytest

from link.app.runtime import AgentRuntime
from link.config.models import AgentConfig


def _make_agent(valid_config_dict, mock_state_manager, mock_zenoh_session):
    config = AgentConfig.model_validate(valid_config_dict)
    return AgentRuntime(config, mock_state_manager, mock_zenoh_session)


@pytest.fixture
def empty_agent(mock_zenoh_session, mock_state_manager):
    """AgentRuntime with an empty config and automatic pipeline teardown."""
    config = AgentConfig.model_validate({"version": 2.1, "identity": {"device_id": "d"}, "pipelines": []})
    runtime = AgentRuntime(config, mock_state_manager, mock_zenoh_session)
    yield runtime
    runtime._shutdown()


def test_agent_init(mock_zenoh_session, mock_state_manager, valid_config_dict):
    """Pipeline configs are registered from the initial AgentConfig."""
    agent = _make_agent(valid_config_dict, mock_state_manager, mock_zenoh_session)
    assert "p1" in agent.pipeline_configs


def test_handle_start_command(empty_agent, capfd):
    """START_MODEL dynamically spawns a new pipeline thread."""
    cmd = {
        "id": "1",
        "command": "START_MODEL",
        "payload": {
            "id": "dynamic",
            "source": {"type": "clock_tick", "args": {}},
            "sink": {"type": "console", "args": {}},
        },
    }

    empty_agent.handle_command(cmd)
    assert "dynamic" in empty_agent.pipelines
    assert empty_agent.pipelines["dynamic"].is_alive()
    # Stop the pipeline before teardown captures its first clock_tick emission.
    empty_agent._shutdown()
    capfd.readouterr()


def test_graceful_shutdown(empty_agent):
    """The run loop exits on SIGTERM."""
    t = threading.Thread(target=empty_agent.run, daemon=True)
    t.start()

    empty_agent._signal_handler(15, None)
    t.join(timeout=1.0)
    assert not t.is_alive()


def test_update_agent_command_sets_flag_and_shuts_down(empty_agent):
    """UPDATE_AGENT sets the update_requested flag and triggers shutdown."""
    assert empty_agent.update_requested is False

    empty_agent.handle_command({"id": "cmd-1", "command": "UPDATE_AGENT", "payload": {}})

    assert empty_agent.update_requested is True
    assert empty_agent.running is False
    assert empty_agent.shutdown_event.is_set()


def test_update_agent_flag_default_false(empty_agent):
    """update_requested defaults to False when no update command is issued."""
    empty_agent.handle_command({"id": "cmd-1", "command": "STATUS", "payload": {}})
    assert empty_agent.update_requested is False
