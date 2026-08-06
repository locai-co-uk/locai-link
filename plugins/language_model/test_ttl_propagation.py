# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""TTL propagation: a per-model `ttl` reaches the generated llama-swap config,
with `None` falling back to the default and per-model values kept independent.

Exercises `add_model` -> `_write_config`; llama-swap process management is
mocked, so no binaries and no ports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

try:
    from .swap_manager import SwapManager
except ImportError:  # flat layout (pytest prepend import mode)
    from swap_manager import SwapManager


def _make_sm(monkeypatch, tmp_path) -> SwapManager:
    monkeypatch.chdir(tmp_path)
    sm = SwapManager(9000, "127.0.0.1", Path("bin"))
    # Don't spawn/reload llama-swap; we only assert the written config.
    monkeypatch.setattr(sm, "_is_running", lambda: False)
    monkeypatch.setattr(sm, "_start", lambda: None)
    monkeypatch.setattr(sm, "_reload", lambda: None)
    return sm


def _models(sm: SwapManager) -> dict[str, Any]:
    return json.loads(sm._config_path.read_text())["models"]


def test_custom_ttl_written(monkeypatch, tmp_path):
    sm = _make_sm(monkeypatch, tmp_path)
    sm.add_model("m", "/models/m.gguf", ttl=42)
    assert _models(sm)["m"]["ttl"] == 42


def test_none_ttl_falls_back_to_default(monkeypatch, tmp_path):
    sm = _make_sm(monkeypatch, tmp_path)
    sm.add_model("m", "/models/m.gguf", ttl=None)
    assert _models(sm)["m"]["ttl"] == SwapManager._MODEL_TTL


def test_per_model_ttls_are_independent(monkeypatch, tmp_path):
    sm = _make_sm(monkeypatch, tmp_path)
    sm.add_model("a", "/models/a.gguf", ttl=60)
    sm.add_model("b", "/models/b.gguf")  # omitted -> default
    models = _models(sm)
    assert models["a"]["ttl"] == 60
    assert models["b"]["ttl"] == SwapManager._MODEL_TTL


@pytest.mark.parametrize("ttl", [-1, 0, 1, 300])
def test_valid_ttl_boundaries_accepted(monkeypatch, tmp_path, ttl):
    sm = _make_sm(monkeypatch, tmp_path)
    sm.add_model("m", "/models/m.gguf", ttl=ttl)
    assert _models(sm)["m"]["ttl"] == ttl


@pytest.mark.parametrize("ttl", [-2, -1.5, 1.9, True, "60"])
def test_invalid_ttl_rejected(monkeypatch, tmp_path, ttl):
    sm = _make_sm(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        sm.add_model("m", "/models/m.gguf", ttl=ttl)
