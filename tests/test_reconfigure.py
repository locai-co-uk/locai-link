# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for hot-reconfiguration via UPDATE_AGENT_CONFIG."""

import pytest

from link.app.reconfigure import apply_agent_config
from link.app.runtime import AgentRuntime
from link.config.models import AgentConfig


def _baseline_config():
    return AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "dev-1", "api_url": "https://api.test", "api_key": "key-1"},
            "pipelines": [
                {
                    "id": "existing",
                    "active": False,  # Inactive — we test config swap, not pipeline emission.
                    "source": {"type": "clock_tick", "args": {"interval": 1.0}},
                    "sink": {"type": "console"},
                }
            ],
        }
    )


@pytest.fixture
def agent(mock_zenoh_session, mock_state_manager):
    """AgentRuntime fixture with automatic pipeline teardown between tests."""
    runtime = AgentRuntime(_baseline_config(), mock_state_manager, mock_zenoh_session)
    yield runtime
    runtime._shutdown()


# --- Happy paths ---


def test_apply_adds_new_pipeline(agent):
    new_cfg = {
        "version": 2.1,
        "identity": {"device_id": "dev-1", "api_url": "https://api.test", "api_key": "key-1"},
        "pipelines": [
            {
                "id": "existing",
                "active": False,
                "source": {"type": "clock_tick"},
                "sink": {"type": "console"},
            },
            {
                "id": "new_one",
                "active": False,  # Don't emit during test
                "source": {"type": "clock_tick", "args": {"interval": 2.0}},
                "sink": {"type": "console"},
            },
        ],
    }

    result = apply_agent_config(agent, new_cfg)
    assert result.ok
    assert "new_one" in agent.pipeline_configs
    assert "existing" in agent.pipeline_configs


def test_apply_removes_pipeline(agent):
    new_cfg = {
        "version": 2.1,
        "identity": {"device_id": "dev-1", "api_url": "https://api.test", "api_key": "key-1"},
        "pipelines": [],  # Remove "existing"
    }

    result = apply_agent_config(agent, new_cfg)
    assert result.ok
    assert agent.pipeline_configs == {}


def test_apply_replaces_full_config(agent):
    new_cfg = {
        "version": 2.1,
        "identity": {"device_id": "dev-1", "api_url": "https://api.test", "api_key": "key-1"},
        "reporting": {"interval": 120, "handlers": []},
        "pipelines": [],
    }

    result = apply_agent_config(agent, new_cfg)
    assert result.ok
    assert agent.agent_config.reporting.interval == 120


# --- Validation failures ---


def test_reject_wrong_version(agent):
    result = apply_agent_config(
        agent,
        {"version": 9.9, "identity": {"device_id": "dev-1"}, "pipelines": []},
    )
    assert not result.ok
    assert "Unsupported version" in result.message


def test_reject_malformed_config(agent):
    # Pipeline missing required `source`
    result = apply_agent_config(
        agent,
        {
            "version": 2.1,
            "identity": {"device_id": "dev-1", "api_url": "https://api.test", "api_key": "key-1"},
            "pipelines": [{"id": "broken", "sink": {"type": "console"}}],
        },
    )
    assert not result.ok
    assert "Invalid config" in result.message


def test_reject_identity_drift(agent):
    # Backend tries to sneak in a different device_id
    result = apply_agent_config(
        agent,
        {
            "version": 2.1,
            "identity": {
                "device_id": "ATTACKER",
                "api_url": "https://api.test",
                "api_key": "key-1",
            },
            "pipelines": [],
        },
    )
    assert not result.ok
    assert "Identity drift" in result.message


def test_rejected_config_leaves_runtime_unchanged(agent):
    original_pipelines = dict(agent.pipeline_configs)

    apply_agent_config(
        agent,
        {"version": 9.9, "identity": {"device_id": "dev-1"}, "pipelines": []},
    )

    # Still has the original pipeline
    assert agent.pipeline_configs == original_pipelines


# --- Template resolution ---


def test_identity_templates_resolved(agent):
    new_cfg = {
        "version": 2.1,
        "identity": {
            "device_id": "${identity.device_id}",
            "api_url": "${identity.api_url}",
            "api_key": "${identity.api_key}",
        },
        "pipelines": [
            {
                "id": "templated",
                "active": False,
                "source": {
                    "type": "http_poll",
                    "args": {
                        "url": "${identity.api_url}/agent/${identity.device_id}/commands",
                        "api_key": "${identity.api_key}",
                    },
                },
                "sink": {"type": "command"},
            }
        ],
    }

    result = apply_agent_config(agent, new_cfg)
    assert result.ok

    templated = agent.pipeline_configs["templated"]
    assert templated.source.args["url"] == "https://api.test/agent/dev-1/commands"
    assert templated.source.args["api_key"] == "key-1"


# --- Command dispatch ---


def test_dispatch_via_handle_command(agent):
    cmd = {
        "id": "cmd-1",
        "type": "UPDATE_AGENT_CONFIG",
        "agent_config": {
            "version": 2.1,
            "identity": {
                "device_id": "dev-1",
                "api_url": "https://api.test",
                "api_key": "key-1",
            },
            "pipelines": [],
        },
    }

    agent.handle_command(cmd)
    assert agent.pipeline_configs == {}


def test_dispatch_rejects_missing_agent_config(agent):
    original = dict(agent.pipeline_configs)

    # agent_config is required by the schema; without it the command fails
    # validation and is reported failed, leaving state untouched.
    cmd = {"id": "cmd-1", "type": "UPDATE_AGENT_CONFIG"}
    agent.handle_command(cmd)

    # Unchanged
    assert agent.pipeline_configs == original


# --- State persistence ---


def test_state_manager_receives_new_config(agent, mock_state_manager):
    new_cfg = {
        "version": 2.1,
        "identity": {"device_id": "dev-1", "api_url": "https://api.test", "api_key": "key-1"},
        "pipelines": [],
    }
    apply_agent_config(agent, new_cfg)

    mock_state_manager.update_full_config.assert_called_once()
