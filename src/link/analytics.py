# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import os
import sys
from typing import Optional

import requests


def send_model_ready(
    *,
    device_id: str,
    api_key: str,
    model_id: str,
    model_name: Optional[str],
    mode: str,
    runner: Optional[str] = None,
    model_format: Optional[str] = None,
) -> bool:
    base_url = os.environ.get("BASE_URL")
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
    device_id: str,
    api_key: str,
    model_id: str,
    model_name: Optional[str],
    model_format: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    download_duration_seconds: Optional[float] = None,
) -> bool:
    base_url = os.environ.get("BASE_URL")
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
