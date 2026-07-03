# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import threading
from typing import Any

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
        "type": "START_MODEL",
        "pipeline_id": "dynamic",
        "config": {
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

    empty_agent.handle_command({"id": "cmd-1", "type": "UPDATE_AGENT"})

    assert empty_agent.update_requested is True
    assert empty_agent.running is False
    assert empty_agent.shutdown_event.is_set()


def test_update_agent_flag_default_false(empty_agent):
    """update_requested defaults to False when no update command is issued."""
    empty_agent.handle_command({"id": "cmd-1", "type": "STATUS"})
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
    status_logger = mocker.patch.object(agent, "status_logger")

    agent.handle_command({"id": "c1", "type": "UNINSTALL_MODEL", "pipeline_id": "m1"})

    assert not artifact.exists()
    assert "m1" not in agent.pipeline_configs
    mock_state_manager.remove_pipeline.assert_called_once_with("m1")
    status_logger.report_model.assert_called_once()
    assert status_logger.report_model.call_args.kwargs["installed"] is False
    assert status_logger.report_command.call_args.args[1] == "completed"


def test_uninstall_running_pipeline_without_force_stop_fails(empty_agent, mocker, capfd):
    """A running pipeline is NOT removed when force_stop is absent/false."""
    empty_agent.handle_command(
        {
            "id": "start",
            "type": "START_MODEL",
            "pipeline_id": "live",
            "config": {
                "id": "live",
                "source": {"type": "clock_tick", "args": {}},
                "sink": {"type": "console", "args": {}},
            },
        }
    )
    assert "live" in empty_agent.pipelines
    status_logger = mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command({"id": "c2", "type": "UNINSTALL_MODEL", "pipeline_id": "live"})

    assert "live" in empty_agent.pipelines  # still running — refused
    assert status_logger.report_command.call_args.args[1] == "failed"
    empty_agent._shutdown()
    capfd.readouterr()


def test_uninstall_running_pipeline_with_force_stop_succeeds(empty_agent, mocker, capfd):
    """force_stop:true stops the live pipeline first, then uninstalls it."""
    empty_agent.handle_command(
        {
            "id": "start",
            "type": "START_MODEL",
            "pipeline_id": "live",
            "config": {
                "id": "live",
                "source": {"type": "clock_tick", "args": {}},
                "sink": {"type": "console", "args": {}},
            },
        }
    )
    assert "live" in empty_agent.pipelines
    status_logger = mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command({"id": "c3", "type": "UNINSTALL_MODEL", "pipeline_id": "live", "force_stop": True})

    assert "live" not in empty_agent.pipelines
    assert "live" not in empty_agent.pipeline_configs
    assert status_logger.report_command.call_args.args[1] == "completed"
    capfd.readouterr()


def test_uninstall_orphaned_file_fallback(mocker, empty_agent, tmp_path, monkeypatch):
    """With no local config, the payload filename locates the artifact under models/."""
    monkeypatch.chdir(tmp_path)
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    orphan = models_dir / "ghost.onnx"
    orphan.write_bytes(b"x")
    status_logger = mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command(
        {
            "id": "c4",
            "type": "UNINSTALL_MODEL",
            "pipeline_id": "ghost",
            "filename_on_server": "ghost",
            "file_extension": "onnx",
        }
    )

    assert not orphan.exists()
    assert status_logger.report_command.call_args.args[1] == "completed"


def test_legacy_command_shape_is_rejected(mocker, mock_zenoh_session, mock_state_manager, tmp_path):
    """A retired loose-shape command (command/payload) fails validation and reports failed.

    The flat typed contract replaced the old envelope, and the REMOVE_MODEL
    alias was dropped. Such a command no longer validates, so it is reported
    failed (it carries an id) and nothing is acted on.
    """
    artifact = tmp_path / "legacy.tflite"
    artifact.write_bytes(b"w")
    agent = _make_agent(_config_with_artifact("d", "legacy", artifact), mock_state_manager, mock_zenoh_session)
    status_logger = mocker.patch.object(agent, "status_logger")

    agent.handle_command({"command": "REMOVE_MODEL", "id": "c5", "payload": {"id": "legacy"}})

    assert artifact.exists()  # rejected - nothing removed
    assert "legacy" in agent.pipeline_configs
    assert status_logger.report_command.call_args.args[1] == "failed"


def test_deploy_model_stores_backend_pipeline(empty_agent, mocker, tmp_path, monkeypatch):
    """DEPLOY_MODEL stores the backend-provided config verbatim (no mapping)."""
    monkeypatch.chdir(tmp_path)
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "m.gguf").write_bytes(b"weights")  # pre-existing → download skipped
    status_logger = mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command(
        {
            "id": "d1",
            "type": "DEPLOY_MODEL",
            "pipeline_id": "m1",
            "model_name": "m.gguf",
            "config": {
                "id": "m1",
                "active": False,
                "source": {"type": "clock_tick", "args": {"model_path": "models/m.gguf"}},
                "sink": {"type": "console", "args": {}},
            },
        }
    )

    stored = empty_agent.pipeline_configs["m1"]
    assert stored.source.type == "clock_tick"
    assert stored.source.args["model_path"] == "models/m.gguf"
    assert status_logger.report_command.call_args.args[1] == "completed"


def test_update_pipeline_stores_config_when_not_running(empty_agent, mocker):
    """UPDATE_PIPELINE on a stopped pipeline stores the new config without starting it."""
    status_logger = mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command(
        {
            "id": "u1",
            "type": "UPDATE_PIPELINE",
            "pipeline_id": "m1",
            "config": {
                "id": "m1",
                "source": {"type": "clock_tick", "args": {"interval": 0.5}},
                "sink": {"type": "console", "args": {}},
            },
        }
    )

    assert empty_agent.pipeline_configs["m1"].source.args["interval"] == 0.5
    assert "m1" not in empty_agent.pipelines  # stored only, not started
    assert status_logger.report_command.call_args.args[1] == "completed"


def test_update_pipeline_restarts_running_pipeline(empty_agent, mocker, capfd):
    """UPDATE_PIPELINE on a running pipeline restarts it with the new config."""
    empty_agent.handle_command(
        {
            "id": "start",
            "type": "START_MODEL",
            "pipeline_id": "live",
            "config": {
                "id": "live",
                "source": {"type": "clock_tick", "args": {}},
                "sink": {"type": "console", "args": {}},
            },
        }
    )
    assert "live" in empty_agent.pipelines
    status_logger = mocker.patch.object(empty_agent, "status_logger")

    empty_agent.handle_command(
        {
            "id": "u2",
            "type": "UPDATE_PIPELINE",
            "pipeline_id": "live",
            "config": {
                "id": "live",
                "source": {"type": "clock_tick", "args": {"interval": 2.0}},
                "sink": {"type": "console", "args": {}},
            },
        }
    )

    assert "live" in empty_agent.pipelines  # restarted, still running
    assert empty_agent.pipeline_configs["live"].source.args["interval"] == 2.0
    assert status_logger.report_command.call_args.args[1] == "completed"
    empty_agent._shutdown()
    capfd.readouterr()


def _serve_pipeline(pid, port, mode: str | None = "serve"):
    args: dict[str, Any] = {"interval": 0.1}
    if mode is not None:
        args["mode"] = mode
        args["port"] = port
        args["host"] = "127.0.0.1"
        args["alias"] = "alias"
    return {
        "id": pid,
        "active": True,
        "source": {"type": "clock_tick", "args": args},
        "sink": {"type": "console", "args": {}},
    }


def _inference_pipeline(pid, model_path="models/m.gguf"):
    return {
        "id": pid,
        "active": True,
        "source": {"type": "clock_tick", "args": {"interval": 0.1, "model_path": model_path}},
        "sink": {"type": "console", "args": {}},
    }


def test_resume_emits_serving_status_for_serve_pipelines(mocker, mock_zenoh_session, mock_state_manager):
    """Auto-resume of a serve-mode pipeline re-announces serving=True to Control.

    Without this, Control keeps showing the model as not-serving after a Link
    restart, because the StartServingCommand handler (which normally emits the
    report) is bypassed when pipelines are recovered from the session file.
    """
    mock_state_manager.load_state.return_value = {"pipelines": [_serve_pipeline("served", 8081)]}
    config = AgentConfig.model_validate({"version": 2.1, "identity": {"device_id": "d"}, "pipelines": []})
    agent = AgentRuntime(config, mock_state_manager, mock_zenoh_session)
    status_logger = mocker.patch.object(agent, "status_logger")
    # Isolate the resume-loop behaviour from real pipeline construction —
    # clock_tick (used for test fixtures) rejects the serve-mode args.
    mocker.patch.object(agent, "_start_pipeline", return_value=True)
    agent.shutdown_event.set()  # exit the wait loop immediately after recovery

    agent.run()

    serving_calls = [c for c in status_logger.report_model.call_args_list if c.kwargs.get("serving") is True]
    assert len(serving_calls) == 1
    assert serving_calls[0].args[0] == "served"
    assert serving_calls[0].kwargs["serving_port"] == 8081
    agent._shutdown()


def test_resume_does_not_emit_serving_for_non_serve_pipelines(mocker, mock_zenoh_session, mock_state_manager):
    """Auto-resume of a non-serve pipeline (e.g. command poller) must not claim it is serving."""
    mock_state_manager.load_state.return_value = {"pipelines": [_serve_pipeline("ticker", 0, mode=None)]}
    config = AgentConfig.model_validate({"version": 2.1, "identity": {"device_id": "d"}, "pipelines": []})
    agent = AgentRuntime(config, mock_state_manager, mock_zenoh_session)
    status_logger = mocker.patch.object(agent, "status_logger")
    mocker.patch.object(agent, "_start_pipeline", return_value=True)
    agent.shutdown_event.set()

    agent.run()

    serving_calls = [c for c in status_logger.report_model.call_args_list if c.kwargs.get("serving") is True]
    assert serving_calls == []
    agent._shutdown()


def test_resume_emits_running_status_for_inference_pipelines(mocker, mock_zenoh_session, mock_state_manager):
    """Auto-resume of an inference-mode model pipeline re-announces running=True to Control.

    Symmetric with the serve-mode case: the StartModelInferenceCommand handler
    is bypassed on auto-resume, so without this Control would keep showing the
    model as not-running after a Link restart.
    """
    mock_state_manager.load_state.return_value = {"pipelines": [_inference_pipeline("infer-model")]}
    config = AgentConfig.model_validate({"version": 2.1, "identity": {"device_id": "d"}, "pipelines": []})
    agent = AgentRuntime(config, mock_state_manager, mock_zenoh_session)
    status_logger = mocker.patch.object(agent, "status_logger")
    mocker.patch.object(agent, "_start_pipeline", return_value=True)
    agent.shutdown_event.set()

    agent.run()

    running_calls = [c for c in status_logger.report_model.call_args_list if c.kwargs.get("running") is True]
    assert len(running_calls) == 1
    assert running_calls[0].args[0] == "infer-model"
    assert running_calls[0].kwargs["serving"] is False
    agent._shutdown()


def test_resume_does_not_emit_running_for_non_model_pipelines(mocker, mock_zenoh_session, mock_state_manager):
    """Telemetry/poller pipelines without a model_path must not be reported as running models."""
    mock_state_manager.load_state.return_value = {"pipelines": [_serve_pipeline("ticker", 0, mode=None)]}
    config = AgentConfig.model_validate({"version": 2.1, "identity": {"device_id": "d"}, "pipelines": []})
    agent = AgentRuntime(config, mock_state_manager, mock_zenoh_session)
    status_logger = mocker.patch.object(agent, "status_logger")
    mocker.patch.object(agent, "_start_pipeline", return_value=True)
    agent.shutdown_event.set()

    agent.run()

    model_status_calls = status_logger.report_model.call_args_list
    assert model_status_calls == []
    agent._shutdown()


def test_resume_swallows_reporter_failure(mocker, mock_zenoh_session, mock_state_manager):
    """A status-logger exception during resume must NOT abort run().

    The recovery block sits above the try/finally that owns _shutdown() and
    the offline lifecycle report. Without local exception handling, a flaky
    reporter would stop pipelines from being torn down cleanly on the next
    shutdown and leave Control without an offline event.
    """
    mock_state_manager.load_state.return_value = {"pipelines": [_serve_pipeline("served", 8081)]}
    config = AgentConfig.model_validate({"version": 2.1, "identity": {"device_id": "d"}, "pipelines": []})
    agent = AgentRuntime(config, mock_state_manager, mock_zenoh_session)
    status_logger = mocker.patch.object(agent, "status_logger")
    status_logger.report_model.side_effect = RuntimeError("reporter exploded")
    mocker.patch.object(agent, "_start_pipeline", return_value=True)
    agent.shutdown_event.set()

    agent.run()  # must not raise

    status_logger.report_lifecycle.assert_any_call("offline")
    agent._shutdown()


def test_resume_skips_report_when_start_fails(mocker, mock_zenoh_session, mock_state_manager):
    """A failed resume must not emit serving=True — Control would then show a phantom serve."""
    mock_state_manager.load_state.return_value = {"pipelines": [_serve_pipeline("broken", 9000)]}
    config = AgentConfig.model_validate({"version": 2.1, "identity": {"device_id": "d"}, "pipelines": []})
    agent = AgentRuntime(config, mock_state_manager, mock_zenoh_session)
    status_logger = mocker.patch.object(agent, "status_logger")
    mocker.patch.object(agent, "_start_pipeline", return_value=False)
    agent.shutdown_event.set()

    agent.run()

    serving_calls = [c for c in status_logger.report_model.call_args_list if c.kwargs.get("serving") is True]
    assert serving_calls == []
    agent._shutdown()


# --- _snapshot_models --------------------------------------------------------


def _pipe_with_model(pid: str, *, alias: str, port: int, mode: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"model_path": f"models/{pid}.gguf", "alias": alias, "port": port, "host": "127.0.0.1"}
    if mode is not None:
        args["mode"] = mode
    return {
        "id": pid,
        "active": False,
        "source": {"type": "clock_tick", "args": args},
        "sink": {"type": "console", "args": {}},
    }


def _pipe_without_model(pid: str) -> dict[str, Any]:
    return {
        "id": pid,
        "active": False,
        "source": {"type": "clock_tick", "args": {"interval": 0.1}},
        "sink": {"type": "console", "args": {}},
    }


def test_snapshot_models_filters_out_non_model_pipelines(mock_zenoh_session, mock_state_manager):
    """_snapshot_models is the /models data source. Pipelines without a
    `model_path` in source args (poller loops, telemetry, etc.) must
    not appear."""
    cfg = AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "d"},
            "pipelines": [
                _pipe_with_model("llm1", alias="Llama-8B", port=8080),
                _pipe_without_model("telemetry"),
                _pipe_with_model("llm2", alias="SmolLM", port=8100),
            ],
        }
    )
    agent = AgentRuntime(cfg, mock_state_manager, mock_zenoh_session)
    models = agent._snapshot_models()
    ids = sorted(m["id"] for m in models)
    assert ids == ["llm1", "llm2"], "only model-bearing pipelines belong in /models"


def test_snapshot_models_reports_is_serving_for_active_serve_pipelines(mock_zenoh_session, mock_state_manager):
    """is_serving must reflect BOTH `in self.pipelines` (running) AND
    `source.args["mode"] == "serve"` — because a pipeline can be
    running in inference mode without serving traffic."""
    cfg = AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "d"},
            "pipelines": [
                _pipe_with_model("running_serve", alias="A", port=8080, mode="serve"),
                _pipe_with_model("running_inference", alias="B", port=8080, mode="inference"),
                _pipe_with_model("stopped", alias="C", port=8080, mode="serve"),
            ],
        }
    )
    agent = AgentRuntime(cfg, mock_state_manager, mock_zenoh_session)
    # Simulate the first two being live; leave "stopped" out.
    agent.pipelines["running_serve"] = object()  # type: ignore[assignment]
    agent.pipelines["running_inference"] = object()  # type: ignore[assignment]

    by_id = {m["id"]: m for m in agent._snapshot_models()}
    assert by_id["running_serve"]["is_serving"] is True
    assert by_id["running_inference"]["is_serving"] is False, "inference-mode isn't serving"
    assert by_id["stopped"]["is_serving"] is False, "not-in-pipelines isn't serving"


def test_snapshot_models_defaults_alias_to_pipeline_id(mock_zenoh_session, mock_state_manager):
    """If a pipeline has no `alias` in args, the id is used as the
    display name so the tray menu never shows a blank row."""
    cfg = AgentConfig.model_validate(
        {
            "version": 2.1,
            "identity": {"device_id": "d"},
            "pipelines": [
                {
                    "id": "aliasless",
                    "active": False,
                    "source": {
                        "type": "clock_tick",
                        "args": {"model_path": "models/x.gguf", "port": 8080, "host": "127.0.0.1"},
                    },
                    "sink": {"type": "console", "args": {}},
                }
            ],
        }
    )
    agent = AgentRuntime(cfg, mock_state_manager, mock_zenoh_session)
    [m] = agent._snapshot_models()
    assert m["alias"] == "aliasless"
