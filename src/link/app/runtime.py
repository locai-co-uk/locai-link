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

from link.app.state import StateManager
from link.components.pipeline import Pipeline
from link.components.registry import Component, ComponentRegistry
from link.config.commands import (
    CancelDeployCommand,
    DeployModelCommand,
    StartModelCommand,
    StartModelInferenceCommand,
    StartServingCommand,
    StatusCommand,
    StopModelInferenceCommand,
    StopServingCommand,
    UninstallModelCommand,
    UpdateAgentCommand,
    UpdatePipelineCommand,
    parse_command,
)
from link.config.models import AgentConfig, GenericConfig, PipelineConfig
from link.config.templating import resolve_templates
from link.infra.health_server import HealthServer, HealthState
from link.utils.logger import LinkReporter
from link.utils.version import resolve_agent_version

if TYPE_CHECKING:
    import zenoh

# Standard logger for debug/info text
logger = logging.getLogger(__name__)


class _AgentWorker:
    """Handle for a long-running background command worker."""

    __slots__ = ("cancel_event", "thread", "command_id")

    def __init__(self, cancel_event: threading.Event, thread: threading.Thread, command_id: str):
        self.cancel_event = cancel_event
        self.thread = thread
        self.command_id = command_id


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
        self._agent_workers: dict[str, _AgentWorker] = {}

        self.lock = threading.RLock()
        self.running = True
        self.shutdown_event = threading.Event()
        self.update_requested = False
        self.config_restart_requested = False

        # Health server for local clients that need a fresh view of agent state.
        self.health_state = HealthState(
            version=resolve_agent_version(),
            models_provider=self._snapshot_models,
            command_handler=self.handle_command,
        )
        self.health_server = HealthServer(self.health_state)

        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._signal_handler)
                signal.signal(signal.SIGTERM, self._signal_handler)
            except ValueError:
                logger.debug("Signal handlers skipped (not main thread).")

    def handle_command(self, data: dict[str, Any]):
        """Validate an incoming command against the shared contract and dispatch it.

        Commands arrive clean and flat, so there is nothing to parse: resolve our
        identity placeholders, validate against the `Command` schema, then act on
        the typed object. A command that fails validation is reported `failed`
        (when it carries an id) so it stops being retried.
        """
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
                self.status_logger.report_command(cmd_id, "failed", "Invalid command payload")
            return

        logger.info(f"Processing command: {cmd.type}")

        try:
            if isinstance(cmd, DeployModelCommand):
                self._deploy_model(cmd)

            elif isinstance(cmd, CancelDeployCommand):
                self._cancel_deploy(cmd)

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
                        {
                            "mode": "serve",
                            "port": cmd.port,
                            "host": cmd.host,
                            "alias": cmd.model_display_name,
                        }
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
                    self.health_state.set_serving(cmd.pipeline_id)
                else:
                    self.status_logger.report_command(cmd.id, "failed", f"Failed to start serving {cmd.pipeline_id}")

            elif isinstance(cmd, StopServingCommand):
                success = self._stop_pipeline(cmd.pipeline_id)
                if success:
                    self.status_logger.report_command(cmd.id, "completed", f"Serving stopped for {cmd.pipeline_id}")
                    self.status_logger.report_model(
                        cmd.pipeline_id, running=False, pid=0, serving=False, serving_pid=0, serving_port=0
                    )

                    if self.health_state.model_id == cmd.pipeline_id:
                        self.health_state.set_serving(None)
                else:
                    self.status_logger.report_command(cmd.id, "failed", f"Failed to stop {cmd.pipeline_id}")

            elif isinstance(cmd, UninstallModelCommand):
                safe_payload: dict[str, str] = {}
                if cmd.filename_on_server:
                    if not _is_safe_basename(cmd.filename_on_server):
                        msg = f"Refusing uninstall: filename_on_server is unsafe ({cmd.filename_on_server!r})"
                        logger.error(msg)
                        self.status_logger.report_command(cmd.id, "failed", "Invalid filename_on_server")
                        return
                    safe_payload["filename_on_server"] = cmd.filename_on_server
                if cmd.file_extension:
                    if not _is_safe_extension(cmd.file_extension):
                        msg = f"Refusing uninstall: file_extension is unsafe ({cmd.file_extension!r})"
                        logger.error(msg)
                        self.status_logger.report_command(cmd.id, "failed", "Invalid file_extension")
                        return
                    safe_payload["file_extension"] = cmd.file_extension
                self._uninstall_model(
                    cmd.id,
                    cmd.pipeline_id,
                    force_stop=cmd.force_stop,
                    payload=safe_payload,
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

            else:  # UpdateAgentConfigCommand — last remaining variant in the Command union
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
        # Lazy-start the health server here (not in __init__) so tests
        # that construct an AgentRuntime in-process don't race for port 8101.
        self.health_server.start()

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
                            started = self._start_pipeline(pid)
                            recovered_any = True
                            # Re-announce serving state to Control.
                            if started:
                                p_conf = self.pipeline_configs[pid]
                                src_args = p_conf.source.args if p_conf.source else {}
                                try:
                                    if src_args.get("mode") == "serve":
                                        self.status_logger.report_model(
                                            pid,
                                            running=False,
                                            pid=0,
                                            serving=True,
                                            serving_pid=1,
                                            serving_port=src_args.get("port", 0),
                                        )
                                        self.health_state.set_serving(pid)
                                    elif src_args.get("model_path"):
                                        # Inference-mode model pipeline
                                        self.status_logger.report_model(
                                            pid,
                                            running=True,
                                            pid=1,
                                            serving=False,
                                            serving_pid=0,
                                            serving_port=0,
                                        )
                                except Exception as e:
                                    logger.warning(f"Failed to re-announce model state for '{pid}': {e}")

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

    def _start_pipeline(self, pipeline_id: str, config_data: dict[str, Any] | None = None) -> bool:
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
        """Validate a DEPLOY_MODEL and dispatch the download to a worker thread."""
        command_id = cmd.id
        pipeline_id = cmd.pipeline_id
        model_name = cmd.model_name

        if cmd.config.id != pipeline_id:
            msg = f"Refusing deploy: pipeline_id {pipeline_id!r} != config.id {cmd.config.id!r}"
            logger.error(msg)
            self.status_logger.report_command(command_id, "failed", "Pipeline id mismatch")
            return
        if model_name and not _is_safe_basename(model_name):
            msg = f"Refusing deploy: model_name is unsafe ({model_name!r})"
            logger.error(msg)
            self.status_logger.report_command(command_id, "failed", "Invalid model_name")
            return

        with self.lock:
            existing = self._agent_workers.get(pipeline_id)
            if existing is not None and existing.thread.is_alive():
                msg = (
                    f"Deploy already in progress for pipeline '{pipeline_id}' "
                    f"(command '{existing.command_id}'). Send CANCEL_DEPLOY first."
                )
                logger.warning(msg)
                self.status_logger.report_command(command_id, "failed", msg)
                return
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._deploy_worker,
                name=f"deploy-{pipeline_id}",
                args=(cmd, cancel_event),
                daemon=True,
            )
            self._agent_workers[pipeline_id] = _AgentWorker(cancel_event, thread, command_id)

        logger.info(f"Initiating deployment for command '{command_id}'...")
        thread.start()

    def _deploy_worker(self, cmd: DeployModelCommand, cancel_event: threading.Event):
        """Run the actual download + config registration for a DEPLOY_MODEL."""
        command_id = cmd.id
        pipeline_id = cmd.pipeline_id
        model_name = cmd.model_name

        try:
            models_dir = Path.cwd().joinpath("models")
            models_dir.mkdir(parents=True, exist_ok=True)

            download_url = (
                f"{self.agent_config.identity.api_url}/models/{pipeline_id}/download/"
                + self.agent_config.identity.device_id
                + "/agent"
            )
            target_path = models_dir / model_name

            partial_path = target_path.with_name(target_path.name + ".partial")
            cancelled = False

            if not target_path.exists():
                if partial_path.exists():
                    try:
                        partial_path.unlink()
                    except OSError as e:
                        logger.warning(f"Failed to remove stale partial {partial_path}: {e}")

                logger.info(f"Downloading {model_name} from {download_url}...")
                try:
                    headers = {"Authorization": f"Bearer {self.agent_config.identity.api_key}"}
                    with requests.get(download_url, headers=headers, stream=True, timeout=600) as r:
                        r.raise_for_status()
                        total = int(r.headers.get("content-length", 0))
                        done = 0
                        last_reported = -1
                        self.status_logger.report_deployment_progress(pipeline_id, "downloading", 0.0, 0, total)
                        with open(partial_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if cancel_event.is_set():
                                    cancelled = True
                                    break
                                f.write(chunk)
                                done += len(chunk)
                                if total:
                                    pct = int((done / total) * 100)
                                    if pct >= last_reported + 5:
                                        self.status_logger.report_deployment_progress(
                                            pipeline_id, "downloading", float(pct), done, total
                                        )
                                        last_reported = pct
                except Exception as e:
                    self._cleanup_partial(partial_path)
                    msg = f"Failed to download model: {e}"
                    logger.error(msg)
                    self.status_logger.report_command(command_id, "failed", msg)
                    return

                if cancelled:
                    self._cleanup_partial(partial_path)
                    logger.info(f"Deploy '{pipeline_id}' cancelled during download.")
                    self.status_logger.report_deployment_progress(pipeline_id, "cancelled", 0.0, 0, 0)
                    self.status_logger.report_command(command_id, "failed", "cancelled")
                    return

                partial_path.replace(target_path)
                logger.info(f"Model saved to {target_path}")
            else:
                logger.info(f"Model {model_name} already exists. Using cached file.")

            # Re-check cancel between download completion and config commit
            # so a cancel arriving at the tail of the download still counts.
            if cancel_event.is_set():
                logger.info(f"Deploy '{pipeline_id}' cancelled before configuring.")
                self.status_logger.report_deployment_progress(pipeline_id, "cancelled", 0.0, 0, 0)
                self.status_logger.report_command(command_id, "failed", "cancelled")
                return

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
        finally:
            with self.lock:
                worker = self._agent_workers.get(pipeline_id)
                if worker is not None and worker.command_id == command_id:
                    del self._agent_workers[pipeline_id]

    def _cancel_deploy(self, cmd: CancelDeployCommand):
        """Signal an in-flight DEPLOY_MODEL for `cmd.pipeline_id` to abort."""
        with self.lock:
            worker = self._agent_workers.get(cmd.pipeline_id)
            if worker is None or not worker.thread.is_alive():
                self.status_logger.report_command(
                    cmd.id, "completed", f"No active deploy for pipeline '{cmd.pipeline_id}'"
                )
                return
            worker.cancel_event.set()
            original = worker.command_id

        logger.info(f"Cancelling deploy for '{cmd.pipeline_id}' (deploy command '{original}')")
        self.status_logger.report_command(cmd.id, "completed", f"Cancel signal sent to deploy '{original}'")

    def _cleanup_partial(self, partial_path: Path):
        """Best-effort delete of a `.partial` file; log and continue on failure."""
        if partial_path.exists():
            try:
                partial_path.unlink()
            except OSError as e:
                logger.warning(f"Failed to delete partial {partial_path}: {e}")

    def _update_pipeline(self, cmd: UpdatePipelineCommand):
        """Updates a deployed pipeline's configuration, preserving its running state.

        If the pipeline is running, it restarts with the new configuration.
        Otherwise, the configuration is saved for the next start.
        """

        pipeline_id = cmd.pipeline_id

        if cmd.config.id != pipeline_id:
            msg = f"Refusing update: pipeline_id {pipeline_id!r} != config.id {cmd.config.id!r}"
            logger.error(msg)
            self.status_logger.report_command(cmd.id, "failed", "Pipeline id mismatch")
            return

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

    def _snapshot_models(self) -> list[dict[str, Any]]:
        """Freshly enumerate servable-model pipelines for `GET /models`.

        Returns:
            list[dict[str, Any]]: One entry per servable pipeline with
                keys ``id``, ``alias``, ``port``, ``host``, ``is_serving``.
        """
        out: list[dict[str, Any]] = []
        with self.lock:
            for pid, cfg in self.pipeline_configs.items():
                args = cfg.source.args or {}
                if "model_path" not in args:
                    continue
                active = pid in self.pipelines
                is_serving = active and args.get("mode") == "serve"
                out.append(
                    {
                        "id": pid,
                        "alias": args.get("alias") or pid,
                        "port": args.get("port"),
                        "host": args.get("host", "127.0.0.1"),
                        "is_serving": is_serving,
                    }
                )
        return out

    def _uninstall_model(
        self,
        command_id: str,
        pipeline_id: str,
        force_stop: bool = False,
        payload: dict[str, Any] | None = None,
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
        # Cancel any in-flight deploy workers before touching pipelines. They
        # are daemon threads and would die at process exit anyway, but joining
        # here lets them close the streaming socket and remove their .partial
        # so an OTA re-exec doesn't leave orphaned bytes on disk.
        with self.lock:
            workers = list(self._agent_workers.values())
        for w in workers:
            w.cancel_event.set()
        for w in workers:
            w.thread.join(timeout=5.0)

        logger.info("Stopping pipelines...")
        with self.lock:
            for pipe in self.pipelines.values():
                pipe.stop()

            for pipe in self.pipelines.values():
                if pipe.is_alive():
                    pipe.join(timeout=1.0)
            self.pipelines.clear()

        # Tear down the health server last — its daemon thread would
        # be killed at process exit anyway, but joining cleanly avoids
        # the "address already in use" race on a fast restart.
        self.health_server.stop()


# ---------------------------------------------------------------------------
# Wire input validators — first line of defense against malicious/malformed
# filenames flowing into filesystem operations. Wire-level pydantic catches
# missing fields and type errors, but it doesn't see "../etc/passwd" as
# semantically unsafe — that's our job here.
# ---------------------------------------------------------------------------


def _is_safe_basename(name: str) -> bool:
    """True if `name` is a single filename component with no path separators."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    # Path(name).name strips any directory parts — if it differs from the
    # input, the input had directory structure we don't want.
    return Path(name).name == name


def _is_safe_extension(ext: str) -> bool:
    """True if `ext` is a plain extension token (no separators)."""
    if not ext:
        return False
    if "/" in ext or "\\" in ext or "\x00" in ext or ext in (".", ".."):
        return False
    return True
