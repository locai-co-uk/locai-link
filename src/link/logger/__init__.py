# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from . import core as link_logger
from .core import (
    CatchContext,
    LogClient,
    Result,
    catch,
    critical,
    # Helper functions
    fail,
    info,
    ok,
    progress,
    raise_if,
    safe,
    warn,
    # Retry/error handling
    wrap,
)

# Exceptions
from .exceptions import (
    APIConnectionLog,
    APIRedirectLog,
    APIResponseLog,
    APITimeoutLog,
    # Authentication
    AuthenticationLog,
    CommandExecutionLog,
    CommandTimeoutLog,
    ConfigDownloadLog,
    ConfigFileNotFoundLog,
    ConfigParseLog,
    # Configuration
    ConfigurationLog,
    ConfigValidationLog,
    # Deployment
    DeploymentLog,
    DeviceAlreadyActivatedLog,
    DeviceNotActivatedLog,
    HardwareAccessLog,
    InferenceAlreadyRunningLog,
    InferenceNotRunningLog,
    InferenceStartLog,
    InferenceStopLog,
    InvalidAPIKeyLog,
    InvalidDeviceModeLog,
    InvalidTokenLog,
    # Base
    LinkLog,
    LogCategory,
    # Enums
    LogSeverity,
    MetricsCollectionLog,
    MissingConfigLog,
    MissingDeploymentPayloadLog,
    ModelDownloadLog,
    ModelNotFoundLog,
    ModelSaveLog,
    # Network
    NetworkLog,
    # Process
    ProcessLog,
    RegistrationKeyLog,
    StorageLog,
    UnknownCommandLog,
)
from .exceptions import (
    # System
    SystemLog as LinkSystemLog,
)

__all__ = [
    # ===== Facade =====
    "link_logger",
    "LogClient",
    "Result",
    # ===== Helper Functions =====
    "fail",
    "ok",
    "warn",
    "info",
    "critical",
    "progress",
    "raise_if",
    # ===== Error Handling =====
    "wrap",
    "catch",
    "safe",
    "CatchContext",
    # ===== Enums =====
    "LogSeverity",
    "LogCategory",
    # ===== Exceptions =====
    "LinkLog",
    # Network
    "NetworkLog",
    "APIConnectionLog",
    "APIResponseLog",
    "APITimeoutLog",
    "APIRedirectLog",
    # Authentication
    "AuthenticationLog",
    "InvalidTokenLog",
    "InvalidAPIKeyLog",
    "DeviceNotActivatedLog",
    "DeviceAlreadyActivatedLog",
    "RegistrationKeyLog",
    # Configuration
    "ConfigurationLog",
    "ConfigFileNotFoundLog",
    "ConfigParseLog",
    "ConfigValidationLog",
    "MissingConfigLog",
    "InvalidDeviceModeLog",
    # Deployment
    "DeploymentLog",
    "ModelDownloadLog",
    "ModelSaveLog",
    "MissingDeploymentPayloadLog",
    "ConfigDownloadLog",
    # Process
    "ProcessLog",
    "InferenceStartLog",
    "InferenceAlreadyRunningLog",
    "InferenceStopLog",
    "InferenceNotRunningLog",
    "ModelNotFoundLog",
    "CommandTimeoutLog",
    "CommandExecutionLog",
    "UnknownCommandLog",
    # System
    "LinkSystemLog",
    "MetricsCollectionLog",
    "HardwareAccessLog",
    "StorageLog",
]
