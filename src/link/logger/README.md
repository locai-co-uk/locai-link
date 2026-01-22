# Link Logs Package

A streamlined logging system for the Device Agent that sends ALL logs to the backend.

## Quick Start

```python
from link.logger import link_logger, LogClient

# 1. Configure at startup (once)
LogClient.get().configure(
    device_id="your-device-id",
    api_key="your-api-key",
    api_url="https://api.locai.co.uk/api/v1"
)

# 2. Log messages (all are sent to backend)
link_logger.info("System initialised")           # INFO level
link_logger.warn("Low disk space")               # WARNING level
link_logger.fail("Model download failed")        # ERROR level (returns tuple)
link_logger.ok("Deployment complete")            # INFO level (returns tuple)
link_logger.critical("System crash")             # CRITICAL level
```

## Files

| File | Purpose |
|------|---------|
| `core.py` | LogClient, helper functions, decorators |
| `exceptions.py` | All exception/log classes |
| `__init__.py` | Exports |

## Functions Reference

### `fail(message, **context) -> tuple`
Log an ERROR and return `("failed", message)`.
```python
if not model_name:
    return link_logger.fail("model_name is required")
```

### `ok(message, **context) -> tuple`
Log an INFO and return `("completed", message)`.
```python
return link_logger.ok("Model deployed successfully")
```

### `warn(message, **context)`
Log a WARNING (no return value).
```python
link_logger.warn("Config file not found, using defaults")
```

### `info(message, **context)`
Log an INFO (no return value).
```python
link_logger.info("Starting inference", model_id="abc123")
```

### `critical(message, **context)`
Log a CRITICAL error (no return value).
```python
link_logger.critical("Database connection lost")
```

## Retry Wrapper

For operations that might fail transiently:

```python
result = link_logger.wrap(
    lambda: requests.get(download_url),
    retries=3
)

if result.failed:
    return result.as_tuple()  # ("failed", "error message")

response = result.data  # The actual response object
```

## Exception Handling

### Context Manager
```python
with link_logger.catch(reraise=False) as ctx:
    risky_operation()

if ctx.failed:
    return ctx.as_tuple()
```

### Decorator
```python
@link_logger.safe(retries=3)
def download_model():
    response = requests.get(url)
    response.raise_for_status()
    return response
```

## Custom Exceptions

Raise specific exception types for better categorization:

```python
from logs import ModelDownloadLog, ModelNotFoundLog

# Raise when download fails
raise ModelDownloadLog(
    message="Failed to download model",
    model_id="abc123",
    status_code=404
)

# Raise when model file not found
raise ModelNotFoundLog(
    message="Model file not found on disk",
    model_name="model.gguf",
    model_path="/path/to/models"
)
```

### Available Exception Classes

| Category | Classes |
|----------|---------|
| Network | `APIConnectionLog`, `APIResponseLog`, `APITimeoutLog`, `APIRedirectLog` |
| Auth | `InvalidTokenLog`, `InvalidAPIKeyLog`, `DeviceNotActivatedLog` |
| Config | `ConfigFileNotFoundLog`, `ConfigParseLog`, `MissingConfigLog` |
| Deploy | `ModelDownloadLog`, `ModelSaveLog`, `ConfigDownloadLog` |
| Process | `InferenceStartLog`, `InferenceStopLog`, `ModelNotFoundLog`, `CommandTimeoutLog` |
| System | `MetricsCollectionLog`, `HardwareAccessLog`, `StorageLog` |

## Backend API

All logs are sent to:
```
POST /logs/device/{device_id}/create
```

Payload:
```json
{
    "message": "Error message",
    "severity": "error",
    "device_id": "device-123",
    "error_type": "ModelDownloadLog",
    "context": {"model_id": "abc"},
    "stack_trace": "..."
}
```

## Severity Levels

| Level | When to Use |
|-------|-------------|
| `info` | Normal operations, success messages |
| `warning` | Non-critical issues, degraded functionality |
| `error` | Operation failures, recoverable errors |
| `critical` | System failures, unrecoverable errors |
