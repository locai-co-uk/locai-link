# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from pathlib import Path

import pytest


@pytest.fixture
def device_config():
    """Valid base auth configuration for a device."""
    return {
        "device_id": "test_device_123",
        "api_key": "test_api_key_abc",
        "api_url": "http://localhost:8000/api/v1",
        "base_url": "http://localhost:8000/api/v1",
    }


@pytest.fixture
def runtime_config():
    """Valid runtime configuration (e.g. from <device_id>.json)."""
    return {
        "serving": {"default_host": "127.0.0.1", "default_port": 9090},
        "process": {
            "artifacts": [{"name": "model", "path": "/agent/models/test_model.gguf", "framework": "GGUF"}],
            "parameters": {"n_gpu_layers": 20, "n_ctx": 4096},
        },
    }


@pytest.fixture
def mock_paths(mocker):
    """Patches path constants across the codebase."""
    mock_root = Path("/mock/root")
    mock_configs = mock_root / "configs"
    mock_models = mock_root / "models"
    mock_agent_cfg = mock_configs / "agent_config.json"

    targets = ["link.utils", "link.agent", "link.server", "link.inference.dispatcher"]

    for module in targets:
        mocker.patch(f"{module}.PROJECT_ROOT", mock_root, create=True)
        mocker.patch(f"{module}.CONFIGS_DIR", mock_configs, create=True)
        mocker.patch(f"{module}.MODELS_DIR", mock_models, create=True)
        mocker.patch(f"{module}.AGENT_CONFIG_PATH", mock_agent_cfg, create=True)

    return mock_root
