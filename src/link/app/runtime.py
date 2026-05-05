# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Agent runtime — owns the pipeline lifecycle, command dispatch, and shutdown flow."""

import logging
import signal
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import requests

import link.components  # noqa: F401
from link.app.state import StateManager
from link.components.pipeline import Pipeline
from link.components.registry import Component, ComponentRegistry
from link.config.models import AgentConfig, GenericConfig, PipelineConfig
from link.utils.logger import LinkReporter

if TYPE_CHECKING:
    import zenoh

# Standard logger for debug/info text
logger = logging.getLogger(__name__)


class AgentRuntime:
    """Manages the lifecycle of device inference pipelines."""

    def __init__(
        self,
        agent_config: AgentConfig,
        state_manager: StateManager | None = None,
        zenoh_session: "zenoh.Session | None" = None,
    ):
        """Initialise the runtime with agent configuration and optional managers.

        Args:
            agent_config (AgentConfig): The agent configuration.
            state_manager (StateManager | None): The state manager instance. Defaults to None.
            zenoh_session (zenoh.Session | None): The Zenoh session. Defaults to None.
        """
        self.agent_config = agent_config
        self.state_manager = state_manager
        self.zenoh_session = zenoh_session
        self.status_logger = cast(LinkReporter, logging.getLogger("link.reporter"))

        self.pipeline_configs = {p.id: p for p in agent_config.pipelines}
        self.pipelines = {}

        self.lock = threading.RLock()
        self.running = True
        self.shutdown_event = threading.Event()
        self.update_requested = False
        self.config_restart_requested = False

        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._signal_handler)
                signal.signal(signal.SIGTERM, self._signal_handler)
            except ValueError:
                logger.debug("Signal handlers skipped (not main thread).")

    def handle_command(self, data: dict):
        """Handles incoming commands from the platform.

        Args:
            data (dict): The command data dictionary.
        """
        if not isinstance(data, dict):
            logger.warning(f"Invalid command format: expected dict, got {type(data)}")
            return

        # 1. Normalise Command
        cmd_type, cmd_id, pipeline_id, payload = self._normalise_command(data)

        if not cmd_type or not cmd_id:
            logger.warning(f"Command rejected: Missing command type or id. Keys: {list(data.keys())}")
            return

        logger.info(f"Processing command: {cmd_type} (Target: {pipeline_id})")

        try:
            if cmd_type == "DEPLOY_MODEL":
                if not cmd_id:
                    logger.warning("DEPLOY command rejected: Missing 'id'")
                    return
                self._deploy_model(cmd_id, payload)

            elif cmd_type == "START_MODEL":
                config = payload.get("config") or data.get("config")
                if not config and "source" in payload:
                    config = payload

                if not pipeline_id:
                    msg = "START command rejected: Missing pipeline ID"
                    logger.warning(msg)
                    self.status_logger.report_command(cmd_id, "failed", msg)
                    return

                success = self._start_pipeline(pipeline_id, config)
                if success:
                    self.status_logger.report_command(cmd_id, "completed", f"Pipeline {pipeline_id} started")
                else:
                    self.status_logger.report_command(cmd_id, "failed", f"Failed to start {pipeline_id}")

            elif cmd_type == "START_MODEL_INFERENCE":
                if not pipeline_id:
                    msg = "START command rejected: Could not identify target pipeline."
                    logger.warning(msg)
                    self.status_logger.report_command(cmd_id, "failed", msg)
                    return

                success = self._start_pipeline(pipeline_id)
                if success:
                    self.status_logger.report_command(cmd_id, "completed", f"Inference started for {pipeline_id}")
                    self.status_logger.report_model(
                        pipeline_id, running=True, pid=1, serving=False, serving_pid=0, serving_port=0
                    )
                else:
                    self.status_logger.report_command(cmd_id, "failed", f"Failed to start {pipeline_id}")

            elif cmd_type == "STOP_MODEL_INFERENCE":
                if not pipeline_id:
                    msg = "STOP command rejected: Could not identify target pipeline."
                    logger.warning(msg)
                    self.status_logger.report_command(cmd_id, "failed", msg)
                    return

                success = self._stop_pipeline(pipeline_id)
                if success:
                    self.status_logger.report_command(cmd_id, "completed", f"Inference stopped for {pipeline_id}")
                    self.status_logger.report_model(
                        pipeline_id, running=False, pid=0, serving=False, serving_pid=0, serving_port=0
                    )
                else:
                    self.status_logger.report_command(cmd_id, "failed", f"Failed to stop {pipeline_id}")

            elif cmd_type == "START_SERVING":
                if not pipeline_id:
                    msg = "START_SERVING command rejected: Could not identify target pipeline."
                    logger.warning(msg)
                    self.status_logger.report_command(cmd_id, "failed", msg)
                    return

                with self.lock:
                    config = self.pipeline_configs.get(pipeline_id)
                    if config:
                        port = payload.get("port", 8100)
                        host = payload.get("host", "127.0.0.1")
                        alias = payload.get("model_display_name", "locai-model")

                        config.source.args.update({"mode": "serve", "port": port, "host": host, "alias": alias})

                        if self.state_manager:
                            self.state_manager.update_pipeline_config(config)
                    else:
                        msg = f"Cannot serve '{pipeline_id}': Pipeline not found/deployed."
                        logger.error(msg)
                        self.status_logger.report_command(cmd_id, "failed", msg)
                        return

                success = self._start_pipeline(pipeline_id)
                if success:
                    self.status_logger.report_command(
                        cmd_id, "completed", f"Serving started for {pipeline_id} on {host}:{port}"
                    )
                    self.status_logger.report_model(
                        pipeline_id, running=False, pid=0, serving=True, serving_pid=1, serving_port=port
                    )
                else:
                    self.status_logger.report_command(cmd_id, "failed", f"Failed to start serving {pipeline_id}")

            elif cmd_type == "STOP_SERVING":
                if not pipeline_id:
                    msg = "STOP command rejected: Could not identify target pipeline."
                    logger.warning(msg)
                    self.status_logger.report_command(cmd_id, "failed", msg)
                    return

                success = self._stop_pipeline(pipeline_id)
                if success:
                    self.status_logger.report_command(cmd_id, "completed", f"Serving stopped for {pipeline_id}")
                    self.status_logger.report_model(
                        pipeline_id, running=False, pid=0, serving=False, serving_pid=0, serving_port=0
                    )
                else:
                    self.status_logger.report_command(cmd_id, "failed", f"Failed to stop {pipeline_id}")

            elif cmd_type == "STATUS":
                self._log_status()

            elif cmd_type == "UPDATE_AGENT":
                logger.info("OTA update command received. Preparing to update...", extra={"category": "deployment"})
                self.status_logger.report_command(cmd_id, "completed", "Update accepted — restarting.")
                # Signal main.py to pull updates and re-exec after shutdown completes
                self.update_requested = True
                self.running = False
                self.shutdown_event.set()

            elif cmd_type == "UPDATE_AGENT_CONFIG":
                from link.app.reconfigure import apply_agent_config

                new_cfg_raw = payload.get("agent_config")
                if not isinstance(new_cfg_raw, dict):
                    self.status_logger.report_command(cmd_id, "failed", "Missing or malformed agent_config in payload")
                    return
                result = apply_agent_config(self, new_cfg_raw)
                status = "completed" if (result.ok or result.scheduled_restart) else "failed"
                self.status_logger.report_command(cmd_id, status, result.message)

            else:
                logger.warning(f"Unknown command: {cmd_type}")

        except Exception as e:
            logger.error(f"Command handling failed: {e}", exc_info=True)
            self.status_logger.report_command(cmd_id, "failed", str(e))

    def run(self):
        """Main Lifecycle Loop.

        Starts the agent runtime and keeps it running until a shutdown event occurs.
        """
        logger.info("Agent Runtime active...")
        self.status_logger.report_lifecycle("online")

        # 1. Try Recovery First
        recovered_any = False
        if self.state_manager:
            if saved_state := self.state_manager.load_state():
                # Recover configs from the 'pipelines' list in the unified dictionary
                raw_pipelines = saved_state.get("pipelines", [])

                # A. Update Configuration Memory
                for p_data in raw_pipelines:
                    try:
                        # Convert dict to Pydantic
                        p_conf = PipelineConfig(**p_data)
                        self.pipeline_configs[p_conf.id] = p_conf
                    except Exception as e:
                        logger.warning(f"Failed to recover pipeline config: {e}")

                # B. Restart Active Pipelines
                with self.lock:
                    for p_data in raw_pipelines:
                        pid = p_data.get("id")
                        is_active = p_data.get("active", False)

                        if is_active and pid in self.pipeline_configs:
                            self._start_pipeline(pid)
                            recovered_any = True

        # 2. Fresh Start Fallback
        if not recovered_any:
            logger.info("No active pipelines found. Idling...")

        try:
            while self.running:
                if self.shutdown_event.wait(timeout=1.0):
                    break
        finally:
            self._shutdown()
            self.status_logger.report_lifecycle("offline")

    def _start_pipeline(self, pipeline_id: str, config_data: dict | None = None) -> bool:
        """Starts (or restarts) a pipeline.

        Args:
            pipeline_id (str): The ID of the pipeline to start.
            config_data (dict | None): Optional configuration data to update the pipeline.

        Returns:
            bool: True if started successfully, False otherwise.
        """
        with self.lock:
            if config_data:
                try:
                    if "id" not in config_data:
                        config_data["id"] = pipeline_id
                    new_conf = PipelineConfig(**config_data)
                    current_conf = self.pipeline_configs.get(pipeline_id)
                    is_running = pipeline_id in self.pipelines

                    if is_running and current_conf and new_conf == current_conf:
                        logger.info(f"Pipeline '{pipeline_id}' config is identical. Skipping restart.")
                        return True

                    logger.info(f"Configuration update for '{pipeline_id}'.")
                    self.pipeline_configs[pipeline_id] = new_conf
                except Exception as e:
                    logger.error(f"Invalid configuration for '{pipeline_id}': {e}")
                    return False

            p_conf = self.pipeline_configs.get(pipeline_id)
            if not p_conf:
                logger.error(f"Cannot start '{pipeline_id}': No configuration found.")
                return False

            if pipeline_id in self.pipelines:
                logger.info(f"Restarting pipeline '{pipeline_id}'...")
                self._stop_pipeline(pipeline_id)

            try:
                source = self._create_component(p_conf.source)
                sink = self._create_component(p_conf.sink) if p_conf.sink else (lambda data: None)
                new_pipe = Pipeline(p_conf.id, source, sink)
                self.pipelines[pipeline_id] = new_pipe
                new_pipe.start()

                if self.state_manager:
                    # Persist Config AND Active Status
                    self.state_manager.update_pipeline_config(p_conf)
                    self.state_manager.set_pipeline_status(pipeline_id, True)

                return True
            except Exception as e:
                logger.error(f"Failed to start pipeline '{pipeline_id}': {e}")
                return False

    def _stop_pipeline(self, pipeline_id: str) -> bool:
        """Stops and removes a pipeline.

        Args:
            pipeline_id (str): The ID of the pipeline to stop.

        Returns:
            bool: True if stopped successfully, False otherwise.
        """
        with self.lock:
            pipe = self.pipelines.get(pipeline_id)
            if pipe:
                try:
                    pipe.stop()
                    pipe.join(timeout=2.0)
                except Exception as e:
                    logger.error(f"Error stopping pipeline: {e}")

                if pipeline_id in self.pipelines:
                    del self.pipelines[pipeline_id]

                if self.state_manager:
                    self.state_manager.set_pipeline_status(pipeline_id, False)
                return True
            else:
                logger.warning(f"Cannot stop '{pipeline_id}': Not running.")
                return True

    def _deploy_model(self, command_id: str, payload: dict):
        """Downloads a model, maps its config, and reports status back to the platform.

        Args:
            command_id (str): The unique ID of the command.
            payload (dict): The command payload containing model details.
        """
        logger.info(f"Initiating deployment for command '{command_id}'...")
        models_dir = Path.cwd().joinpath("models")
        models_dir.mkdir(parents=True, exist_ok=True)

        pipeline_id = payload.get("model_id")
        model_name = payload.get("model_name")
        runtime_config = payload.get("runtime_config", {})

        if not pipeline_id or not model_name:
            msg = "Deploy failed: Missing model_id or model_name."
            logger.error(msg)
            self.status_logger.report_command(command_id, "failed", msg)
            return

        download_url = (
            f"{self.agent_config.identity.api_url}/models/{pipeline_id}/download/"
            + self.agent_config.identity.device_id
            + "/agent"
        )
        target_path = models_dir / model_name

        if not target_path.exists():
            logger.info(f"Downloading {model_name} from {download_url}...")
            try:
                headers = {"Authorization": f"Bearer {self.agent_config.identity.api_key}"}
                with requests.get(download_url, headers=headers, stream=True, timeout=600) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0))
                    done = 0
                    last_reported = -1
                    self.status_logger.report_deployment_progress(pipeline_id, "downloading", 0.0, 0, total)
                    with open(target_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                pct = int((done / total) * 100)
                                # Throttle to 5%-step deltas — keeps frontend SSE
                                # smooth without flooding the Zenoh fanout.
                                if pct >= last_reported + 5:
                                    self.status_logger.report_deployment_progress(
                                        pipeline_id, "downloading", float(pct), done, total
                                    )
                                    last_reported = pct
                logger.info(f"Model saved to {target_path}")
            except Exception as e:
                msg = f"Failed to download model: {e}"
                logger.error(msg)
                self.status_logger.report_command(command_id, "failed", msg)
                return
        else:
            logger.info(f"Model {model_name} already exists. Using cached file.")

        self.status_logger.report_deployment_progress(pipeline_id, "configuring", 95.0, 0, 0)

        try:
            new_pipeline_config = self._map_runtime_to_pipeline_config(pipeline_id, target_path, runtime_config)
        except Exception as e:
            msg = f"Failed to map runtime config: {e}"
            logger.error(msg)
            self.status_logger.report_command(command_id, "failed", msg)
            return

        with self.lock:
            self.pipeline_configs[pipeline_id] = new_pipeline_config
            if self.state_manager:
                try:
                    self.state_manager.update_pipeline_config(new_pipeline_config)
                except Exception as e:
                    logger.warning(f"Failed to persist state: {e}")

        logger.info(f"Pipeline '{pipeline_id}' deployed successfully.")
        self.status_logger.report_deployment_progress(pipeline_id, "completed", 100.0, 0, 0)
        self.status_logger.report_command(command_id, "completed", f"Model {model_name} deployed successfully.")

    def _log_status(self):
        """Logs the current status of the agent, including running and configured pipelines."""
        status = {
            "running_pipelines": list(self.pipelines.keys()),
            "configured_pipelines": list(self.pipeline_configs.keys()),
        }
        logger.info(f"Agent Status: {status}")

    def _create_component(self, comp: GenericConfig) -> Component:
        """Instantiates a component using the registry.

        Args:
            comp (GenericConfig): The component configuration.

        Returns:
            Component: The instantiated component.
        """
        name = comp.type
        args = dict(comp.args)

        if name == "command":
            args["callback"] = self.handle_command

        elif name.startswith("zenoh_"):
            if not self.zenoh_session:
                raise RuntimeError(f"Component '{name}' requires Zenoh, but no active session was provided.")

            args["session"] = self.zenoh_session

        component_cls = ComponentRegistry.get(name)
        if component_cls:
            return component_cls(**args)

        return ComponentRegistry.load_plugin(name, args)

    def _signal_handler(self, signum: int, frame: Any):
        """Handle SIGINT/SIGTERM for graceful shutdown.

        Args:
            signum (int): The signal number.
            frame (Any): The current stack frame.
        """
        logger.info(f"Signal {signum} received. Stopping Agent...")
        self.running = False
        self.shutdown_event.set()

    def _shutdown(self):
        """Graceful System Shutdown.

        Stops all pipelines and cleans up resources.
        """
        logger.info("Stopping pipelines...")
        with self.lock:
            for pipe in self.pipelines.values():
                pipe.stop()

            for pipe in self.pipelines.values():
                if pipe.is_alive():
                    pipe.join(timeout=1.0)
            self.pipelines.clear()

    # --- will be removed ---
    def _normalise_command(self, data: dict) -> tuple[str, str | None, str | None, dict]:
        """Normalises the command data structure.

        Args:
            data (dict): The raw command data.

        Returns:
            tuple[str, str | None, str | None, dict]: A tuple containing (cmd_type, cmd_id, pipeline_id, payload).
        """
        inner = data.get("data")
        if isinstance(inner, dict) and "command_type" in inner:
            cmd_type = inner.get("command_type", "").upper()
            cmd_id = data.get("id")
            payload = inner.get("payload", {})
            pipeline_id = payload.get("model_id") or payload.get("model_name")
            return cmd_type, cmd_id, pipeline_id, payload

        cmd_type = data.get("command", "").upper()
        cmd_id = data.get("id")
        payload = data.get("payload", {})
        pipeline_id = payload.get("id") or payload.get("model_id") or payload.get("model_name")
        if not pipeline_id and isinstance(cmd_id, str):
            pipeline_id = cmd_id
        return cmd_type, cmd_id, pipeline_id, payload

    def _map_runtime_to_pipeline_config(
        self, pipeline_id: str, model_path: Path, runtime_config: dict
    ) -> PipelineConfig:
        """Maps runtime configuration to pipeline configuration.

        Args:
            pipeline_id (str): The pipeline ID.
            model_path (Path): Path to the model file.
            runtime_config (dict): The runtime configuration dictionary.

        Returns:
            PipelineConfig: The generated pipeline configuration.
        """
        process_conf = runtime_config.get("process", {})
        impl_conf = process_conf.get("impl", {})
        runner = impl_conf.get("runner", "")

        source_args = process_conf.get("parameters", {}).copy()
        source_args["model_path"] = str(model_path)

        source_type = "unknown"
        semantic_type = "unknown"
        outputs = runtime_config.get("outputs", [])

        if outputs:
            semantic_type = outputs[0].get("semantic_type", "")

        if runner == "gguf_language_model" or semantic_type == "text_generation":
            source_type = "language_model"
            inputs = runtime_config.get("inputs", [])
            source_args["new_terminal"] = True
        elif "image_detection" in runner or semantic_type == "object_detection":
            source_type = "image_classifier"
            inputs = runtime_config.get("inputs", [])
            for inp in inputs:
                if inp.get("type") == "camera":
                    source_args["camera_index"] = inp.get("index", 0)
                    if inp.get("resolution"):
                        source_args["width"] = inp["resolution"][0]
                        source_args["height"] = inp["resolution"][1]
            if "show_window" not in source_args:
                source_args["show_window"] = False
        elif "audio_classification" in runner or semantic_type == "audio_classification":
            source_type = "audio_classifier"
            inputs = runtime_config.get("inputs", [])
            for inp in inputs:
                if inp.get("type") == "microphone":
                    if inp.get("sample_rate"):
                        source_args["sample_rate"] = inp["sample_rate"]
                    if inp.get("channels"):
                        source_args["channels"] = inp["channels"]
        else:
            logger.warning(f"Unknown runner '{runner}'. Defaulting to 'generic_model'.")
            source_type = "generic_model"

        sink_conf = GenericConfig(type="console", args={})

        if outputs:
            out_def = outputs[0]
            route = out_def.get("route", "console")

            if route == "agent":
                identity = self.agent_config.identity
                url = f"{identity.api_url}/agent/model_results/{identity.device_id}/create_from_agent"
                sink_conf = GenericConfig(
                    type="http_post",
                    args={"url": url, "api_key": identity.api_key, "timeout": 10},
                )

        return PipelineConfig(
            id=pipeline_id,
            source=GenericConfig(type=source_type, args=source_args),
            sink=sink_conf,
        )
