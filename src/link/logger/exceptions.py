# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from enum import Enum
from typing import Any, Dict, Optional


class LogSeverity(Enum):
    """Severity levels for logs."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogCategory(Enum):
    """Categories of logs in the device agent system."""

    PROCESS = "process"
    DEPLOY = "deploy"
    CONFIG = "config"
    NETWORK = "network"
    AUTHENTICATION = "authentication"
    SYSTEM = "system"
    UNKNOWN = "unknown"


# --- Base Exception ---


class LinkLog(Exception):
    """Base exception/log class for all Link (Device Agent) logs.

    Attributes:
        message (str): The log message.
        context (Optional[Dict[str, Any]]): Additional context for the log.
        original_log (Optional[Exception]): The original exception that caused this log.
    """

    def __init__(
        self, message: str, context: Optional[Dict[str, Any]] = None, original_log: Optional[Exception] = None
    ):
        """Initialise a LinkLog instance.

        Args:
            message (str): The log message.
            context (Optional[Dict[str, Any]]): Additional context for the log.
            original_log (Optional[Exception]): The original exception that caused this log.
        """
        super().__init__(message)
        self.message = message
        self.context = context or {}
        self.original_log = original_log

    def __str__(self) -> str:
        """Return a string representation of the log.

        Returns:
            str: A string representation of the log.
        """
        base = self.message
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            base += f" [Context: {context_str}]"
        if self.original_log:
            base += f" (Caused by: {type(self.original_log).__name__}: {self.original_log})"
        return base


# --- Network Logs ---


class NetworkLog(LinkLog):
    """Base class for all network-related logs."""

    pass


class APIConnectionLog(NetworkLog):
    """Raised when unable to connect to the backend API."""

    def __init__(self, message: str = "Failed to connect to the API server", url: Optional[str] = None, **kwargs):
        """Initialise an APIConnectionLog instance.

        Args:
            message (str): The log message.
            url (Optional[str]): The URL that was being accessed.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if url:
            context["url"] = url
        super().__init__(message, context=context, **kwargs)


class APIResponseLog(NetworkLog):
    """Raised when the API returns an unexpected response."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        url: Optional[str] = None,
        **kwargs,
    ):
        """Initialise an APIResponseLog instance.

        Args:
            message (str): The log message.
            status_code (Optional[int]): The HTTP status code.
            response_body (Optional[str]): The response body.
            url (Optional[str]): The URL that was being accessed.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if status_code:
            context["status_code"] = status_code
        if response_body:
            context["response_body"] = response_body[:500]
        if url:
            context["url"] = url
        super().__init__(message, context=context, **kwargs)


class APITimeoutLog(NetworkLog):
    """Raised when an API request times out."""

    def __init__(
        self,
        message: str = "API request timed out",
        timeout_seconds: Optional[float] = None,
        url: Optional[str] = None,
        **kwargs,
    ):
        """Initialise an APITimeoutLog instance.

        Args:
            message (str): The log message.
            timeout_seconds (Optional[float]): The timeout duration in seconds.
            url (Optional[str]): The URL that was being accessed.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if timeout_seconds:
            context["timeout_seconds"] = timeout_seconds
        if url:
            context["url"] = url
        super().__init__(message, context=context, **kwargs)


class APIRedirectLog(NetworkLog):
    """Raised when the API returns an unexpected redirect."""

    def __init__(
        self,
        message: str = "Unexpected API redirect detected",
        original_url: Optional[str] = None,
        redirect_url: Optional[str] = None,
        **kwargs,
    ):
        """Initialise an APIRedirectLog instance.

        Args:
            message (str): The log message.
            original_url (Optional[str]): The original URL that was being accessed.
            redirect_url (Optional[str]): The redirect URL.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if original_url:
            context["original_url"] = original_url
        if redirect_url:
            context["redirect_url"] = redirect_url
        super().__init__(message, context=context, **kwargs)


# --- Authentication Logs ---


class AuthenticationLog(LinkLog):
    """Base class for all authentication-related logs."""

    pass


class InvalidTokenLog(AuthenticationLog):
    """Raised when the authentication token is invalid or expired."""

    def __init__(self, message: str = "Invalid or expired authentication token", **kwargs):
        """Initialise an InvalidTokenLog instance.

        Args:
            message (str): The log message.
            **kwargs: Additional keyword arguments.
        """
        super().__init__(message, **kwargs)


class InvalidAPIKeyLog(AuthenticationLog):
    """Raised when the API key is invalid."""

    def __init__(self, message: str = "Invalid API key", device_id: Optional[str] = None, **kwargs):
        """Initialise an InvalidAPIKeyLog instance.

        Args:
            message (str): The log message.
            device_id (Optional[str]): The device ID.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if device_id:
            context["device_id"] = device_id
        super().__init__(message, context=context, **kwargs)


class DeviceNotActivatedLog(AuthenticationLog):
    """Raised when trying to use a device that hasn't been activated."""

    def __init__(self, message: str = "Device is not activated", device_id: Optional[str] = None, **kwargs):
        """Initialise a DeviceNotActivatedLog instance.

        Args:
            message (str): The log message.
            device_id (Optional[str]): The device ID.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if device_id:
            context["device_id"] = device_id
        super().__init__(message, context=context, **kwargs)


class DeviceAlreadyActivatedLog(AuthenticationLog):
    """Raised when trying to activate a device that's already active."""

    def __init__(self, message: str = "Device is already activated", device_id: Optional[str] = None, **kwargs):
        """Initialise a DeviceAlreadyActivatedLog instance.

        Args:
            message (str): The log message.
            device_id (Optional[str]): The device ID.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if device_id:
            context["device_id"] = device_id
        super().__init__(message, context=context, **kwargs)


class RegistrationKeyLog(AuthenticationLog):
    """Raised when there's an issue with the registration key."""

    def __init__(self, message: str = "Invalid or expired registration key", **kwargs):
        """Initialise a RegistrationKeyLog instance.

        Args:
            message (str): The log message.
            **kwargs: Additional keyword arguments.
        """
        super().__init__(message, **kwargs)


# --- Configuration Logs ---


class ConfigurationLog(LinkLog):
    """Base class for all configuration-related logs."""

    pass


class ConfigFileNotFoundLog(ConfigurationLog):
    """Raised when a required configuration file is not found."""

    def __init__(self, message: str = "Configuration file not found", file_path: Optional[str] = None, **kwargs):
        """Initialise a ConfigFileNotFoundLog instance.

        Args:
            message (str): The log message.
            file_path (Optional[str]): The path to the configuration file.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if file_path:
            context["file_path"] = file_path
        super().__init__(message, context=context, **kwargs)


class ConfigParseLog(ConfigurationLog):
    """Raised when a configuration file cannot be parsed."""

    def __init__(self, message: str = "Failed to parse configuration file", file_path: Optional[str] = None, **kwargs):
        """Initialise a ConfigParseLog instance.

        Args:
            message (str): The log message.
            file_path (Optional[str]): The path to the configuration file.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if file_path:
            context["file_path"] = file_path
        super().__init__(message, context=context, **kwargs)


class ConfigValidationLog(ConfigurationLog):
    """Raised when configuration values are invalid."""

    def __init__(
        self,
        message: str = "Configuration validation failed",
        field: Optional[str] = None,
        value: Optional[Any] = None,
        **kwargs,
    ):
        """Initialise a ConfigValidationLog instance.

        Args:
            message (str): The log message.
            field (Optional[str]): The field that failed validation.
            value (Optional[Any]): The value that failed validation.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if field:
            context["field"] = field
        if value is not None:
            context["value"] = str(value)
        super().__init__(message, context=context, **kwargs)


class MissingConfigLog(ConfigurationLog):
    """Raised when a required configuration value is missing."""

    def __init__(self, message: str = "Required configuration value is missing", field: Optional[str] = None, **kwargs):
        """Initialise a MissingConfigLog instance.

        Args:
            message (str): The log message.
            field (Optional[str]): The missing field.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if field:
            context["missing_field"] = field
        super().__init__(message, context=context, **kwargs)


class InvalidDeviceModeLog(ConfigurationLog):
    """Raised when DEVICE_MODE is not set or invalid."""

    def __init__(self, message: str = "Invalid or missing DEVICE_MODE", device_mode: Optional[str] = None, **kwargs):
        """Initialise an InvalidDeviceModeLog instance.

        Args:
            message (str): The log message.
            device_mode (Optional[str]): The invalid device mode.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if device_mode:
            context["device_mode"] = device_mode
        context["valid_modes"] = ["LOCAI", "LOCAL", "CUSTOM"]
        super().__init__(message, context=context, **kwargs)


# --- Deployment Logs ---


class DeploymentLog(LinkLog):
    """Base class for all deployment-related logs."""

    pass


class ModelDownloadLog(DeploymentLog):
    """Raised when a model file fails to download."""

    def __init__(
        self,
        message: str = "Failed to download model",
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
        status_code: Optional[int] = None,
        **kwargs,
    ):
        """Initialise a ModelDownloadLog instance.

        Args:
            message (str): The log message.
            model_id (Optional[str]): The model ID.
            model_name (Optional[str]): The model name.
            status_code (Optional[int]): The status code.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if model_id:
            context["model_id"] = model_id
        if model_name:
            context["model_name"] = model_name
        if status_code:
            context["status_code"] = status_code
        super().__init__(message, context=context, **kwargs)


class ModelSaveLog(DeploymentLog):
    """Raised when a model file fails to save to disk."""

    def __init__(
        self,
        message: str = "Failed to save model file",
        model_name: Optional[str] = None,
        file_path: Optional[str] = None,
        **kwargs,
    ):
        """Initialise a ModelSaveLog instance.

        Args:
            message (str): The log message.
            model_name (Optional[str]): The model name.
            file_path (Optional[str]): The file path.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if model_name:
            context["model_name"] = model_name
        if file_path:
            context["file_path"] = file_path
        super().__init__(message, context=context, **kwargs)


class MissingDeploymentPayloadLog(DeploymentLog):
    """Raised when the deployment payload is missing required fields."""

    def __init__(
        self,
        message: str = "Deployment payload missing required fields",
        missing_fields: Optional[list] = None,
        **kwargs,
    ):
        """Initialise a MissingDeploymentPayloadLog instance.

        Args:
            message (str): The log message.
            missing_fields (Optional[list]): The missing fields.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if missing_fields:
            context["missing_fields"] = missing_fields
        super().__init__(message, context=context, **kwargs)


class ConfigDownloadLog(DeploymentLog):
    """Raised when a runtime config fails to download."""

    def __init__(
        self,
        message: str = "Failed to download runtime configuration",
        config_url: Optional[str] = None,
        model_id: Optional[str] = None,
        **kwargs,
    ):
        """Initialise a ConfigDownloadLog instance.

        Args:
            message (str): The log message.
            config_url (Optional[str]): The config URL.
            model_id (Optional[str]): The model ID.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if config_url:
            context["config_url"] = config_url
        if model_id:
            context["model_id"] = model_id
        super().__init__(message, context=context, **kwargs)


# --- Process Logs ---


class ProcessLog(LinkLog):
    """Base class for all process-related logs."""

    pass


class InferenceStartLog(ProcessLog):
    """Raised when model inference fails to start."""

    def __init__(
        self,
        message: str = "Failed to start model inference",
        model_name: Optional[str] = None,
        model_id: Optional[str] = None,
        **kwargs,
    ):
        """Initialise an InferenceStartLog instance.

        Args:
            message (str): The log message.
            model_name (Optional[str]): The model name.
            model_id (Optional[str]): The model ID.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if model_name:
            context["model_name"] = model_name
        if model_id:
            context["model_id"] = model_id
        super().__init__(message, context=context, **kwargs)


class InferenceAlreadyRunningLog(ProcessLog):
    """Raised when trying to start inference for a model that's already running."""

    def __init__(
        self,
        message: str = "Model inference is already running",
        model_name: Optional[str] = None,
        pid: Optional[int] = None,
        **kwargs,
    ):
        """Initialise an InferenceAlreadyRunningLog instance.

        Args:
            message (str): The log message.
            model_name (Optional[str]): The model name.
            pid (Optional[int]): The process ID.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if model_name:
            context["model_name"] = model_name
        if pid:
            context["pid"] = pid
        super().__init__(message, context=context, **kwargs)


class InferenceStopLog(ProcessLog):
    """Raised when model inference fails to stop."""

    def __init__(
        self,
        message: str = "Failed to stop model inference",
        model_name: Optional[str] = None,
        pid: Optional[int] = None,
        **kwargs,
    ):
        """Initialise an InferenceStopLog instance.

        Args:
            message (str): The log message.
            model_name (Optional[str]): The model name.
            pid (Optional[int]): The process ID.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if model_name:
            context["model_name"] = model_name
        if pid:
            context["pid"] = pid
        super().__init__(message, context=context, **kwargs)


class InferenceNotRunningLog(ProcessLog):
    """Raised when trying to stop inference that's not running."""

    def __init__(self, message: str = "No active inference process found", model_name: Optional[str] = None, **kwargs):
        """Initialise an InferenceNotRunningLog instance.

        Args:
            message (str): The log message.
            model_name (Optional[str]): The model name.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if model_name:
            context["model_name"] = model_name
        super().__init__(message, context=context, **kwargs)


class ModelNotFoundLog(ProcessLog):
    """Raised when the model file is not found on disk."""

    def __init__(
        self,
        message: str = "Model file not found",
        model_name: Optional[str] = None,
        model_path: Optional[str] = None,
        **kwargs,
    ):
        """Initialise a ModelNotFoundLog instance.

        Args:
            message (str): The log message.
            model_name (Optional[str]): The model name.
            model_path (Optional[str]): The model path.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if model_name:
            context["model_name"] = model_name
        if model_path:
            context["model_path"] = model_path
        super().__init__(message, context=context, **kwargs)


class CommandTimeoutLog(ProcessLog):
    """Raised when a shell command times out."""

    def __init__(
        self,
        message: str = "Command execution timed out",
        command: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        **kwargs,
    ):
        """Initialise a CommandTimeoutLog instance.

        Args:
            message (str): The log message.
            command (Optional[str]): The command.
            timeout_seconds (Optional[int]): The timeout seconds.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if command:
            context["command"] = command[:100]
        if timeout_seconds:
            context["timeout_seconds"] = timeout_seconds
        super().__init__(message, context=context, **kwargs)


class CommandExecutionLog(ProcessLog):
    """Raised when a shell command fails to execute."""

    def __init__(
        self,
        message: str = "Command execution failed",
        command: Optional[str] = None,
        return_code: Optional[int] = None,
        stderr: Optional[str] = None,
        **kwargs,
    ):
        """Initialise a CommandExecutionLog instance.

        Args:
            message (str): The log message.
            command (Optional[str]): The command.
            return_code (Optional[int]): The return code.
            stderr (Optional[str]): The standard error.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if command:
            context["command"] = command[:100]
        if return_code is not None:
            context["return_code"] = return_code
        if stderr:
            context["stderr"] = stderr[:500]
        super().__init__(message, context=context, **kwargs)


class UnknownCommandLog(ProcessLog):
    """Raised when an unknown command type is received."""

    def __init__(
        self,
        message: str = "Unknown command type",
        command_type: Optional[str] = None,
        command_id: Optional[str] = None,
        **kwargs,
    ):
        """Initialise an UnknownCommandLog instance.

        Args:
            message (str): The log message.
            command_type (Optional[str]): The command type.
            command_id (Optional[str]): The command ID.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if command_type:
            context["command_type"] = command_type
        if command_id:
            context["command_id"] = command_id
        super().__init__(message, context=context, **kwargs)


# --- System Logs ---


class SystemLog(LinkLog):
    """Base class for all system-related logs."""

    pass


class MetricsCollectionLog(SystemLog):
    """Raised when system metrics collection fails."""

    def __init__(self, message: str = "Failed to collect system metrics", metric_type: Optional[str] = None, **kwargs):
        """Initialise a MetricsCollectionLog instance.

        Args:
            message (str): The log message.
            metric_type (Optional[str]): The metric type.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if metric_type:
            context["metric_type"] = metric_type
        super().__init__(message, context=context, **kwargs)


class HardwareAccessLog(SystemLog):
    """Raised when hardware access fails (e.g., camera, microphone)."""

    def __init__(
        self,
        message: str = "Failed to access hardware",
        hardware_type: Optional[str] = None,
        device_index: Optional[int] = None,
        **kwargs,
    ):
        """Initialise a HardwareAccessLog instance.

        Args:
            message (str): The log message.
            hardware_type (Optional[str]): The hardware type.
            device_index (Optional[int]): The device index.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if hardware_type:
            context["hardware_type"] = hardware_type
        if device_index is not None:
            context["device_index"] = device_index
        super().__init__(message, context=context, **kwargs)


class StorageLog(SystemLog):
    """Raised when there's a storage-related issue."""

    def __init__(
        self,
        message: str = "Storage operation failed",
        path: Optional[str] = None,
        operation: Optional[str] = None,
        **kwargs,
    ):
        """Initialise a StorageLog instance.

        Args:
            message (str): The log message.
            path (Optional[str]): The path.
            operation (Optional[str]): The operation.
            **kwargs: Additional keyword arguments.
        """
        context = kwargs.pop("context", {})
        if path:
            context["path"] = path
        if operation:
            context["operation"] = operation
        super().__init__(message, context=context, **kwargs)
