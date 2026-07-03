# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Typed command schema — the control-plane → agent contract."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from link.config.models import AgentConfig, PipelineConfig


class _CommandBase(BaseModel):
    """Envelope fields shared by every command variant."""

    model_config = ConfigDict(
        # Reject unknown top-level fields so a rogue key on the wire surfaces
        # as a validation error instead of being silently dropped.
        extra="forbid",
    )

    id: str = Field(description="Unique command ID — the agent echoes this in every status update it emits.")


class DeployModelCommand(_CommandBase):
    """Download a model artifact and register its pipeline definition."""

    type: Literal["DEPLOY_MODEL"] = "DEPLOY_MODEL"
    pipeline_id: str = Field(description="Target pipeline identifier — also used as the model download key.")
    model_name: str = Field(description="Artifact filename to save under models/ (e.g. 'SmolLM2-135M.gguf').")
    config: PipelineConfig = Field(
        description=(
            "Complete pipeline definition. The agent stores this as-is. Use "
            "`${models_dir}/${model_name}` in `source.args.model_path` if you "
            "need the agent to resolve the on-disk location at runtime."
        ),
    )


class StartModelCommand(_CommandBase):
    """Start (or replace) a pipeline from a full inline config."""

    type: Literal["START_MODEL"] = "START_MODEL"
    pipeline_id: str
    config: PipelineConfig = Field(description="Complete pipeline definition — overrides any stored config.")


class StartModelInferenceCommand(_CommandBase):
    """Resume a previously-deployed pipeline in inference mode."""

    type: Literal["START_MODEL_INFERENCE"] = "START_MODEL_INFERENCE"
    pipeline_id: str


class StopModelInferenceCommand(_CommandBase):
    """Stop a pipeline currently running in inference mode."""

    type: Literal["STOP_MODEL_INFERENCE"] = "STOP_MODEL_INFERENCE"
    pipeline_id: str


class StartServingCommand(_CommandBase):
    """Start a deployed pipeline in 'serve' mode (exposes an HTTP endpoint)."""

    type: Literal["START_SERVING"] = "START_SERVING"
    pipeline_id: str
    port: int = 8100
    host: str = "0.0.0.0"
    model_display_name: str = Field(default="locai-model", description="Alias advertised by the model server.")


class StopServingCommand(_CommandBase):
    """Stop a pipeline that's currently serving."""

    type: Literal["STOP_SERVING"] = "STOP_SERVING"
    pipeline_id: str


class UninstallModelCommand(_CommandBase):
    """Drop a pipeline's config and delete its on-disk artifact."""

    type: Literal["UNINSTALL_MODEL"] = "UNINSTALL_MODEL"
    pipeline_id: str
    force_stop: bool = Field(
        default=False,
        description="Stop the pipeline first if it's running, instead of refusing.",
    )
    # Orphaned-file fallback: let the agent locate the artifact under models/ even
    # when it no longer has a local config for this pipeline.
    filename_on_server: str | None = None
    file_extension: str | None = None


class UpdatePipelineCommand(_CommandBase):
    """Replace a deployed pipeline's definition with an updated one."""

    type: Literal["UPDATE_PIPELINE"] = "UPDATE_PIPELINE"
    pipeline_id: str
    config: PipelineConfig = Field(
        description="Complete pipeline definition; stored as-is and restarted if running.",
    )


class StatusCommand(_CommandBase):
    """Emit a status snapshot to the agent's log."""

    type: Literal["STATUS"] = "STATUS"


class UpdateAgentCommand(_CommandBase):
    """Trigger an OTA code+dependency update (git pull + plugin refresh + re-exec)."""

    type: Literal["UPDATE_AGENT"] = "UPDATE_AGENT"


class UpdateAgentConfigCommand(_CommandBase):
    """Hot-swap the agent's active `AgentConfig`."""

    type: Literal["UPDATE_AGENT_CONFIG"] = "UPDATE_AGENT_CONFIG"
    agent_config: AgentConfig


Command = Annotated[
    DeployModelCommand
    | StartModelCommand
    | StartModelInferenceCommand
    | StopModelInferenceCommand
    | StartServingCommand
    | StopServingCommand
    | UninstallModelCommand
    | UpdatePipelineCommand
    | StatusCommand
    | UpdateAgentCommand
    | UpdateAgentConfigCommand,
    Field(discriminator="type"),
]


_COMMAND_ADAPTER: TypeAdapter[Command] = TypeAdapter(Command)


def parse_command(raw: dict[str, Any]) -> Command:
    """Validate a raw command dict against the `Command` union.

    Any shape or type mismatch raises `pydantic.ValidationError` with a
    concrete field path — no translation, no fallbacks. The backend and the
    agent share this schema verbatim, so there's nothing to paper over.

    Args:
        raw: The decoded JSON body as a dict.

    Returns:
        A validated instance of one of the `Command` variants.
    """
    return _COMMAND_ADAPTER.validate_python(raw)
