# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Device registration and activation against the Loc.ai control plane."""

import getpass
import json
import logging
import platform
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from link.config.models import (
    SCHEMA_VERSION,
    AgentConfig,
    GenericConfig,
    IdentityConfig,
    LoggingConfig,
    PipelineConfig,
    ReportingConfig,
    TransportConfig,
)
from link.config.templating import resolve_templates
from link.utils.version import resolve_agent_version

logger = logging.getLogger(__name__)

# Non-secret marker written after successful fleet enrollment. Lets `run` distinguish
# "wiped fleet device" (fail loudly) from a fresh interactive install (factory defaults ok).
FLEET_MARKER_PATH = Path("configs") / ".fleet_device"

# Onboarding retry policy: retries transient control-plane call failures (HTTP 429/5xx, network)
# with jittered backoff; honors Retry-After. Permanent client errors are not retried.
_RETRY_MAX_ATTEMPTS = 8
_RETRY_BACKOFF_BASE_SECONDS = 2.0
_RETRY_BACKOFF_CAP_SECONDS = 60.0
# Cap on Retry-After to prevent a hostile/absurd value from stalling the agent.
_RETRY_AFTER_HONOR_CAP_SECONDS = 300.0


def _resolve_fleet_key(value: str) -> str:
    """Resolves the --fleet-key arg to the actual key; accepts the key itself or file:<path>."""
    if not value.startswith("file:"):
        return value
    key_path = Path(value[len("file:") :])
    try:
        key = key_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RuntimeError(f"Could not read fleet key file {key_path}: {e}") from e
    if not key:
        raise RuntimeError(f"Fleet key file {key_path} is empty.")
    return key


def _write_fleet_marker(device_id) -> None:
    """Persists the fleet-device marker (best-effort, contains no secrets)."""
    try:
        FLEET_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        FLEET_MARKER_PATH.write_text(
            json.dumps(
                {
                    "enrolled_at": datetime.now(timezone.utc).isoformat(),
                    "device_id": str(device_id),
                }
            ),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"Could not write fleet marker {FLEET_MARKER_PATH}: {e}")


def _retry_backoff_seconds(attempt: int) -> float:
    """Full-jitter exponential backoff: uniform random in [0, min(cap, base*2^(n-1))]."""
    ceiling = min(
        _RETRY_BACKOFF_CAP_SECONDS,
        _RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
    )
    return random.uniform(0, ceiling)


def _retry_after_seconds(resp) -> float | None:
    """Returns the Retry-After header in seconds if present and parseable, else None. Capped."""
    raw = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
    if not raw:
        return None
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        return None
    if secs < 0:
        return None
    return min(secs, _RETRY_AFTER_HONOR_CAP_SECONDS)


def _response_error_detail(resp) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)
    except Exception:
        return (getattr(resp, "text", "") or "")[:200]


def _request_with_retry(
    do_request: Callable[[], requests.Response],
    op_name: str,
    permanent_handler: Callable[[requests.Response], None] | None = None,
) -> requests.Response:
    """Issue control-plane onboarding requests, retrying transient failures
    (network, 429, 5xx) with jittered backoff and Retry-After. Raises on retry
    exhaustion or a permanent failure. ``permanent_handler`` runs on a permanent
    failure before the generic rejection.
    """
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = do_request()
        except requests.exceptions.RequestException as exc:
            if attempt >= _RETRY_MAX_ATTEMPTS:
                raise RuntimeError(f"{op_name} failed after {attempt} attempts (network error): {exc}") from exc
            delay = _retry_backoff_seconds(attempt)
            logger.warning(
                "%s attempt %d/%d failed (network error: %s); retrying in %.1fs",
                op_name,
                attempt,
                _RETRY_MAX_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
            continue

        status_code = resp.status_code

        # Transient: 429 (rate-limited) and 5xx.
        if status_code == 429 or 500 <= status_code < 600:
            if attempt >= _RETRY_MAX_ATTEMPTS:
                raise RuntimeError(f"{op_name} failed after {attempt} attempts (HTTP {status_code}).")
            delay = _retry_after_seconds(resp)
            if delay is None:
                delay = _retry_backoff_seconds(attempt)
            logger.warning(
                "%s attempt %d/%d got HTTP %d; retrying in %.1fs",
                op_name,
                attempt,
                _RETRY_MAX_ATTEMPTS,
                status_code,
                delay,
            )
            time.sleep(delay)
            continue

        # Permanent: any other client error (4xx).
        if status_code >= 400:
            if permanent_handler is not None:
                permanent_handler(resp)
            raise RuntimeError(f"{op_name} rejected (HTTP {status_code}): {_response_error_detail(resp)}")

        return resp

    # Defensive: the loop returns, retries, or raises on the final attempt.
    raise RuntimeError(f"{op_name} failed: no response received.")


def enroll_device(fleet_key: str, api_url: str) -> AgentConfig:
    """Headless fleet enrollment via a reusable org-scoped fleet key.

    Resolves file: key references, posts to /devices/enroll with retry/backoff,
    and writes the fleet marker on success. No user credentials required.
    """
    from link.infra.utils import get_machine_id_hash

    fleet_key = _resolve_fleet_key(fleet_key)

    logger.info("Starting headless fleet enrollment...")

    machine_id_hash = get_machine_id_hash()

    _agent_ver = resolve_agent_version()
    payload: dict[str, Any] = {
        "machine_id_hash": machine_id_hash,
        "os": platform.system(),
        "arch": platform.machine(),
        "hostname": platform.node(),
    }
    if _agent_ver:
        payload["agent_version"] = _agent_ver

    headers = {"Authorization": f"Bearer {fleet_key}"}

    resp = _request_with_retry(
        lambda: requests.post(f"{api_url}/devices/enroll", json=payload, headers=headers, timeout=15),
        op_name="Fleet enrollment",
    )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Enrollment response was not valid JSON: {exc}") from exc

    device_id = data.get("device_id")
    api_key = data.get("api_key")
    if not device_id or not api_key:
        raise RuntimeError(f"Enrollment response missing device_id or api_key: {data}")

    device_name = data.get("device_name") or device_id
    logger.info(f"Fleet enrollment successful. Device: {device_name} ({device_id})")

    _write_fleet_marker(device_id)

    return _resolve_agent_config(
        server_config=data.get("config"),
        device_id=device_id,
        device_name=device_name,
        api_key=api_key,
        api_url=api_url,
    )


def _default_device_name() -> str:
    """`<os-user>@<hostname>`, the contract's default headless device name."""
    host = platform.node()
    try:
        return f"{getpass.getuser()}@{host}"
    except Exception:  # noqa: BLE001 - getuser can raise if no user db / env
        return host


def register_with_key(reg_key: str, api_url: str) -> AgentConfig:
    """Headless single-device registration via a registration key from Control.

    Machine-bound like enroll_device: the registration key is the credential (sent
    as the bearer, no user JWT), the machine is identified by its id hash + hostname,
    and Control assigns the device_id. Re-redemption from the same machine is an
    idempotent retry that rotates the api_key, so installer re-runs are safe.
    """
    from link.infra.utils import get_machine_id_hash

    logger.info("Registering device with registration key...")

    payload: dict[str, Any] = {
        "machine_id_hash": get_machine_id_hash(),
        "device_name": _default_device_name(),
        "os": platform.system(),
        "arch": platform.machine(),
        "hostname": platform.node(),
        "device_type": "other",
    }
    _agent_ver = resolve_agent_version()
    if _agent_ver:
        payload["agent_version"] = _agent_ver
    headers = {"Authorization": f"Bearer {reg_key}"}

    resp = _request_with_retry(
        lambda: requests.post(
            f"{api_url}/devices/headless/register-with-reg-key", json=payload, headers=headers, timeout=15
        ),
        op_name="Device registration",
    )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"Registration response was not valid JSON: {exc}") from exc

    device_id = data.get("device_id")
    api_key = data.get("api_key")
    if not device_id or not api_key:
        raise RuntimeError(f"Registration response missing device_id or api_key: {data}")

    # The server name is final (it may suffix on collision); persist what it returns.
    device_name = data.get("device_name") or platform.node() or device_id
    logger.info(f"Registration successful. Device: {device_name} ({device_id})")

    return _resolve_agent_config(
        server_config=data.get("config"),
        device_id=device_id,
        device_name=device_name,
        api_key=api_key,
        api_url=api_url,
    )


def _resolve_agent_config(
    server_config: dict[str, Any] | None,
    device_id: str,
    device_name: str,
    api_key: str,
    api_url: str,
) -> AgentConfig:
    """Select and resolve the AgentConfig for this device.

    Prefers a backend-provided config, else the built-in defaults from
    `_bootstrap_config`. If a backend config is present but unresolvable (wrong
    schema, failed validation), logs a loud error and falls back to defaults so
    registration still succeeds.
    """
    if server_config is None:
        logger.info("No config from backend — using built-in defaults.")
        return _bootstrap_config(device_id, device_name, api_key, api_url)

    try:
        return _apply_server_config(server_config, device_id, device_name, api_key, api_url)
    except Exception as e:
        logger.critical(
            f"Backend config rejected ({e}). Falling back to built-in defaults — "
            "check the template on the control plane."
        )
        return _bootstrap_config(device_id, device_name, api_key, api_url)


def _apply_server_config(
    raw: dict[str, Any], device_id: str, device_name: str, api_key: str, api_url: str
) -> AgentConfig:
    """Resolve templates, inject identity, and validate a backend config.
    Raises ValueError if the schema version is unknown or validation fails.
    """

    raw_version = raw.get("version")
    if raw_version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported config schema version {raw_version!r} — this agent requires {SCHEMA_VERSION}")

    context = {
        "identity": {
            "device_id": device_id,
            "device_name": device_name,
            "api_key": api_key,
            "api_url": api_url,
        },
        "api_url": api_url,
    }
    resolved = resolve_templates(raw, context)

    resolved["identity"] = {
        "device_id": device_id,
        "device_name": device_name,
        "api_key": api_key,
        "api_url": api_url,
    }

    config = AgentConfig(**resolved)
    logger.info(
        f"Applied backend config: {len(config.pipelines)} pipeline(s), "
        f"transport={config.transport.type if config.transport else 'http'}, "
        f"{len(config.logging.handlers)} logging handler(s), "
        f"{len(config.reporting.handlers)} reporting handler(s)."
    )
    return config


def _bootstrap_config(device_id: str, device_name: str, api_key: str, api_url: str) -> AgentConfig:
    """Generate the standard hardcoded configuration object."""
    logger.info(f"Onboarding successful. Assigned ID: {device_id}")

    return AgentConfig(
        version=SCHEMA_VERSION,
        identity=IdentityConfig(device_id=device_id, device_name=device_name, api_key=api_key, api_url=api_url),
        # 1. Transport
        transport=TransportConfig(type="http"),
        # 2. Logging
        logging=LoggingConfig(
            level="INFO",
            handlers=[
                GenericConfig(type="console"),
                GenericConfig(
                    type="http",
                    args={
                        # Standard Logs (POST)
                        "url": f"{api_url}/agent/{device_id}/logs",
                    },
                ),
            ],
        ),
        # 3. Reporting (New Structure)
        reporting=ReportingConfig(
            interval=30,
            handlers=[
                GenericConfig(type="console"),
                GenericConfig(
                    type="http",
                    args={
                        # Lifecycle Status (PUT)
                        "lifecycle_status": f"{api_url}/agent/{device_id}/status",
                        # Command Status (POST) -> outputs .../commands/{cid}/status
                        "command_status": f"{api_url}/agent/{device_id}/commands/{{cid}}/status",
                        # Model Status (POST) -> outputs .../models/{mid}/status
                        "model_status": f"{api_url}/agent/{device_id}/models/{{mid}}/status",
                    },
                ),
            ],
        ),
        # 4. Pipelines
        pipelines=[
            PipelineConfig(
                id="command_center",
                active=True,
                source=GenericConfig(
                    type="http_poll",
                    args={
                        "url": f"{api_url}/agent/{device_id}/commands",
                        "api_key": api_key,
                        "interval": 10,
                    },
                ),
                sink=GenericConfig(type="command"),
            ),
            PipelineConfig(
                id="system_metrics",
                active=True,
                source=GenericConfig(
                    type="system_monitor",
                    args={
                        "interval": 5,
                        "metrics": ["cpu_usage", "ram_usage", "temperature_celsius", "storage_available_gb"],
                    },
                ),
                sink=GenericConfig(
                    type="http_post",
                    args={
                        "url": f"{api_url}/agent/{device_id}/metrics",
                        "api_key": api_key,
                        "timeout": 30,
                    },
                ),
            ),
        ],
    )
