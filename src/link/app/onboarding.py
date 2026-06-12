# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Device registration and activation against the Loc.ai control plane."""

import getpass
import json
import logging
import platform
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

# RFC 8628 grant_type.
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# expires_in is 600s with a 5s interval (~120 polls); cap at 240 to leave room for slow_down.
_DEVICE_POLL_MAX_ITERATIONS = 240

# Non-secret marker written after successful fleet enrollment. Lets `run` distinguish
# "wiped fleet device" (fail loudly) from a fresh interactive install (factory defaults ok).
FLEET_MARKER_PATH = Path("configs") / ".fleet_device"

# Enroll retry policy: transient failures (429, 5xx, network) use jittered backoff.
# Permanent failures (bad key, cap full, 4xx) are not retried.
_ENROLL_MAX_ATTEMPTS = 8
_ENROLL_BACKOFF_BASE_SECONDS = 2.0
_ENROLL_BACKOFF_CAP_SECONDS = 60.0
# Cap on Retry-After to prevent a hostile/absurd value from stalling the agent.
_RETRY_AFTER_HONOR_CAP_SECONDS = 300.0


class UseDeviceFlowError(Exception):
    """HTTP 409 use_device_flow — account uses SSO, no password set."""


def login_and_get_token(email: str, password: str, api_url: str) -> str:
    """Authenticates with the platform and returns a JWT access token.

    Args:
        email (str): The user's platform email address.
        password (str): The user's platform password.
        api_url (str): The API base URL.

    Returns:
        str: The JWT access token.

    Raises:
        UseDeviceFlowError: If the backend returns HTTP 409 `use_device_flow`
            (account has no password — caller should run the device flow).
        RuntimeError: If authentication fails for any other reason.
    """
    logger.info("Authenticating with the platform...")
    try:
        resp = requests.post(f"{api_url}/auth/login", data={"email": email, "password": password}, timeout=10)
        if resp.status_code == 200:
            token = resp.json().get("access_token")
            if token:
                logger.info("Authentication successful.")
                return token
            raise RuntimeError("Login succeeded but no access token returned.")
        if resp.status_code == 409:
            try:
                detail = resp.json().get("detail", {})
            except Exception:
                detail = {}
            if isinstance(detail, dict) and detail.get("error") == "use_device_flow":
                raise UseDeviceFlowError(detail.get("message", "Account requires device authorization."))
        detail = ""
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Authentication failed ({resp.status_code}): {detail}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error during authentication: {e}") from e


def _device_flow(api_url: str, client_metadata: dict[str, Any] | None = None) -> str:
    """Drives the OAuth 2.0 Device Authorization Grant (RFC 8628) from the CLI.

    Prints a verification URI/code to stderr, then polls until approved, denied, or expired.
    Used as a fallback for SSO-only users who cannot complete the password flow.
    """
    payload: dict[str, Any] = {}
    if client_metadata:
        payload["client_metadata"] = client_metadata

    try:
        resp = requests.post(f"{api_url}/auth/device/code", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to initiate device authorization: {e}") from e

    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = data["verification_uri"]
    verification_uri_complete = data.get("verification_uri_complete", verification_uri)
    interval = int(data.get("interval", 5))

    # Print to stderr so the banner stays visible regardless of stdout redirection.
    print(
        "\n"
        "To authenticate, open this URL on any device:\n"
        f"    {verification_uri}\n"
        "And enter this code:\n"
        f"    {user_code}\n"
        "\n"
        "Or open this URL directly:\n"
        f"    {verification_uri_complete}\n"
        "\n"
        "Waiting for approval...",
        file=sys.stderr,
        flush=True,
    )

    token_url = f"{api_url}/auth/device/token"
    token_body = {"device_code": device_code, "grant_type": DEVICE_GRANT_TYPE}

    for _ in range(_DEVICE_POLL_MAX_ITERATIONS):
        time.sleep(interval)
        try:
            resp = requests.post(token_url, json=token_body, timeout=10)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Network error while polling for device authorization: {e}")
            continue

        if resp.status_code == 200:
            token = resp.json().get("access_token")
            if not token:
                raise RuntimeError("Device approval succeeded but no access token returned.")
            print("Device approved.", file=sys.stderr, flush=True)
            return token

        try:
            detail = resp.json().get("detail", {})
            error = detail.get("error") if isinstance(detail, dict) else None
        except Exception:
            error = None

        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise RuntimeError("Device authorization denied.")
        if error == "expired_token":
            raise RuntimeError("Device authorization expired — please re-run.")

        raise RuntimeError(f"Device authorization failed ({resp.status_code}): {resp.text}")

    raise RuntimeError("Device authorization timed out.")


def _resolve_token(
    email: str | None,
    password: str | None,
    token: str | None,
    api_url: str,
    client_metadata: dict[str, Any] | None = None,
) -> str:
    """Resolves a JWT token from the provided credentials.

    Args:
        email (str | None): The user's email address.
        password (str | None): The user's password (prompted if None and email is provided).
        token (str | None): A pre-obtained JWT token.
        api_url (str): The API base URL.
        client_metadata (dict | None): Optional metadata surfaced to the user
            on the approval page if the flow falls through to device auth.

    Returns:
        str: A valid JWT token.

    Raises:
        ValueError: If neither token nor email is provided.
        RuntimeError: If authentication fails.
    """
    if token:
        return token
    if email:
        if password is None:
            password = getpass.getpass("Enter platform password (leave blank for SSO accounts): ")
        if not password:
            # Empty password: skip /auth/login (backend rejects it with 422 before the SSO check).
            print(
                "No password provided; using device authorization flow.",
                file=sys.stderr,
                flush=True,
            )
            return _device_flow(api_url, client_metadata)
        try:
            return login_and_get_token(email, password, api_url)
        except UseDeviceFlowError:
            # SSO-only user — let them know why we're switching flows so they
            # don't read the banner as "wrong password, try again".
            print(
                "This account uses single sign-on; falling back to device authorization.",
                file=sys.stderr,
                flush=True,
            )
            return _device_flow(api_url, client_metadata)
    raise ValueError("Provide either --token or --email to authenticate.")


def register_device(
    name: str,
    reg_key: str,
    api_url: str,
    email: str | None = None,
    password: str | None = None,
    token: str | None = None,
) -> AgentConfig:
    """Exchanges a Registration Key + Name for a BRAND NEW Device ID and API Key.

    Args:
        name (str): The device name.
        reg_key (str): The registration key.
        api_url (str): The API base URL.
        email (str | None): The user's email (used to obtain a token if token is not provided).
        password (str | None): The user's password (prompted if None).
        token (str | None): A pre-obtained JWT token (alternative to email/password).

    Returns:
        AgentConfig: The initial AgentConfig.
    """
    logger.info(f"Registering new device: {name}")

    client_metadata = {
        "device_name": name,
        "os": platform.system(),
        "hostname": platform.node(),
    }
    auth_token = _resolve_token(email, password, token, api_url, client_metadata=client_metadata)

    _agent_ver = resolve_agent_version()
    _metadata: dict[str, Any] = {"os": platform.system(), "arch": platform.machine()}
    if _agent_ver:
        _metadata["agent_version"] = _agent_ver
    payload = {
        "registration_key": reg_key,
        "name": name,
        "device_type": "other",
        "metadata": _metadata,
    }
    headers = {"Authorization": f"Bearer {auth_token}"}

    try:
        resp = requests.post(f"{api_url}/devices/register-with-key", json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        return _resolve_agent_config(
            server_config=data.get("config"),
            device_id=data["device_id"],
            device_name=name,
            api_key=data["api_key"],
            api_url=api_url,
        )

    except Exception as e:
        logger.critical(f"Registration failed: {e}")
        raise


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


def _enroll_backoff_seconds(attempt: int) -> float:
    """Full-jitter exponential backoff: uniform random in [0, min(cap, base*2^(n-1))]."""
    ceiling = min(
        _ENROLL_BACKOFF_CAP_SECONDS,
        _ENROLL_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
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


def _enroll_error_detail(resp) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)
    except Exception:
        return (getattr(resp, "text", "") or "")[:200]


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

    data: dict[str, Any] | None = None
    for attempt in range(1, _ENROLL_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                f"{api_url}/devices/enroll",
                json=payload,
                headers=headers,
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            if attempt >= _ENROLL_MAX_ATTEMPTS:
                raise RuntimeError(f"Fleet enrollment failed after {attempt} attempts (network error): {exc}") from exc
            delay = _enroll_backoff_seconds(attempt)
            logger.warning(
                "Enroll attempt %d/%d failed (network error: %s); retrying in %.1fs",
                attempt,
                _ENROLL_MAX_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)
            continue

        status_code = resp.status_code

        if status_code == 200:
            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError(f"Enrollment response was not valid JSON: {exc}") from exc
            break

        # Transient: 429 (rate-limited) and 5xx.
        if status_code == 429 or 500 <= status_code < 600:
            if attempt >= _ENROLL_MAX_ATTEMPTS:
                raise RuntimeError(f"Fleet enrollment failed after {attempt} attempts (HTTP {status_code}).")
            delay = _retry_after_seconds(resp)
            if delay is None:
                delay = _enroll_backoff_seconds(attempt)
            logger.warning(
                "Enroll attempt %d/%d got HTTP %d; retrying in %.1fs",
                attempt,
                _ENROLL_MAX_ATTEMPTS,
                status_code,
                delay,
            )
            time.sleep(delay)
            continue

        # Permanent: 401/403 (bad/expired key), 409 (cap full), 422, other 4xx.
        raise RuntimeError(f"Fleet enrollment rejected (HTTP {status_code}): {_enroll_error_detail(resp)}")

    if data is None:
        raise RuntimeError("Fleet enrollment failed: no response received.")

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


def activate_device(device_id: str, reg_key: str, api_url: str) -> AgentConfig:
    """Exchanges a Registration Key + Existing Device ID for a NEW API Key.

    Args:
        device_id (str): The existing device ID.
        reg_key (str): The registration key.
        api_url (str): The API base URL.

    Returns:
        AgentConfig: The recovered AgentConfig.
    """
    logger.info(f"Activating existing device: {device_id}")

    payload = {"device_id": device_id, "registration_key": reg_key, "device_type": "edge_device"}

    try:
        resp = requests.post(f"{api_url}/agent/activate-with-key", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        return _resolve_agent_config(
            server_config=data.get("config"),
            device_id=device_id,
            device_name="recovered-device",
            api_key=data["api_key"],
            api_url=api_url,
        )

    except Exception as e:
        logger.critical(f"Activation failed: {e}")
        raise


def _resolve_agent_config(
    server_config: dict[str, Any] | None,
    device_id: str,
    device_name: str,
    api_key: str,
    api_url: str,
) -> AgentConfig:
    """Select and resolve the AgentConfig for this device.

    Preference order:
      1. A backend-provided config (delivered in the registration response).
      2. The built-in hardcoded defaults from `_bootstrap_config`.

    If a backend config is present but cannot be resolved (wrong schema version,
    fails Pydantic validation, etc.), this logs a loud error and falls back to
    defaults so registration still succeeds — operators can investigate via the
    backend admin surface without bricking the device.

    Args:
        server_config: Raw `config` dict from the backend response, or None.
        device_id: Device identifier from the registration response.
        device_name: Device name (from the client-side registration request).
        api_key: API key from the registration response.
        api_url: Base URL the agent was started with.

    Returns:
        A validated `AgentConfig` ready to hand to the state manager.
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

    Args:
        raw: The raw config dict from the backend response.
        device_id: Device identifier from the registration response.
        device_name: Device name (from the client-side registration request).
        api_key: API key from the registration response.
        api_url: Base URL the agent was started with.

    Returns:
        A validated `AgentConfig`.

    Raises:
        ValueError: If the schema version is unknown or validation fails.
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
    """Helper to generate the standard configuration object.

    Args:
        device_id (str): The device ID.
        device_name (str): The device name.
        api_key (str): The API key.
        api_url (str): The API URL.

    Returns:
        AgentConfig: The generated agent configuration.
    """
    logger.info(f"Onboarding successful. Assigned ID: {device_id}")

    return AgentConfig(
        version=2.1,
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
