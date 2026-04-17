# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class GenericConfig(BaseModel):
    """Represents a generic component (Source, Sink, Transport, Handler)."""

    type: str
    args: dict[str, Any] = Field(default_factory=dict)


class IdentityConfig(BaseModel):
    """Device Identity (Control Plane)."""

    device_id: str
    device_name: str = "unknown"
    api_url: str | None = None
    api_key: str | None = None


class TransportConfig(GenericConfig):
    """Main Data Plane Transport (e.g., Zenoh).

    Inherits type/args from GenericConfig.
    """

    pass


class LoggingConfig(BaseModel):
    """System Logs Configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    handlers: list[GenericConfig] = Field(default_factory=list)


class ReportingConfig(BaseModel):
    """Lifecycle Status Configuration (Heartbeats)."""

    interval: int = 30
    handlers: list[GenericConfig] = Field(default_factory=list)


class PipelineConfig(BaseModel):
    """Data Pipeline Configuration."""

    id: str
    active: bool = False
    source: GenericConfig
    sink: GenericConfig | None = None


class AgentConfig(BaseModel):
    """Root Configuration Object."""

    version: float
    identity: IdentityConfig

    # Optional Systems
    transport: TransportConfig | None = None

    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)

    pipelines: list[PipelineConfig] = []
