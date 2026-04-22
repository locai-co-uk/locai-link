# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Device registration and activation against the Loc.ai control plane."""

import getpass
import logging
import platform
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

logger = logging.getLogger(__name__)


def login_and_get_token(email: str, password: str, api_url: str) -> str:
    """Authenticates with the platform and returns a JWT access token.

    Args:
        email (str): The user's platform email address.
        password (str): The user's platform password.
        api_url (str): The API base URL.

    Returns:
        str: The JWT access token.

    Raises:
        RuntimeError: If authentication fails.
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
        else:
            detail = ""
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Authentication failed ({resp.status_code}): {detail}")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error during authentication: {e}") from e


def _resolve_token(email: str | None, password: str | None, token: str | None, api_url: str) -> str:
    """Resolves a JWT token from the provided credentials.

    Args:
        email (str | None): The user's email address.
        password (str | None): The user's password (prompted if None and email is provided).
        token (str | None): A pre-obtained JWT token.
        api_url (str): The API base URL.

    Returns:
        str: A valid JWT token.

    Raises:
        ValueError: If neither token nor email is provided.
    """
    if token:
        return token
    if email:
        password = password or getpass.getpass("Enter platform password: ")
        return login_and_get_token(email, password, api_url)
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

    auth_token = _resolve_token(email, password, token, api_url)

    payload = {
        "registration_key": reg_key,
        "name": name,
        "device_type": "other",
        "metadata": {"os": platform.system(), "arch": platform.machine()},
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
            device_name="recovered-device",  # Name is already on server
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
        # Loud, critical — the operator needs to see this. But don't brick
        # registration: fall back to known-good defaults.
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

    # Strict version check. Unknown versions could mean the backend is ahead of
    # the agent, in which case we can't safely interpret the config.
    raw_version = raw.get("version")
    if raw_version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported config schema version {raw_version!r} — this agent requires {SCHEMA_VERSION}")

    print(raw)

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

    # Always inject our known identity, regardless of what the template declared.
    # The backend shouldn't be overriding the client's view of its own identity.
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

    # Note: We resolve ${identity...} variables immediately here since we have them.
    # We use double curly braces {{cid}} to output literal {cid} for the runtime logger.

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
