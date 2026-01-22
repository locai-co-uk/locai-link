# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import functools
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, Optional, TypeVar

import requests

from .exceptions import LinkLog

T = TypeVar("T")


# --- Result Class (for wrap() return type) ---


class Result:
    """Simple result wrapper for operations that might fail."""

    def __init__(
        self,
        success: bool,
        message: str = "",
        data: Any = None,
        error: Optional[Exception] = None,
        status: str = "completed",
    ):
        """Initialise a Result instance.

        Args:
            success (bool): Whether the operation was successful.
            message (str): The log message.
            data (Any): The data returned by the operation.
            error (Optional[Exception]): The error that occurred.
            status (str): The status of the operation.
        """
        self.success = success
        self.message = message
        self.data = data
        self.error = error
        self.status = status

    @property
    def failed(self) -> bool:
        """Return whether the operation failed."""
        return not self.success

    def as_tuple(self) -> tuple:
        """Return the result as a tuple."""
        return (self.status, self.message)

    @classmethod
    def ok(cls, message: str = "Success", data: Any = None) -> "Result":
        """Return a successful result."""
        return cls(success=True, message=message, data=data, status="completed")

    @classmethod
    def fail(cls, message: str, error: Optional[Exception] = None) -> "Result":
        """Return a failed result."""
        return cls(success=False, message=message, error=error, status="failed")


# --- LogClient - Singleton for Backend Reporting ---


class LogClient:
    """Singleton client for sending logs to the backend.

    All logs (INFO, WARNING, ERROR, CRITICAL) are sent to the backend.

    Attributes:
        _instance (Optional[LogClient]): The singleton instance.
        device_id (Optional[str]): The device ID.
        api_key (Optional[str]): The API key for authentication.
        api_url (Optional[str]): The base API URL (e.g., "https://api.locai.co.uk/api/v1").
        agent_version (Optional[str]): The version of the agent software.
    """

    _instance: Optional["LogClient"] = None

    def __init__(self):
        """Initialise a LogClient instance."""
        self.device_id: Optional[str] = None
        self.api_key: Optional[str] = None
        self.api_url: Optional[str] = None

    @classmethod
    def get(cls) -> "LogClient":
        """Get the singleton instance.

        Returns:
            LogClient: The singleton instance.
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def configure(self, device_id: str, api_key: str, api_url: str, agent_version: str = "unknown"):
        """Configure the log client with device credentials.

        Args:
            device_id: The device ID
            api_key: The API key for authentication
            api_url: The base API URL (e.g., "https://api.locai.co.uk/api/v1")
            agent_version: The version of the agent software
        """
        self.device_id = device_id
        self.api_key = api_key
        self.api_url = api_url
        self.agent_version = agent_version

    def report(
        self,
        message: str,
        severity: str = "error",
        category: str = "other",
        action: Optional[str] = None,
        stage: Optional[str] = None,
        progress: Optional[int] = None,
        target: Optional[str] = None,
        state_before: Optional[Dict[str, Any]] = None,
        state_after: Optional[Dict[str, Any]] = None,
        error_type: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        stack_trace: Optional[str] = None,
    ) -> bool:
        """Send a log to the backend. Called for ALL severity levels.

        Args:
            message: The log message
            severity: One of "info", "warning", "error", "critical"
            category: Event category (e.g. deployment, execution, health)
            action: Action performed
            stage: Stage of the action
            progress: Progress percentage (0-100)
            target: Target of the action
            state_before: State before action
            state_after: State after action
            error_type: Optional exception type name
            context: Optional context dictionary
            stack_trace: Optional stack trace string

        Returns:
            True if successfully sent, False otherwise
        """
        # Always print locally
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] [{severity.upper()}] {message}")

        # Check if configured
        if not all([self.device_id, self.api_key, self.api_url]):
            return False

        # Extract special fields from context if provided there
        if context:
            if category == "other" and "category" in context:
                category = context.pop("category")
            if action is None and "action" in context:
                action = context.pop("action")
            if stage is None and "stage" in context:
                stage = context.pop("stage")
            if progress is None and "progress" in context:
                progress = context.pop("progress")
            if target is None and "target" in context:
                target = context.pop("target")
            if state_before is None and "state_before" in context:
                state_before = context.pop("state_before")
            if state_after is None and "state_after" in context:
                state_after = context.pop("state_after")

        try:
            payload = {
                "message": message,
                "severity": severity,
                "category": category,
                "action": action,
                "stage": stage,
                "progress": progress,
                "target": target,
                "state_before": state_before,
                "state_after": state_after,
                "device_id": self.device_id,
                "agent_version": self.agent_version,
                "error_type": error_type,
                "context": context or {},
                "stack_trace": stack_trace,
            }

            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

            url = f"{self.api_url}/logs/device/{self.device_id}/create"

            # Short timeout to not block operations
            response = requests.post(url, json=payload, headers=headers, timeout=5)

            return response.status_code in (200, 201)

        except Exception as e:
            # Silently fail to avoid infinite recursion
            print(f"[LOG SEND FAILED] {e}")
            return False

    def progress(self, message: str, progress: int, stage: str, category: str = "deployment", **context) -> None:
        """Helper to send progress updates."""
        self.report(
            message=message, severity="info", category=category, stage=stage, progress=progress, context=context
        )


# --- Helper Functions ---


def progress(message: str, progress: int, stage: str, category: str = "deployment", **context) -> None:
    """Log progress event."""
    LogClient.get().progress(message, progress, stage, category, **context)


def fail(message: str, **context) -> tuple:
    """Log an ERROR and return a failure tuple.

    Args:
        message: The error message
        **context: Additional context key-value pairs

    Returns:
        Tuple of ("failed", message)
    """
    LogClient.get().report(
        message=message, severity="error", context=context if context else None, stack_trace=traceback.format_exc()
    )
    return ("failed", message)


def ok(message: str = "Operation completed successfully", **context) -> tuple:
    """Log an INFO and return a success tuple.

    Args:
        message: The success message
        **context: Additional context key-value pairs

    Returns:
        Tuple of ("completed", message)
    """
    LogClient.get().report(message=message, severity="info", context=context if context else None)
    return ("completed", message)


def warn(message: str, **context):
    """Log a WARNING.

    Args:
        message: The warning message
        **context: Additional context key-value pairs
    """
    LogClient.get().report(message=message, severity="warning", context=context if context else None)


def info(message: str, **context):
    """Log an INFO message.

    Args:
        message: The info message
        **context: Additional context key-value pairs
    """
    LogClient.get().report(message=message, severity="info", context=context if context else None)


def critical(message: str, **context):
    """Log a CRITICAL error.

    Args:
        message: The critical error message
        **context: Additional context key-value pairs
    """
    LogClient.get().report(
        message=message, severity="critical", context=context if context else None, stack_trace=traceback.format_exc()
    )


def raise_if(condition: bool, message: str, **kwargs):
    """Raise a LinkLog if condition is True.

    Args:
        condition: If True, raise an exception
        message: The error message
        **kwargs: Additional context
    """
    if condition:
        raise LinkLog(message, context=kwargs if kwargs else None)


# --- Retry Logic ---

# Retry configuration by exception type
RETRY_CONFIG = {
    # Network errors are retryable
    requests.exceptions.ConnectionError: {"max_retries": 3, "base_delay": 1.0},
    requests.exceptions.Timeout: {"max_retries": 3, "base_delay": 1.0},
    # Default for unknown errors
    Exception: {"max_retries": 0, "base_delay": 1.0},
}


def _get_retry_config(error: Exception) -> Dict[str, Any]:
    """Get retry configuration for an exception type."""
    for error_type, config in RETRY_CONFIG.items():
        if isinstance(error, error_type):
            return config
    return RETRY_CONFIG[Exception]


def wrap(
    operation: Callable[[], T],
    retries: int = 3,
    context: Optional[Dict[str, Any]] = None,
) -> Result:
    """Execute an operation with retry logic and error handling.

    Args:
        operation: Callable to execute
        retries: Maximum number of retry attempts
        context: Additional context for logging

    Returns:
        Result object with success/failure status and data
    """
    last_error = None

    for attempt in range(retries + 1):
        try:
            data = operation()
            return Result.ok(data=data)

        except Exception as e:
            last_error = e
            config = _get_retry_config(e)

            # Check if we should retry
            if attempt < retries and config["max_retries"] > 0:
                delay = config["base_delay"] * (2**attempt)  # Exponential backoff
                print(f"[RETRY] Attempt {attempt + 1}/{retries + 1} failed: {e}")
                print(f"[RETRY] Retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue

            # No more retries - log the error
            LogClient.get().report(
                message=str(e),
                severity="error",
                error_type=type(e).__name__,
                context=context,
                stack_trace=traceback.format_exc(),
            )

            return Result.fail(message=str(e), error=e)

    # Should not reach here, but handle edge case
    return Result.fail(message=str(last_error) if last_error else "Unknown error", error=last_error)


# --- Context Manager ---


class CatchContext:
    """Context holder for the catch() context manager."""

    def __init__(self):
        """Initialise the context."""
        self.failed = False
        self.success = True
        self.error: Optional[Exception] = None
        self.message: str = ""

    def as_tuple(self) -> tuple:
        """Return (status, message) tuple."""
        return ("failed" if self.failed else "completed", self.message)


@contextmanager
def catch(reraise: bool = True, context: Optional[Dict[str, Any]] = None, log: bool = True):
    """Context manager for handling exceptions in a code block.

    Args:
        reraise: If True, re-raise the exception after handling
        context: Additional context for logging
        log: If True, log the exception

    Usage:
        with catch(reraise=False) as ctx:
            risky_operation()

        if ctx.failed:
            return ctx.as_tuple()
    """
    ctx = CatchContext()

    try:
        yield ctx
    except Exception as e:
        ctx.failed = True
        ctx.success = False
        ctx.error = e
        ctx.message = str(e)

        if log:
            LogClient.get().report(
                message=str(e),
                severity="error",
                error_type=type(e).__name__,
                context=context,
                stack_trace=traceback.format_exc(),
            )

        if reraise:
            raise


# --- Decorator ---


def safe(
    _func: Optional[Callable] = None,
    *,
    retries: int = 0,
    context: Optional[Dict[str, Any]] = None,
):
    """Decorator that wraps a function with automatic error handling.

    Args:
        _func: The function to wrap
        retries: Maximum number of retry attempts
        context: Additional context for logging

    Usage:
        @safe
        def simple_func():
            ...

        @safe(retries=3)
        def download_model():
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> tuple:
            func_context = dict(context or {})
            func_context["function"] = func.__name__

            for attempt in range(retries + 1):
                try:
                    result = func(*args, **kwargs)

                    # If already a tuple (status, message), pass through
                    if isinstance(result, tuple) and len(result) == 2:
                        return result

                    return ("completed", "Success")

                except Exception as e:
                    if attempt < retries:
                        delay = 1.0 * (2**attempt)
                        print(f"[RETRY] {func.__name__} attempt {attempt + 1}/{retries + 1} failed: {e}")
                        time.sleep(delay)
                        continue

                    # Final attempt failed
                    LogClient.get().report(
                        message=str(e),
                        severity="error",
                        error_type=type(e).__name__,
                        context=func_context,
                        stack_trace=traceback.format_exc(),
                    )

                    return ("failed", str(e))

            return ("failed", "Unknown error")

        return wrapper

    if _func is not None:
        return decorator(_func)

    return decorator
