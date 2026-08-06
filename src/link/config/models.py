# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Config schema for Loc.ai:Link agents.

This module defines the canonical `AgentConfig` schema the backend returns to an
agent during registration/enrollment. The top-level `AgentConfig` model below is
the contract between the control plane and the edge agent.

Integrators (backend developers, operators writing config templates) should
read this file top-to-bottom:

- `AgentConfig`              : root object
- `IdentityConfig`           : device credentials
- `TransportConfig`          : data-plane transport (HTTP or Zenoh)
- `LoggingConfig`            : log handler routing
- `ReportingConfig`          : lifecycle/status handler routing
- `PipelineConfig`           : a running source-to-sink dataflow
- `GenericConfig`            : the building block used for sources, sinks, handlers

Known component `type` values (sources, sinks, handlers) are enumerated in
`docs/config-schema.md`. New values can be added by shipping a plugin that
registers itself with `ComponentRegistry`.

To regenerate the JSON schema for backend validation:

    python -c "from link.config.models import AgentConfig; \\
               import json; print(json.dumps(AgentConfig.model_json_schema(), indent=2))"
"""

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# Schema version the agent understands. Backend templates must declare the same
# value. Incompatible configs are rejected at bootstrap.
SCHEMA_VERSION = 2.1


# -- Generic building block ----------------------------------------------------


class TransportArgs(BaseModel):
    """Typed view of a Zenoh transport's ``args``. Parsed from the raw
    ``GenericConfig.args`` dict at the consumer (see ``ZenohClient``) so a
    missing or mistyped field surfaces as validation rather than a silent
    ``None``. Unknown keys are ignored for forward-compatibility with Control.
    """

    model_config = ConfigDict(extra="ignore")

    mode: Literal["client", "peer", "router"] = "client"
    endpoints: list[str] = Field(default_factory=list)
    tls_root_ca: str | None = None
    username: str | None = None
    password: str | None = None


class GenericConfig(BaseModel):
    """A pluggable component reference: `{type, args}`.

    Used for sources, sinks, transports, and logging/reporting handlers. The
    `type` string is looked up in the component registry at runtime; `args` is
    passed as keyword arguments to the component's constructor.

    Known `type` values shipped in-core:

    **Sources** (emit data):
      - `clock_tick`      : periodic timestamp tick
      - `random_gen`      : random-value generator
      - `http_poll`       : poll an HTTP endpoint at a fixed interval
      - `system_monitor`  : CPU/RAM/storage/temperature metrics
      - `zenoh_sub`       : Zenoh subscription

    **Sinks** (consume data):
      - `console`         : print to stdout
      - `http_post`       : POST to an HTTP endpoint
      - `command`         : dispatch to the agent's command handler
      - `zenoh_pub`       : publish to a Zenoh topic

    **Logging/reporting handlers**:
      - `console`         : print logs to stdout
      - `http`            : async HTTP POST/PUT with route-keyed URLs
      - `zenoh`           : async Zenoh publish with route-keyed topics
      - `posthog`         : (optional) per-device PostHog events
      - `hubspot`         : (optional) per-device HubSpot timeline events

    Plugins can register additional `type` values; see `plugins/*/adapter.py`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"type": "console"},
                {"type": "clock_tick", "args": {"interval": 1.0}},
                {
                    "type": "http_poll",
                    "args": {
                        "url": "https://api.loc.ai/api/v1/agent/${identity.device_id}/commands",
                        "api_key": "${identity.api_key}",
                        "interval": 10,
                    },
                },
            ]
        }
    )

    type: str = Field(
        description=(
            "Component type identifier. Must match a registered component name. "
            "See the module docstring for the list of in-core types."
        ),
        examples=["http_post", "console", "system_monitor"],
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Keyword arguments passed to the component constructor. "
            "Shape depends on the `type`. See docs/config-schema.md for per-type args."
        ),
    )


# -- Identity ------------------------------------------------------------------


class IdentityConfig(BaseModel):
    """Device identity and control-plane credentials.

    Populated by the backend at registration time. The agent uses these values
    to substitute `${identity.device_id}`, `${identity.api_key}`, and
    `${identity.api_url}` placeholders elsewhere in the config.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "device_id": "dev_abc123",
                    "device_name": "factory-line-01",
                    "api_url": "https://api.loc.ai/api/v1",
                    "api_key": "sk_live_xxx",
                }
            ]
        }
    )

    device_id: str = Field(
        description="Unique device identifier assigned by the control plane.",
        examples=["dev_abc123"],
    )
    device_name: str = Field(
        default="unknown",
        description="Human-readable device name (set by the operator at registration).",
        examples=["factory-line-01", "reception-camera"],
    )
    api_url: str | None = Field(
        default=None,
        description="Base URL of the control-plane API this device reports to.",
        examples=["https://api.loc.ai/api/v1"],
    )
    api_key: str | None = Field(
        default=None,
        description="Bearer token for authenticating device→control-plane requests.",
        examples=["sk_live_xxx"],
    )


# -- Transport -----------------------------------------------------------------


class TransportConfig(GenericConfig):
    """Data-plane transport used by pipelines.

    Two transports ship in-core:

    - `http`  : no external dependency; pipelines use `http_poll` / `http_post`.
      No `args` required.
    - `zenoh` : peer-to-peer pub/sub via Eclipse Zenoh. Requires `args.endpoints`
      (list of router endpoints) and optionally `args.mode` (`peer` | `client`).

    Omitting `transport` from `AgentConfig` is equivalent to `{type: "http"}`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"type": "http"},
                {
                    "type": "zenoh",
                    "args": {
                        "endpoints": ["tcp/router.internal:7447"],
                        "mode": "peer",
                    },
                },
            ]
        }
    )

    type: Literal["http", "zenoh"] = Field(  # pyright: ignore[reportIncompatibleVariableOverride]
        description="Transport implementation to use for pipeline sources and sinks.",
    )


# -- Logging -------------------------------------------------------------------


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class LoggingConfig(BaseModel):
    """System log routing.

    Routes Python log records from the agent to one or more sinks. Each handler
    is a `GenericConfig` whose `type` selects an implementation:

    - `console` : print to stdout.
    - `http`    : async POST to a URL. Required `args.url`. Optional `args.api_key`
      (sent as `Authorization: Bearer …`).
    - `zenoh`   : async publish to a topic. Required `args.topic`.

    Any handler may set `args.level` (`"DEBUG" | "INFO" | "WARNING" | "ERROR"`)
    to override the parent `LoggingConfig.level` for that single handler. This
    lets you route DEBUG logs to console but only ship WARNING+ to a remote
    endpoint, for example.

    If `handlers` is empty, the agent installs a default `console` handler.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "level": "INFO",
                    "handlers": [
                        {"type": "console", "args": {"level": "DEBUG"}},
                        {
                            "type": "http",
                            "args": {
                                "level": "WARNING",
                                "url": "https://api.loc.ai/api/v1/agent/${identity.device_id}/logs",
                                "api_key": "${identity.api_key}",
                            },
                        },
                    ],
                }
            ]
        }
    )

    level: LogLevel = Field(
        default="INFO",
        description=(
            "Minimum severity to emit at the logger level. Handlers without their "
            "own `args.level` inherit this; handlers with `args.level` set can go "
            "higher or lower."
        ),
    )
    handlers: list[GenericConfig] = Field(
        default_factory=list,
        description=(
            "Fan-out targets for log records. Each handler is instantiated once "
            "at startup and receives every record that clears its effective level."
        ),
    )


# -- Reporting -----------------------------------------------------------------


class ReportingConfig(BaseModel):
    """Lifecycle and status reporting.

    Separate from `logging` because these handlers route *structured status*
    events (device online/offline, command completed, model state changed) to
    route-keyed destinations, not free-text log records.

    Each `http` handler supports route keys in its `args` to target different
    endpoints per event type:

    - `lifecycle_status` : PUT for device online/offline
    - `command_status`   : POST for command completion (placeholder `{cid}` is
       the command ID, substituted at emit time)
    - `model_status`     : POST for model state (placeholder `{mid}` is the
       model ID)

    Identity placeholders (`${identity.device_id}`, etc.) are resolved once at
    startup. Runtime placeholders (`{cid}`, `{mid}`) stay as literal curly
    braces in the config and are substituted per-event.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "interval": 30,
                    "handlers": [
                        {"type": "console"},
                        {
                            "type": "http",
                            "args": {
                                "lifecycle_status": ("https://api.loc.ai/api/v1/agent/${identity.device_id}/status"),
                                "command_status": (
                                    "https://api.loc.ai/api/v1/agent/${identity.device_id}/commands/{cid}/status"
                                ),
                                "model_status": (
                                    "https://api.loc.ai/api/v1/agent/${identity.device_id}/models/{mid}/status"
                                ),
                                "api_key": "${identity.api_key}",
                            },
                        },
                    ],
                }
            ]
        }
    )

    interval: int = Field(
        default=30,
        ge=1,
        description="Heartbeat interval in seconds (reserved for future use — not currently enforced).",
    )
    handlers: list[GenericConfig] = Field(
        default_factory=list,
        description="Fan-out targets for lifecycle, command, and model status events.",
    )


# -- Pipelines -----------------------------------------------------------------


class PipelineConfig(BaseModel):
    """A single data pipeline: one source feeding one sink.

    Pipelines are the agent's execution primitive. Each one runs on its own
    thread. The source emits data (by being called), the sink consumes it.

    Common patterns:

    - **Command polling**: `source: http_poll`, `sink: command`: fetches pending
      commands from the control plane and dispatches them to the runtime.
    - **System metrics**: `source: system_monitor`, `sink: http_post`: periodic
      telemetry to the control plane.
    - **Inference**: `source: <plugin_name>` (e.g. `language_model`), `sink:
      http_post`: a plugin produces inference results; sink reports them.

    Set `active: true` to auto-start on boot. Pipelines can also be started and
    stopped via runtime commands (`START_MODEL_INFERENCE`, `STOP_MODEL_INFERENCE`).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "command_center",
                    "active": True,
                    "source": {
                        "type": "http_poll",
                        "args": {
                            "url": "https://api.loc.ai/api/v1/agent/${identity.device_id}/commands",
                            "api_key": "${identity.api_key}",
                            "interval": 10,
                        },
                    },
                    "sink": {"type": "command"},
                },
                {
                    "id": "system_metrics",
                    "active": True,
                    "source": {
                        "type": "system_monitor",
                        "args": {
                            "interval": 5,
                            "metrics": ["cpu_usage", "ram_usage", "temperature_celsius"],
                        },
                    },
                    "sink": {
                        "type": "http_post",
                        "args": {
                            "url": "https://api.loc.ai/api/v1/agent/${identity.device_id}/metrics",
                            "api_key": "${identity.api_key}",
                            "timeout": 30,
                        },
                    },
                },
            ]
        }
    )

    id: str = Field(
        description="Unique pipeline identifier. Used for runtime start/stop commands.",
        examples=["command_center", "system_metrics", "inference_llm"],
    )
    active: bool = Field(
        default=False,
        description="If true, the pipeline auto-starts at agent boot.",
    )
    source: GenericConfig = Field(
        description="The component that produces data for this pipeline.",
    )
    sink: GenericConfig | None = Field(
        default=None,
        description=(
            "The component that consumes data from the source. "
            "If null, the source runs for its side effects only (rare — usually set)."
        ),
    )


# -- Root ----------------------------------------------------------------------


class AgentConfig(BaseModel):
    """Root configuration delivered to a Loc.ai:Link agent.

    This is what the backend returns in the `config` field of the registration
    and activation responses. The agent saves it as the initial session state
    and uses it as the source of truth for all subsequent operations.

    Backend integrators should treat this model as the contract: all fields
    accept `${identity.*}` template placeholders for values that the agent fills
    in from the registration response. Unresolved placeholders in other
    namespaces (e.g. `{cid}`, `{mid}`) are preserved and substituted by runtime
    handlers at emit time.

    Minimum viable config (only what's required):

        {
            "version": 2.1,
            "identity": {"device_id": "dev_abc123"}
        }

    A realistic production config is shown in the `examples` block of this model.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "version": 2.1,
                    "identity": {
                        "device_id": "dev_abc123",
                        "device_name": "factory-line-01",
                        "api_url": "https://api.loc.ai/api/v1",
                        "api_key": "sk_live_xxx",
                    },
                    "transport": {"type": "http"},
                    "logging": {
                        "level": "INFO",
                        "handlers": [
                            {"type": "console"},
                            {
                                "type": "http",
                                "args": {
                                    "url": ("https://api.loc.ai/api/v1/agent/${identity.device_id}/logs"),
                                    "api_key": "${identity.api_key}",
                                },
                            },
                        ],
                    },
                    "reporting": {
                        "interval": 30,
                        "handlers": [
                            {"type": "console"},
                            {
                                "type": "http",
                                "args": {
                                    "lifecycle_status": (
                                        "https://api.loc.ai/api/v1/agent/${identity.device_id}/status"
                                    ),
                                    "command_status": (
                                        "https://api.loc.ai/api/v1/agent/${identity.device_id}/commands/{cid}/status"
                                    ),
                                    "model_status": (
                                        "https://api.loc.ai/api/v1/agent/${identity.device_id}/models/{mid}/status"
                                    ),
                                    "api_key": "${identity.api_key}",
                                },
                            },
                        ],
                    },
                    "pipelines": [
                        {
                            "id": "command_center",
                            "active": True,
                            "source": {
                                "type": "http_poll",
                                "args": {
                                    "url": ("https://api.loc.ai/api/v1/agent/${identity.device_id}/commands"),
                                    "api_key": "${identity.api_key}",
                                    "interval": 10,
                                },
                            },
                            "sink": {"type": "command"},
                        },
                        {
                            "id": "system_metrics",
                            "active": True,
                            "source": {
                                "type": "system_monitor",
                                "args": {
                                    "interval": 5,
                                    "metrics": [
                                        "cpu_usage",
                                        "ram_usage",
                                        "temperature_celsius",
                                        "storage_available_gb",
                                    ],
                                },
                            },
                            "sink": {
                                "type": "http_post",
                                "args": {
                                    "url": ("https://api.loc.ai/api/v1/agent/${identity.device_id}/metrics"),
                                    "api_key": "${identity.api_key}",
                                    "timeout": 30,
                                },
                            },
                        },
                    ],
                }
            ]
        }
    )

    version: float = Field(
        description=(
            f"Schema version. Must equal {SCHEMA_VERSION}. Agents reject configs with an unrecognised version."
        ),
        examples=[SCHEMA_VERSION],
    )
    identity: IdentityConfig = Field(
        description="Device credentials (populated by the backend at registration).",
    )
    transport: TransportConfig | None = Field(
        default=None,
        description=(
            "Data-plane transport for pipelines. Omit (or use `{type: 'http'}`) "
            "unless the device should communicate via Zenoh."
        ),
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="System log routing.",
    )
    reporting: ReportingConfig = Field(
        default_factory=ReportingConfig,
        description="Lifecycle, command, and model status event routing.",
    )
    pipelines: list[PipelineConfig] = Field(
        default_factory=list,
        description=(
            "Pipelines to define (and optionally auto-start). Every production "
            "device typically has at least `command_center` and `system_metrics`."
        ),
    )
