# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import sys
from typing import Optional

import requests


def send_model_ready(
    *,
    base_url: str,
    device_id: str,
    api_key: str,
    model_id: str,
    model_name: Optional[str],
    mode: str,
    runner: Optional[str] = None,
    model_format: Optional[str] = None,
) -> bool:
    """Report that a model is ready to be used on a device."""
    if not base_url:
        return False

    try:
        url = f"{base_url}/agent/{device_id}/model-ready"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model_id": model_id,
            "model_name": model_name,
            "mode": mode,
            "runner": runner,
            "model_format": model_format,
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
        return resp.status_code == 200
    except Exception as e:
        print(f"[model_ready] Failed to report model_ready: {e}", file=sys.stderr)
        return False


def send_model_downloaded(
    *,
    base_url: str,
    device_id: str,
    api_key: str,
    model_id: str,
    model_name: Optional[str],
    model_format: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    download_duration_seconds: Optional[float] = None,
) -> bool:
    """Report that a model has been downloaded to a device."""
    if not base_url:
        return False

    try:
        url = f"{base_url}/agent/{device_id}/model-downloaded"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model_id": model_id,
            "model_name": model_name,
            "model_format": model_format,
            "file_size_bytes": file_size_bytes,
            "download_duration_seconds": download_duration_seconds,
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
        return resp.status_code == 200
    except Exception as e:
        print(f"[model_downloaded] Failed to report model_downloaded: {e}", file=sys.stderr)
        return False


def send_agent_error(
    *,
    base_url: str,
    device_id: str,
    api_key: str,
    error_type: str,
    model_id: str,
    error_message: str,
    raw_log_line: str,
) -> bool:
    """Report an agent-level error to the backend.

    Args:
        base_url: The base API URL.
        device_id: The device ID.
        api_key: The device API key.
        error_type: One of "serving", "inference", "deployment".
        model_id: The model involved.
        error_message: Human-readable error description.
        raw_log_line: Raw log output associated with the error.
    """
    if not base_url:
        return False

    try:
        url = f"{base_url}/agent/{device_id}/agent-error"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "error_type": error_type,
            "model_id": model_id,
            "error_message": error_message,
            "raw_log_line": raw_log_line,
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
        return resp.status_code == 200
    except Exception as e:
        print(f"[agent_error] Failed to report error: {e}", file=sys.stderr)
        return False
