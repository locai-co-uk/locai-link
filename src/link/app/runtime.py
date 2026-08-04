# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Agent runtime — owns the pipeline lifecycle, command dispatch, and shutdown flow."""

import json
import logging
import signal
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

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
from link.utils.logger import LinkReporter, rebuild_handlers
from link.utils.version import resolve_agent_version

if TYPE_CHECKING:
    import zenoh

# Standard logger for debug/info text
logger = logging.getLogger(__name__)

# Uninstall reports that failed to reach Control, kept until delivered.
PENDING_UNINSTALL_REPORTS_PATH = StateManager.STATE_DIR / ".pending_uninstall_reports.json"
_PENDING_REPORT_RETRY_SECONDS = 60.0

class _AgentWorker:
    """Handle for a long-running background command worker."""

    __slots__ = ("cancel_event", "thread", "command_id", "response")

    def __init__(self, cancel_event: threading.Event, thread: threading.Thread, command_id: str) -> None:
        self.cancel_event = cancel_event
        self.thread = thread
        self.command_id = command_id
        # Streaming HTTP response stashed while a download is in flight, so
        # `_cancel_deploy` can close the underlying socket and let
        # `iter_content` unblock immediately instead of waiting for the read
        # timeout to fire.
        self.response: requests.Response | None = None


class AgentRuntime:
    """Manages the lifecycle of device inference pipelines."""

    def __init__(
        self,
        agent_config: AgentConfig,
        state_manager: StateManager | None = None,
        zenoh_session: "zenoh.Session | None" = None,
    ) -> None:
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
        # Serializes slow pipeline create/start/stop (plugin install, process
        # spawn, thread join) so that work does not run under self.lock, keeping
        # the read path (_snapshot_models / GET /models) responsive. Lock order:
        # acquire this BEFORE self.lock, never the reverse.
        self._pipeline_ops_lock = threading.RLock()
        self.running = True
        self.shutdown_event = threading.Event()
        # Guards read-modify-write of the pending uninstall-reports file.
        self._pending_reports_lock = threading.Lock()
        # Serializes concurrent DEPLOY_MODEL downloads that target the SAME file
        # (e.g. two catalog aliases for one GGUF, deployed together at onboarding).
        # Without it both stream into the same `<name>.partial` and race the
        # rename — the loser hits FileNotFoundError. Keyed on model_name; the
        # waiter then hits the `target_path.exists()` cache guard and skips.
        self._download_locks: dict[str, threading.Lock] = {}
        self._download_locks_guard = threading.Lock()
        self.update_requested = False
        self.config_restart_requested = False

        # Health server for local clients that need a fresh view of agent state.
        self.health_state = HealthState(
            version=resolve_agent_version(),
            models_provider=self._snapshot_models,
            command_handler=self.handle_command,
            # Decline a companion /update synchronously when there's no
            # installable asset — mirrors the UPDATE_AGENT handler's pre-flight
            # so the UI never hangs on "Updating" for a decline.
            update_preflight=self._can_update,
        )
        # Transport diagnostic: the session is opened before AgentRuntime exists,
        # so holding one here means connected=True. Mid-session disconnects aren't
        # observed today; shutdown() flips this back to False.
        if zenoh_session is not None:
            endpoints = agent_config.transport.args.get("endpoints", []) if agent_config.transport else []
            self.health_state.set_transport(
                transport_type=agent_config.transport.type if agent_config.transport else None,
                endpoint=endpoints[0] if endpoints else None,
                connected=True,
            )
        # Health server owns the update-available field; inject the checker.
        # Version check uses the device's api_url (Control); download uses GitHub.
        from link.app.updater import check_ui_version_drift, check_update_available

        control_base = getattr(agent_config.identity, "api_url", None) if agent_config.identity else None
        self.health_server = HealthServer(
            self.health_state,
            update_checker=lambda: check_update_available(control_base_url=control_base),
        )

        # If a prior OTA moved the runtime ahead but couldn't swap the macOS UI
        # apps (pre-fix root-owned bundles), prompt a one-time reinstall. No-op
        # off macOS / on source installs.
        check_ui_version_drift()

        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._signal_handler)
                signal.signal(signal.SIGTERM, self._signal_handler)
            except ValueError:
                logger.debug("Signal handlers skipped (not main thread).")

    def _can_update(self) -> bool:
        """Whether an OTA can actually proceed. A frozen install needs a
        published per-platform asset; source installs update via git (always
        True). Shared by the UPDATE_AGENT handler and the health server's
        /update pre-flight so a decline is decided one way."""
        from link.app.updater import bundle_asset_available, running_frozen_bundle

        return not (running_frozen_bundle() and not bundle_asset_available())

    def handle_command(self, data: dict[str, Any]) -> None:
        """Validate an incoming command against the shared contract and dispatch it.

        Commands arrive clean and flat, so there is nothing to parse: resolve our
        identity placeholders, validate against the `Command` schema, then act on
        the typed object. A command that fails validation is reported `failed`
        (when it carries an id) so it stops being retried.

        Args:
            data: Raw command dict as decoded from the wire.
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
                # Pre-flight: a frozen install with no published per-platform asset
                # can't update. Accepting anyway shuts down, fails in swap_bundle,
                # relaunches, and loops forever (cancelling in-flight work each
                # time). Decline and stay on the current version instead.
                if not self._can_update():
                    logger.warning("Update requested but no installable asset is published yet; staying put.")
                    self.status_logger.report_command(cmd.id, "failed", "No installable update asset available yet")
                else:
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

    def run(self) -> None:
        """Main Lifecycle Loop.

        Starts the agent runtime and keeps it running until a shutdown event occurs.
        """
        logger.info("Agent Runtime active...")
        # Lazy-start the health server here (not in __init__) so tests
        # that construct an AgentRuntime in-process don't race for port 20505.
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

                # B. Restart Active Pipelines. Collect ids under the lock, then
                # start each outside it — _start_pipeline takes the ops lock and
                # must not be called while holding self.lock (lock ordering).
                with self.lock:
                    to_start = [
                        p_data.get("id")
                        for p_data in raw_pipelines
                        if p_data.get("active", False) and p_data.get("id") in self.pipeline_configs
                    ]

                for pid in to_start:
                    started = self._start_pipeline(pid)
                    recovered_any = True
                    # Re-announce serving state to Control.
                    if started:
                        p_conf = self.pipeline_configs.get(pid)
                        src_args = p_conf.source.args if p_conf and p_conf.source else {}
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

        # Announce "online" only now that the command subscription is declared,
        # so Control never dispatches a command before we can receive it.
        self.status_logger.report_lifecycle("online")

        # Reports that failed while offline get another chance now and then
        # periodically below.
        self._flush_pending_uninstall_reports()

        try:
            last_flush = time.monotonic()
            while self.running:
                if self.shutdown_event.wait(timeout=1.0):
                    break
                if time.monotonic() - last_flush >= _PENDING_REPORT_RETRY_SECONDS:
                    last_flush = time.monotonic()
                    self._flush_pending_uninstall_reports()
        finally:
            self._shutdown()
            self.status_logger.report_lifecycle("offline")

    def apply_config(self, target_cfg: AgentConfig, *, previous_cfg: AgentConfig) -> None:
        """Hot-swap the running config to `target_cfg`: diff/stop/start pipelines
        relative to `previous_cfg`, then rebuild handlers and publish the new
        agent_config/pipeline_configs.

        Pipeline start/stop acquire `_pipeline_ops_lock` then `self.lock`, so the
        swap must run OUTSIDE `self.lock` — calling it while holding `self.lock`
        inverts the lock order and can deadlock. State persistence is the
        caller's job, so this one method drives both apply and revert.
        """
        # Hold _pipeline_ops_lock across the whole swap so a concurrent deploy or
        # pipeline update can't commit into the gap before the final replace
        # (RLock: the nested start/stop calls reacquire it fine).
        with self._pipeline_ops_lock:
            self._diff_and_swap_pipelines(previous_cfg, target_cfg)
            with self.lock:
                rebuild_handlers(target_cfg.logging, target_cfg.reporting, self.zenoh_session)
                self.agent_config = target_cfg
                self.pipeline_configs = {p.id: p for p in target_cfg.pipelines}

    def _diff_and_swap_pipelines(self, old: AgentConfig, new: AgentConfig) -> None:
        """Stop removed pipelines and (re)start active ones. Raises if a pipeline
        that should be active fails to start, so `apply_config`'s caller reverts
        instead of leaving a half-applied config reported as success.
        """
        old_ids = {p.id for p in old.pipelines}
        new_ids = {p.id for p in new.pipelines}

        # Stop pipelines that were removed.
        for pid in old_ids - new_ids:
            if not self._stop_pipeline(pid):
                raise RuntimeError(f"pipeline '{pid}' failed to stop")

        # Start active pipelines (no-ops on unchanged ones); stop now-inactive
        # ones. Inactive configs are published by apply_config's final assignment.
        for p in new.pipelines:
            if p.active:
                if not self._start_pipeline(p.id, p.model_dump()):
                    raise RuntimeError(f"pipeline '{p.id}' failed to start")
            elif p.id in self.pipelines:
                if not self._stop_pipeline(p.id):
                    raise RuntimeError(f"pipeline '{p.id}' failed to stop")

    def _start_pipeline(self, pipeline_id: str, config_data: dict[str, Any] | None = None) -> bool:
        """Starts (or restarts) a pipeline.

        Args:
            pipeline_id (str): The ID of the pipeline to start.
            config_data (dict | None): Optional configuration data to update the pipeline.

        Returns:
            bool: True if started successfully, False otherwise.
        """
        with self._pipeline_ops_lock:
            # Fast: validate/update config and decide restart, under self.lock.
            with self.lock:
                # Hold the new config aside; don't commit it to
                # pipeline_configs until the restart actually succeeds, so a
                # failed stop/build leaves the previous working config intact.
                pending_conf: PipelineConfig | None = None
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
                        pending_conf = new_conf
                    except Exception as e:
                        logger.error(f"Invalid configuration for '{pipeline_id}': {e}")
                        return False

                p_conf = pending_conf or self.pipeline_configs.get(pipeline_id)
                if not p_conf:
                    logger.error(f"Cannot start '{pipeline_id}': No configuration found.")
                    return False

                # Snapshot under the lock: the slow section below reads
                # source.args off-lock while a concurrent StartServingCommand
                # mutates the stored config's args dict in place.
                p_conf = p_conf.model_copy(deep=True)

                needs_restart = pipeline_id in self.pipelines

            # Slow: stop old pipeline + build components. Serialized by the ops
            # lock but NOT under self.lock, so reads (_snapshot_models) stay live.
            if needs_restart:
                logger.info(f"Restarting pipeline '{pipeline_id}'...")
                if not self._stop_pipeline(pipeline_id):
                    logger.error(f"Cannot restart '{pipeline_id}': previous instance still running.")
                    return False

            try:
                source = self._create_component(p_conf.source)
                sink = self._create_component(p_conf.sink) if p_conf.sink else (lambda data: None)
                new_pipe = Pipeline(p_conf.id, source, sink)
            except Exception as e:
                logger.error(f"Failed to start pipeline '{pipeline_id}': {e}")
                return False

            with self.lock:
                if pending_conf is not None:
                    self.pipeline_configs[pipeline_id] = pending_conf
                self.pipelines[pipeline_id] = new_pipe
            new_pipe.start()

            if self.state_manager:
                try:
                    self.state_manager.update_pipeline_config(p_conf)
                    self.state_manager.set_pipeline_status(pipeline_id, True)
                except Exception as e:
                    logger.warning(f"Failed to persist pipeline state for '{pipeline_id}': {e}")

            return True

    def _stop_pipeline(self, pipeline_id: str) -> bool:
        """Stops and removes a pipeline.

        Args:
            pipeline_id (str): The ID of the pipeline to stop.

        Returns:
            bool: True if stopped successfully, False otherwise.
        """
        with self._pipeline_ops_lock:
            with self.lock:
                pipe = self.pipelines.get(pipeline_id)
            if not pipe:
                logger.warning(f"Cannot stop '{pipeline_id}': Not running.")
                return True

            # Slow: signal + join outside self.lock so reads stay live.
            try:
                pipe.stop()
                pipe.join(timeout=2.0)
            except Exception as e:
                logger.error(f"Error stopping pipeline: {e}")

            if pipe.is_alive():
                # Cooperative stop didn't take within the timeout; keep it
                # tracked rather than orphan a thread still holding resources.
                logger.error(f"Pipeline '{pipeline_id}' did not stop within timeout.")
                return False

            with self.lock:
                self.pipelines.pop(pipeline_id, None)

            if self.state_manager:
                try:
                    self.state_manager.set_pipeline_status(pipeline_id, False)
                except Exception as e:
                    logger.warning(f"Failed to persist stop state for '{pipeline_id}': {e}")
            return True

    def _deploy_model(self, cmd: DeployModelCommand) -> None:
        """Validate a DEPLOY_MODEL and dispatch the download to a worker thread.

        Args:
            cmd: The DEPLOY_MODEL command to validate and dispatch.
        """
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
            # Start under the lock so _cancel_deploy can't observe is_alive()==False
            # between register and start and miss a pending CANCEL_DEPLOY.
            logger.info(f"Initiating deployment for command '{command_id}'...")
            thread.start()

    def _download_lock_for(self, model_name: str) -> threading.Lock:
        """Per-model_name download lock, created on first use. Concurrent deploys
        of the same target file share it so they serialize instead of racing the
        `.partial` staging path."""
        with self._download_locks_guard:
            return self._download_locks.setdefault(model_name, threading.Lock())

    def _deploy_worker(self, cmd: DeployModelCommand, cancel_event: threading.Event) -> None:
        """Orchestrate download → publish → commit for a single DEPLOY_MODEL.

        Delegates the two heavy phases to `_download_and_publish` and
        `_commit_deploy`. Any terminal condition is reported by the phase
        that hit it; this method only wires the phases and cleans up the
        worker registration in `finally`.

        Args:
            cmd: The DEPLOY_MODEL command being executed.
            cancel_event: Set by `_cancel_deploy` to break the chunk loop.
        """
        pipeline_id = cmd.pipeline_id
        command_id = cmd.id
        try:
            models_dir = Path.cwd().joinpath("models")
            models_dir.mkdir(parents=True, exist_ok=True)
            target_path = models_dir / cmd.model_name
            partial_path = target_path.with_name(target_path.name + ".partial")
            download_url = (
                f"{self.agent_config.identity.api_url}/models/{pipeline_id}/download/"
                + self.agent_config.identity.device_id
                + "/agent"
            )

            # Serialize downloads that share this target file so a concurrent
            # same-file deploy doesn't race the `.partial` rename; the waiter
            # then sees the cached file below and skips the re-download.
            with self._download_lock_for(cmd.model_name):
                if target_path.exists():
                    logger.info(f"Model {cmd.model_name} already exists. Using cached file.")
                elif not self._download_and_publish(cmd, cancel_event, download_url, target_path, partial_path):
                    return

            if not self._commit_deploy(cmd, cancel_event):
                self._report_cancelled(cmd, "before configuring")
                return

            logger.info(f"Pipeline '{pipeline_id}' deployed successfully.")
            self.status_logger.report_deployment_progress(pipeline_id, "completed", 100.0, 0, 0)
            self.health_state.set_deployment_progress(pipeline_id, "completed", 100.0, cmd.model_name)
            self.status_logger.report_command(command_id, "completed", f"Model {cmd.model_name} deployed successfully.")
        except Exception as e:
            logger.error(f"Deploy '{pipeline_id}' failed: {e}", exc_info=True)
            self.status_logger.report_command(command_id, "failed", str(e))
            self.health_state.set_deployment_progress(pipeline_id, "completed", 0.0, cmd.model_name)
        finally:
            with self.lock:
                worker = self._agent_workers.get(pipeline_id)
                if worker is not None and worker.command_id == command_id:
                    del self._agent_workers[pipeline_id]

    def _download_and_publish(
        self,
        cmd: DeployModelCommand,
        cancel_event: threading.Event,
        download_url: str,
        target_path: Path,
        partial_path: Path,
    ) -> bool:
        """Stream the model to `partial_path`, then rename to `target_path`.

        Reports its own failure / cancellation via `status_logger`; the caller
        only needs the terminal-vs-continue signal.

        Returns:
            True when the file was published to `target_path`; False if the
            download failed or was cancelled at any point up to (and
            including) the rename.
        """
        pipeline_id = cmd.pipeline_id
        command_id = cmd.id

        if partial_path.exists():
            try:
                partial_path.unlink()
            except OSError as e:
                logger.warning(f"Failed to remove stale partial {partial_path}: {e}")

        logger.info(f"Downloading {cmd.model_name} from {download_url}...")
        cancelled = False
        total = 0
        done = 0
        # `(connect, read)`: cap a stalled or hung stream so a cancel that
        # slips past the chunk-loop check still surfaces within seconds
        # rather than waiting on the old 600 s single timeout.
        download_timeout: tuple[float, float] = (10.0, 30.0)
        try:
            headers = {"Authorization": f"Bearer {self.agent_config.identity.api_key}"}
            with requests.get(download_url, headers=headers, stream=True, timeout=download_timeout) as r:
                self._attach_response(pipeline_id, command_id, r)
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                last_reported = -1
                self.status_logger.report_deployment_progress(pipeline_id, "downloading", 0.0, 0, total)
                self.health_state.set_deployment_progress(pipeline_id, "downloading", 0.0, cmd.model_name)
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
                                self.health_state.set_deployment_progress(
                                    pipeline_id, "downloading", float(pct), cmd.model_name
                                )
                                last_reported = pct
        except Exception as e:
            if cancel_event.is_set():
                cancelled = True
            else:
                self._cleanup_partial(partial_path)
                msg = f"Failed to download model: {e}"
                logger.error(msg)
                self.status_logger.report_command(command_id, "failed", msg)
                # Drop the in-flight row so the UI doesn't leave a stuck progress bar.
                self.health_state.set_deployment_progress(pipeline_id, "completed", 0.0, cmd.model_name)
                return False
        finally:
            self._attach_response(pipeline_id, command_id, None)

        if cancelled:
            self._cleanup_partial(partial_path)
            self._report_cancelled(cmd, "during download")
            return False

        # Guard the rename: `iter_content` can return normally on a short
        # read (server closes early, proxy truncates, etc.)
        if total and done != total:
            self._cleanup_partial(partial_path)
            msg = f"incomplete download: {done}/{total} bytes"
            logger.error(msg)
            self.status_logger.report_command(command_id, "failed", msg)
            self.health_state.set_deployment_progress(pipeline_id, "completed", 0.0, cmd.model_name)
            return False

        # Guard the rename: a cancel arriving after the last chunk but
        # before the atomic replace must not publish the file.
        if cancel_event.is_set():
            self._cleanup_partial(partial_path)
            self._report_cancelled(cmd, "before publish")
            return False

        partial_path.replace(target_path)
        logger.info(f"Model saved to {target_path}")
        return True

    def _commit_deploy(self, cmd: DeployModelCommand, cancel_event: threading.Event) -> bool:
        """Register `cmd.config` on the runtime and persist it to state.

        Args:
            cmd: The DEPLOY_MODEL command being finalised.
            cancel_event: A final cancel signal — checked inside the write
                lock so a cancel that arrives after the download completes
                still prevents the pipeline_config commit.

        Returns:
            True when the config was committed; False when a cancel
            arrived after acquiring the lock (caller should report).
        """
        self.status_logger.report_deployment_progress(cmd.pipeline_id, "configuring", 95.0, 0, 0)
        self.health_state.set_deployment_progress(cmd.pipeline_id, "configuring", 95.0, cmd.model_name)
        # Serialise the commit against a concurrent reconfigure (apply_config).
        with self._pipeline_ops_lock:
            with self.lock:
                if cancel_event.is_set():
                    return False
                self.pipeline_configs[cmd.pipeline_id] = cmd.config
                if self.state_manager:
                    try:
                        self.state_manager.update_pipeline_config(cmd.config)
                    except Exception as e:
                        logger.warning(f"Failed to persist state: {e}")
        return True

    def _attach_response(self, pipeline_id: str, command_id: str, response: requests.Response | None) -> None:
        """Stash (or clear) the streaming response on this pipeline's worker.

        Used only by `_download_and_publish` so `_cancel_deploy` can close
        the socket and unblock `iter_content` without waiting for the read
        timeout. Guarded by `worker.command_id == command_id` so we never
        clobber a newer worker if the caller races with cleanup.
        """
        with self.lock:
            worker = self._agent_workers.get(pipeline_id)
            if worker is not None and worker.command_id == command_id:
                worker.response = response

    def _report_cancelled(self, cmd: DeployModelCommand, when: str) -> None:
        """Emit the cancelled progress + failed command pair for `cmd`."""
        logger.info(f"Deploy '{cmd.pipeline_id}' cancelled {when}.")
        self.status_logger.report_deployment_progress(cmd.pipeline_id, "cancelled", 0.0, 0, 0)
        # Clear the in-flight row so the UI drops the progress wheel.
        self.health_state.set_deployment_progress(cmd.pipeline_id, "completed", 0.0, cmd.model_name)
        self.status_logger.report_command(cmd.id, "failed", "cancelled")

    def _cancel_deploy(self, cmd: CancelDeployCommand) -> None:
        """Signal an in-flight DEPLOY_MODEL for `cmd.pipeline_id` to abort.

        Args:
            cmd: The CANCEL_DEPLOY command naming the pipeline to abort.
        """
        with self.lock:
            worker = self._agent_workers.get(cmd.pipeline_id)
            if worker is None or not worker.thread.is_alive():
                self.status_logger.report_command(
                    cmd.id, "completed", f"No active deploy for pipeline '{cmd.pipeline_id}'"
                )
                return
            worker.cancel_event.set()
            original = worker.command_id
            active_response = worker.response

        # Close the stashed streaming response so `iter_content` unblocks
        # immediately instead of waiting for the read timeout. Best effort —
        # cancel_event is authoritative; this only shortens latency.
        if active_response is not None:
            try:
                active_response.close()
            except Exception as e:
                logger.debug(f"Failed to close active deploy response: {e}")

        logger.info(f"Cancelling deploy for '{cmd.pipeline_id}' (deploy command '{original}')")
        self.status_logger.report_command(cmd.id, "completed", f"Cancel signal sent to deploy '{original}'")

    def _cleanup_partial(self, partial_path: Path) -> None:
        """Best-effort delete of a `.partial` file; log and continue on failure.

        Args:
            partial_path: The `.partial` file to remove.
        """
        if partial_path.exists():
            try:
                partial_path.unlink()
            except OSError as e:
                logger.warning(f"Failed to delete partial {partial_path}: {e}")

    def _update_pipeline(self, cmd: UpdatePipelineCommand) -> None:
        """Updates a deployed pipeline's configuration, preserving its running state.

        If the pipeline is running, it restarts with the new configuration.
        Otherwise, the configuration is saved for the next start.

        Args:
            cmd: The UPDATE_PIPELINE command carrying the new config.
        """

        pipeline_id = cmd.pipeline_id

        if cmd.config.id != pipeline_id:
            msg = f"Refusing update: pipeline_id {pipeline_id!r} != config.id {cmd.config.id!r}"
            logger.error(msg)
            self.status_logger.report_command(cmd.id, "failed", "Pipeline id mismatch")
            return

        # Hold _pipeline_ops_lock so the running-state check and the resulting
        # start/commit stay atomic against a concurrent reconfigure.
        with self._pipeline_ops_lock:
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
    ) -> None:
        """Uninstalls a pipeline by removing its configuration and on-disk artifacts.

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

        # Link-initiated removals (loopback command id) are invisible to Control,
        # so report them so the dashboard drops the model. Control-initiated
        # UNINSTALL_MODEL commands are already tracked by Control, not re-reported.
        if command_id.startswith("loopback-"):
            self._report_model_uninstalled_to_control(pipeline_id)

    def _report_model_uninstalled_to_control(self, model_id: str) -> None:
        """Tell Control a local model removal happened so the dashboard stops
        showing it. A delivery failure (offline, 5xx) queues the report; the
        main loop retries until Control acknowledges it.
        """
        if not self._post_uninstall_report(model_id):
            self._queue_pending_uninstall(model_id)

    def _post_uninstall_report(self, model_id: str) -> bool:
        """Single delivery attempt. Returns False only on retryable failures.

        Device-authenticated with the session api_key. Local removal is the
        source of truth: no identity or a non-HTTPS api_url means the report
        can never be sent, so those return True (drop, don't retry). 404 =
        device/model already absent on Control, which is fine.
        """
        ident = self.agent_config.identity if self.agent_config else None
        if not ident or not ident.api_url or not ident.api_key or not ident.device_id:
            return True
        base = ident.api_url.rstrip("/")
        if not base.startswith("https://"):
            logger.warning("Skipping Control uninstall report: api_url is not HTTPS")
            return True
        url = f"{base}/agent/{quote(ident.device_id, safe='')}/models/{quote(model_id, safe='')}/uninstalled"
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {ident.api_key}"},
                timeout=(5.0, 10.0),
            )
            if resp.status_code in (200, 404):
                return True
            logger.warning(f"Control uninstall report for '{model_id}' returned HTTP {resp.status_code}")
            return False
        except requests.RequestException as e:
            logger.warning(f"Control uninstall report for '{model_id}' failed (will retry): {e}")
            return False

    def _load_pending_uninstalls(self) -> list[str]:
        try:
            ids = json.loads(PENDING_UNINSTALL_REPORTS_PATH.read_text())
            return [m for m in ids if isinstance(m, str)] if isinstance(ids, list) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read pending uninstall reports (dropping): {e}")
            return []

    def _save_pending_uninstalls(self, ids: list[str]) -> None:
        try:
            if not ids:
                PENDING_UNINSTALL_REPORTS_PATH.unlink(missing_ok=True)
                return
            PENDING_UNINSTALL_REPORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = PENDING_UNINSTALL_REPORTS_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(ids))
            tmp.replace(PENDING_UNINSTALL_REPORTS_PATH)
        except OSError as e:
            logger.warning(f"Could not persist pending uninstall reports: {e}")

    def _queue_pending_uninstall(self, model_id: str) -> None:
        with self._pending_reports_lock:
            ids = self._load_pending_uninstalls()
            if model_id not in ids:
                ids.append(model_id)
                self._save_pending_uninstalls(ids)
        logger.info(f"Queued Control uninstall report for '{model_id}'.")

    def _flush_pending_uninstall_reports(self) -> None:
        """Re-sends queued uninstall reports; delivered ones leave the queue.

        A flush failure (corrupt queue file, disk error) must never crash the
        runtime loop, so any error is logged and swallowed.
        """
        try:
            with self._pending_reports_lock:
                ids = self._load_pending_uninstalls()
                if not ids:
                    return
                remaining = [m for m in ids if not self._post_uninstall_report(m)]
                if remaining != ids:
                    self._save_pending_uninstalls(remaining)
                    logger.info(f"Delivered {len(ids) - len(remaining)} queued uninstall report(s) to Control.")
        except Exception as e:  # noqa: BLE001 - a flush failure must never crash the runtime loop
            logger.warning(f"Pending uninstall report flush failed: {e}")

    def _log_status(self) -> None:
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

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle SIGINT/SIGTERM for graceful shutdown.

        Args:
            signum (int): The signal number.
            frame (Any): The current stack frame.
        """
        logger.info(f"Signal {signum} received. Stopping Agent...")
        self.running = False
        self.shutdown_event.set()

    def _shutdown(self) -> None:
        """Graceful System Shutdown.

        Stops all pipelines and cleans up resources.
        """
        # Cancel any in-flight deploy workers before touching pipelines.
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

        # Flip transport.connected=false so a companion mid-poll sees the disconnect.
        if self.health_state.transport_type is not None:
            self.health_state.set_transport(
                transport_type=self.health_state.transport_type,
                endpoint=self.health_state.transport_endpoint,
                connected=False,
            )

        # Tear down the health server last.
        self.health_server.stop()


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
