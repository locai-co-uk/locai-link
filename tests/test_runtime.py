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


def _config_with_artifact(device_id, pipeline_id, model_path):
    """Config dict with one stopped pipeline whose source carries a model_path."""
    return {
        "version": 2.1,
        "identity": {"device_id": device_id},
        "pipelines": [
            {
                "id": pipeline_id,
                "source": {"type": "clock_tick", "args": {"interval": 0.1, "model_path": str(model_path)}},
                "sink": {"type": "console", "args": {}},
            }
        ],
    }


def test_uninstall_model_deletes_artifact_and_completes(mocker, mock_zenoh_session, mock_state_manager, tmp_path):
    """UNINSTALL_MODEL on a stopped pipeline unlinks the artifact and reports completed."""
    artifact = tmp_path / "m1.tflite"
    artifact.write_bytes(b"weights")
    agent = _make_agent(_config_with_artifact("d", "m1", artifact), mock_state_manager, mock_zenoh_session)
    mocker.patch.object(agent, "status_logger")

    agent.handle_command({"command_type": "uninstall_model", "id": "c1", "payload": {"model_id": "m1"}})

    assert not artifact.exists()
    assert "m1" not in agent.pipeline_configs
    mock_state_manager.remove_pipeline.assert_called_once_with("m1")
    agent.status_logger.report_model.assert_called_once()
    assert agent.status_logger.report_model.call_args.kwargs["installed"] is False
    assert agent.status_logger.report_command.call_args.args[1] == "completed"


def test_uninstall_running_pipeline_without_force_stop_fails(empty_agent, mocker, capfd):
    """A running pipeline is NOT removed when force_stop is absent/false."""
    empty_agent.handle_command(
        {
            "id": "start",
            "command": "START_MODEL",
            "payload": {
                "id": "live",
                "source": {"type": "clock_tick", "args": {}},
                "sink": {"type": "console", "args": {}},
            },
        }
    )
    assert "live" in empty_agent.pipelines
    mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command({"command_type": "uninstall_model", "id": "c2", "payload": {"model_id": "live"}})

    assert "live" in empty_agent.pipelines  # still running — refused
    assert empty_agent.status_logger.report_command.call_args.args[1] == "failed"
    empty_agent._shutdown()
    capfd.readouterr()


def test_uninstall_running_pipeline_with_force_stop_succeeds(empty_agent, mocker, capfd):
    """force_stop:true stops the live pipeline first, then uninstalls it."""
    empty_agent.handle_command(
        {
            "id": "start",
            "command": "START_MODEL",
            "payload": {
                "id": "live",
                "source": {"type": "clock_tick", "args": {}},
                "sink": {"type": "console", "args": {}},
            },
        }
    )
    assert "live" in empty_agent.pipelines
    mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command(
        {"command_type": "uninstall_model", "id": "c3", "payload": {"model_id": "live", "force_stop": True}}
    )

    assert "live" not in empty_agent.pipelines
    assert "live" not in empty_agent.pipeline_configs
    assert empty_agent.status_logger.report_command.call_args.args[1] == "completed"
    capfd.readouterr()


def test_uninstall_orphaned_file_fallback(mocker, empty_agent, tmp_path, monkeypatch):
    """With no local config, the payload filename locates the artifact under models/."""
    monkeypatch.chdir(tmp_path)
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    orphan = models_dir / "ghost.onnx"
    orphan.write_bytes(b"x")
    mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command(
        {
            "command_type": "uninstall_model",
            "id": "c4",
            "payload": {"model_id": "ghost", "filename_on_server": "ghost", "file_extension": "onnx"},
        }
    )

    assert not orphan.exists()
    assert empty_agent.status_logger.report_command.call_args.args[1] == "completed"


def test_remove_model_alias_still_handled(mocker, mock_zenoh_session, mock_state_manager, tmp_path):
    """The legacy REMOVE_MODEL command name remains supported."""
    artifact = tmp_path / "legacy.tflite"
    artifact.write_bytes(b"w")
    agent = _make_agent(_config_with_artifact("d", "legacy", artifact), mock_state_manager, mock_zenoh_session)
    mocker.patch.object(agent, "status_logger")

    agent.handle_command({"command": "REMOVE_MODEL", "id": "c5", "payload": {"id": "legacy"}})

    assert not artifact.exists()
    assert agent.status_logger.report_command.call_args.args[1] == "completed"
