# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Agent runtime — owns the pipeline lifecycle, command dispatch, and shutdown flow."""

import logging
import signal
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import requests
from pydantic import ValidationError

import link.components  # noqa: F401
from link.app.state import StateManager
from link.components.pipeline import Pipeline
from link.components.registry import Component, ComponentRegistry
from link.config.commands import (
    DeployModelCommand,
    StartModelCommand,
    StartModelInferenceCommand,
    StartServingCommand,
    StatusCommand,
    StopModelInferenceCommand,
    StopServingCommand,
    UninstallModelCommand,
    UpdateAgentCommand,
    UpdateAgentConfigCommand,
    UpdatePipelineCommand,
    parse_command,
)
from link.config.models import AgentConfig, GenericConfig, PipelineConfig
from link.config.templating import resolve_templates
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
        """Validate an incoming command against the shared contract and dispatch it.

        Commands arrive clean and flat, so there is nothing to parse: resolve our
        identity placeholders, validate against the `Command` schema, then act on
        the typed object. A command that fails validation is reported `failed`
        (when it carries an id) so it stops being retried.
        """
        if not isinstance(data, dict):
            logger.warning(f"Invalid command format: expected dict, got {type(data)}")
            return

        # Resolve ${identity.*} placeholders (e.g. a sink topic) before validating.
        ident = self.agent_config.identity
        context = {
            "identity": {
                "device_id": ident.device_id,
                "device_name": ident.device_name,
                "api_key": ident.api_key,
                "api_url": ident.api_url,
            },
            "api_url": ident.api_url,
        }
        resolved = resolve_templates(data, context)

        try:
            cmd = parse_command(resolved)
        except ValidationError as e:
            cmd_id = data.get("id")
            logger.warning(f"Command rejected (schema validation failed): {e}")
            if isinstance(cmd_id, str) and cmd_id:
                self.status_logger.report_command(cmd_id, "failed", f"Invalid command: {e}")
            return

        logger.info(f"Processing command: {cmd.type}")

        try:
            if isinstance(cmd, DeployModelCommand):
                self._deploy_model(cmd)

            elif isinstance(cmd, StartModelCommand):
                success = self._start_pipeline(cmd.pipeline_id, cmd.config.model_dump())
                if success:
                    self.status_logger.report_command(cmd.id, "completed", f"Pipeline {cmd.pipeline_id} started")
                else:
                    self.status_logger.report_command(cmd.id, "failed", f"Failed to start {cmd.pipeline_id}")

            elif isinstance(cmd, StartModelInferenceCommand):
                success = self._start_pipeline(cmd.pipeline_id)
                if success:
                    self.status_logger.report_command(cmd.id, "completed", f"Inference started for {cmd.pipeline_id}")
                    self.status_logger.report_model(
                        cmd.pipeline_id, running=True, pid=1, serving=False, serving_pid=0, serving_port=0
                    )
                else:
                    self.status_logger.report_command(cmd.id, "failed", f"Failed to start {cmd.pipeline_id}")

            elif isinstance(cmd, StopModelInferenceCommand):
                success = self._stop_pipeline(cmd.pipeline_id)
                if success:
                    self.status_logger.report_command(cmd.id, "completed", f"Inference stopped for {cmd.pipeline_id}")
                    self.status_logger.report_model(
                        cmd.pipeline_id, running=False, pid=0, serving=False, serving_pid=0, serving_port=0
                    )
                else:
                    self.status_logger.report_command(cmd.id, "failed", f"Failed to stop {cmd.pipeline_id}")

            elif isinstance(cmd, StartServingCommand):
                with self.lock:
                    config = self.pipeline_configs.get(cmd.pipeline_id)
                    if not config:
                        msg = f"Cannot serve '{cmd.pipeline_id}': Pipeline not found/deployed."
                        logger.error(msg)
                        self.status_logger.report_command(cmd.id, "failed", msg)
                        return
                    config.source.args.update(
                        {"mode": "serve", "port": cmd.port, "host": cmd.host, "alias": cmd.model_display_name}
                    )
                    if self.state_manager:
                        self.state_manager.update_pipeline_config(config)

                success = self._start_pipeline(cmd.pipeline_id)
                if success:
                    self.status_logger.report_command(
                        cmd.id, "completed", f"Serving started for {cmd.pipeline_id} on {cmd.host}:{cmd.port}"
                    )
                    self.status_logger.report_model(
                        cmd.pipeline_id, running=False, pid=0, serving=True, serving_pid=1, serving_port=cmd.port
                    )
                else:
                    self.status_logger.report_command(cmd.id, "failed", f"Failed to start serving {cmd.pipeline_id}")

            elif isinstance(cmd, StopServingCommand):
                success = self._stop_pipeline(cmd.pipeline_id)
                if success:
                    self.status_logger.report_command(cmd.id, "completed", f"Serving stopped for {cmd.pipeline_id}")
                    self.status_logger.report_model(
                        cmd.pipeline_id, running=False, pid=0, serving=False, serving_pid=0, serving_port=0
                    )
                else:
                    self.status_logger.report_command(cmd.id, "failed", f"Failed to stop {cmd.pipeline_id}")

            elif isinstance(cmd, UninstallModelCommand):
                self._uninstall_model(
                    cmd.id,
                    cmd.pipeline_id,
                    force_stop=cmd.force_stop,
                    payload={
                        "filename_on_server": cmd.filename_on_server,
                        "file_extension": cmd.file_extension,
                    },
                )

            elif isinstance(cmd, UpdatePipelineCommand):
                self._update_pipeline(cmd)

            elif isinstance(cmd, StatusCommand):
                self._log_status()

            elif isinstance(cmd, UpdateAgentCommand):
                logger.info("OTA update command received. Preparing to update...", extra={"category": "deployment"})
                self.status_logger.report_command(cmd.id, "completed", "Update accepted - restarting.")
                # Signal main.py to pull updates and re-exec after shutdown completes
                self.update_requested = True
                self.running = False
                self.shutdown_event.set()

            elif isinstance(cmd, UpdateAgentConfigCommand):
                from link.app.reconfigure import apply_agent_config

                result = apply_agent_config(self, cmd.agent_config.model_dump())
                status = "completed" if (result.ok or result.scheduled_restart) else "failed"
                self.status_logger.report_command(cmd.id, status, result.message)

        except Exception as e:
            logger.error(f"Command handling failed: {e}", exc_info=True)
            self.status_logger.report_command(cmd.id, "failed", str(e))

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

    def _deploy_model(self, cmd: DeployModelCommand):
        """Download the model artifact and register the provided pipeline.

        The pipeline definition arrives ready-made; the agent stores `cmd.config`
        verbatim, with no mapping.
        """
        command_id = cmd.id
        pipeline_id = cmd.pipeline_id
        model_name = cmd.model_name

        logger.info(f"Initiating deployment for command '{command_id}'...")
        models_dir = Path.cwd().joinpath("models")
        models_dir.mkdir(parents=True, exist_ok=True)

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

        with self.lock:
            self.pipeline_configs[pipeline_id] = cmd.config
            if self.state_manager:
                try:
                    self.state_manager.update_pipeline_config(cmd.config)
                except Exception as e:
                    logger.warning(f"Failed to persist state: {e}")

        logger.info(f"Pipeline '{pipeline_id}' deployed successfully.")
        self.status_logger.report_deployment_progress(pipeline_id, "completed", 100.0, 0, 0)
        self.status_logger.report_command(command_id, "completed", f"Model {model_name} deployed successfully.")

    def _update_pipeline(self, cmd: UpdatePipelineCommand):
        """Updates a deployed pipeline's configuration, preserving its running state.

        If the pipeline is running, it restarts with the new configuration.
        Otherwise, the configuration is saved for the next start.
        """

        pipeline_id = cmd.pipeline_id

        with self.lock:
            is_running = pipeline_id in self.pipelines

        if is_running:
            # _start_pipeline stops, re-creates, and persists in one step.
            success = self._start_pipeline(pipeline_id, cmd.config.model_dump())
        else:
            with self.lock:
                self.pipeline_configs[pipeline_id] = cmd.config
                if self.state_manager:
                    try:
                        self.state_manager.update_pipeline_config(cmd.config)
                    except Exception as e:
                        logger.warning(f"Failed to persist state: {e}")
            success = True

        if success:
            self.status_logger.report_command(cmd.id, "completed", f"Pipeline '{pipeline_id}' updated")
        else:
            self.status_logger.report_command(cmd.id, "failed", f"Failed to update pipeline '{pipeline_id}'")

    def _uninstall_model(
        self,
        command_id: str,
        pipeline_id: str,
        force_stop: bool = False,
        payload: dict | None = None,
    ):
        """Uninstalls a pipeline by removing its configuration and on-disk artifacts.

        Behavior:
        - If running, it refuses removal unless `force_stop` is True (which stops it first).
        - Refuses removal if another pipeline references the same artifact path.
        - Idempotent: missing configs or already-deleted files report as successful.
        - Uses the command payload as a fallback to locate and free files if local state has drifted.

        Args:
            command_id: The command ID.
            pipeline_id: The pipeline / model ID to remove.
            force_stop: Whether to stop the pipeline before removal if it is running.
            payload: Optional payload containing filename fallbacks for orphaned files.
        """
        logger.info(f"Uninstalling pipeline '{pipeline_id}' (force_stop={force_stop})...")

        # Stop FIRST, before acquiring self.lock — _stop_pipeline takes the lock itself,
        # so calling it inside `with self.lock` would deadlock.
        if pipeline_id in self.pipelines:
            if not force_stop:
                msg = (
                    f"Cannot uninstall '{pipeline_id}': pipeline is running. "
                    "Stop it first (STOP_MODEL_INFERENCE / STOP_SERVING) and retry."
                )
                logger.warning(msg)
                self.status_logger.report_command(command_id, "failed", msg)
                return
            logger.info(f"force_stop: stopping running pipeline '{pipeline_id}' before uninstall")
            if not self._stop_pipeline(pipeline_id):
                msg = f"Cannot uninstall '{pipeline_id}': failed to stop running pipeline."
                logger.error(msg)
                self.status_logger.report_command(command_id, "failed", msg)
                return

        with self.lock:
            cfg = self.pipeline_configs.get(pipeline_id)
            artifact_path: Path | None = None
            if cfg is not None:
                src_args = getattr(cfg.source, "args", {}) or {}
                model_path = src_args.get("model_path")
                if model_path:
                    artifact_path = Path(str(model_path))

            # Orphaned-file fallback: no local config (so no model_path), but the
            # command payload told us the filename — reconstruct the path under the
            # same models/ dir _deploy_model writes to, so we still free disk.
            if artifact_path is None and payload:
                filename = payload.get("filename_on_server")
                if filename:
                    extension = payload.get("file_extension") or ""
                    if extension and not str(extension).startswith("."):
                        extension = f".{extension}"
                    artifact_path = Path.cwd().joinpath("models", f"{filename}{extension}")
                    logger.info(f"No local config for '{pipeline_id}'; using payload fallback path: {artifact_path}")

            if artifact_path is not None:
                for other_id, other_cfg in self.pipeline_configs.items():
                    if other_id == pipeline_id:
                        continue
                    other_args = getattr(other_cfg.source, "args", {}) or {}
                    other_path = other_args.get("model_path")
                    if other_path and Path(str(other_path)) == artifact_path:
                        msg = (
                            f"Cannot remove '{pipeline_id}': artifact "
                            f"'{artifact_path.name}' is also used by pipeline "
                            f"'{other_id}'. Remove that one first and retry."
                        )
                        logger.warning(msg)
                        self.status_logger.report_command(command_id, "failed", msg)
                        return

            self.pipeline_configs.pop(pipeline_id, None)
            if self.state_manager:
                try:
                    self.state_manager.remove_pipeline(pipeline_id)
                except Exception as e:
                    logger.warning(f"Failed to remove pipeline from state: {e}")

        deleted = False
        if artifact_path is not None:
            try:
                if artifact_path.is_file():
                    artifact_path.unlink()
                    deleted = True
                    logger.info(f"Deleted artifact: {artifact_path}")
                else:
                    logger.info(f"No artifact at {artifact_path} (already removed).")
            except Exception as e:
                logger.warning(f"Failed to delete artifact at {artifact_path}: {e}")
        else:
            logger.info(f"No artifact path on '{pipeline_id}' config — skipping file deletion.")

        self.status_logger.report_model(
            pipeline_id,
            installed=False,
            running=False,
            serving=False,
            pid=0,
            serving_pid=0,
            serving_port=0,
        )
        suffix = f" (file deleted: {artifact_path.name})" if deleted and artifact_path else ""
        self.status_logger.report_command(command_id, "completed", f"Pipeline '{pipeline_id}' removed{suffix}")

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
