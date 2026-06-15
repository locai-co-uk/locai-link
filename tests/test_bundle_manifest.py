# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for bundling/manifest.py — asset-name derivation + manifest writing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from manifest import (
    MANIFEST_VERSION,
    PLUGIN_CODES,
    PLUGIN_ORDER,
    derive_asset_name,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# derive_asset_name
# ---------------------------------------------------------------------------


def test_single_plugin():
    assert derive_asset_name(["language_model"]) == "locai-link-llm"


def test_two_plugins_in_canonical_order():
    assert derive_asset_name(["language_model", "audio_transcriber"]) == "locai-link-llm-stt"


def test_two_plugins_input_order_doesnt_matter():
    """Asset name is deterministic regardless of input order."""
    a = derive_asset_name(["language_model", "audio_transcriber"])
    b = derive_asset_name(["audio_transcriber", "language_model"])
    assert a == b == "locai-link-llm-stt"


def test_stt_only():
    assert derive_asset_name(["audio_transcriber"]) == "locai-link-stt"


def test_empty_plugin_list_fails():
    with pytest.raises(SystemExit) as exc:
        derive_asset_name([])
    assert "bare bundles" in str(exc.value).lower()


def test_unknown_plugin_fails_with_actionable_message():
    with pytest.raises(SystemExit) as exc:
        derive_asset_name(["language_model", "not_real"])
    msg = str(exc.value)
    assert "not_real" in msg
    assert "PLUGIN_CODES" in msg  # tells you where to fix it


def test_plugin_codes_and_order_are_consistent():
    """Every plugin in PLUGIN_ORDER has a code, and vice versa."""
    assert set(PLUGIN_CODES.keys()) == set(PLUGIN_ORDER)


# ---------------------------------------------------------------------------
# write_manifest
# ---------------------------------------------------------------------------


def test_write_manifest_single_plugin(tmp_path):
    target = write_manifest(tmp_path, ["language_model"], REPO_ROOT)
    data = json.loads(target.read_text())
    assert data["manifest_version"] == MANIFEST_VERSION
    assert data["asset_name"] == "locai-link-llm"
    assert [p["name"] for p in data["plugins"]] == ["language_model"]
    assert data["version"] and isinstance(data["version"], str)
    assert data["built_at"].endswith("Z")


def test_write_manifest_two_plugins(tmp_path):
    target = write_manifest(tmp_path, ["language_model", "audio_transcriber"], REPO_ROOT)
    data = json.loads(target.read_text())
    assert data["asset_name"] == "locai-link-llm-stt"
    plugin_names = [p["name"] for p in data["plugins"]]
    assert "language_model" in plugin_names
    assert "audio_transcriber" in plugin_names
    assert all(p["version"] for p in data["plugins"])


def test_write_manifest_rejects_empty(tmp_path):
    with pytest.raises(SystemExit):
        write_manifest(tmp_path, [], REPO_ROOT)


def test_write_manifest_rejects_unknown_plugin(tmp_path):
    with pytest.raises(SystemExit):
        write_manifest(tmp_path, ["language_model", "image_classifier"], REPO_ROOT)
