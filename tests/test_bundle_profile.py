# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for bundling/bundle_profile.py — the loader + CLI-override merge.

Covers:
- A real profile in bundling/profiles/ loads to the expected BundleSpec.
- Unknown / typo'd top-level fields surface as SystemExit with a useful message.
- CLI flags override profile values; profile values pass through when no flag set.
- --all-plugins short-circuits to the full known set.
- Unknown plugins fail validation cleanly.
- write_manifest produces well-formed JSON with the expected fields.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from bundle_profile import (
    MANIFEST_VERSION,
    BundleSpec,
    empty_spec,
    load_profile,
    merge_cli,
    validate,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = REPO_ROOT / "bundling" / "profiles"
KNOWN_PLUGINS = ["language_model", "audio_transcriber", "audio_classifier", "image_classifier"]


def _cli(**overrides) -> argparse.Namespace:
    """Helper: build an argparse.Namespace matching build.py's CLI surface."""
    base = {
        "profile": None,
        "plugins": None,
        "all_plugins": False,
        "asset_name": None,
        "display_name": None,
        "description": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------


def test_standalone_profile_loads():
    spec = load_profile("standalone", PROFILES_DIR)
    assert spec.name == "standalone"
    assert spec.asset_name == "locai-link"
    assert spec.plugins == ()  # standalone bakes in no plugins


def test_meetily_profile_loads():
    spec = load_profile("meetily", PROFILES_DIR)
    assert spec.name == "meetily"
    assert spec.asset_name == "locai-link-meetily"
    assert "language_model" in spec.plugins
    assert "audio_transcriber" in spec.plugins


def test_safechat_profile_loads():
    spec = load_profile("safechat", PROFILES_DIR)
    assert spec.name == "safechat"
    assert spec.asset_name == "locai-link-safechat"
    # SafeChat is LLM-only — pin the contract so a future edit doesn't quietly
    # add plugins the production bundle wouldn't ship.
    assert spec.plugins == ("language_model",)


def test_missing_profile_lists_alternatives(tmp_path):
    (tmp_path / "alpha.yaml").write_text("name: alpha\ndisplay_name: A\ndescription: a\nasset_name: a\n")
    with pytest.raises(SystemExit) as exc:
        load_profile("beta", tmp_path)
    msg = str(exc.value)
    assert "Profile not found" in msg
    assert "alpha" in msg  # the available alternative is surfaced


def test_unknown_field_rejected(tmp_path):
    """A typo like ``pluginz:`` would otherwise silently produce an empty plugin set."""
    (tmp_path / "x.yaml").write_text(
        "name: x\ndisplay_name: X\ndescription: x\nasset_name: x\npluginz: [language_model]\n"
    )
    with pytest.raises(SystemExit) as exc:
        load_profile("x", tmp_path)
    assert "pluginz" in str(exc.value)


def test_missing_required_field(tmp_path):
    (tmp_path / "x.yaml").write_text("name: x\ndisplay_name: X\ndescription: x\n")  # no asset_name
    with pytest.raises(SystemExit) as exc:
        load_profile("x", tmp_path)
    assert "asset_name" in str(exc.value)


def test_invalid_yaml(tmp_path):
    (tmp_path / "x.yaml").write_text("name: [unclosed\n")
    with pytest.raises(SystemExit) as exc:
        load_profile("x", tmp_path)
    assert "Invalid YAML" in str(exc.value)


def test_plugins_must_be_strings(tmp_path):
    (tmp_path / "x.yaml").write_text("name: x\ndisplay_name: X\ndescription: x\nasset_name: x\nplugins: [1, 2]\n")
    with pytest.raises(SystemExit) as exc:
        load_profile("x", tmp_path)
    assert "list of strings" in str(exc.value)


# ---------------------------------------------------------------------------
# CLI merge
# ---------------------------------------------------------------------------


def test_empty_spec_for_pure_cli():
    spec = empty_spec()
    assert spec.plugins == ()
    assert spec.asset_name == "locai-link"


def test_cli_plugins_override_profile():
    base = load_profile("meetily", PROFILES_DIR)  # has language_model + audio_transcriber
    merged = merge_cli(base, _cli(plugins=["language_model"]), KNOWN_PLUGINS)
    assert merged.plugins == ("language_model",)
    # other fields preserved from profile
    assert merged.asset_name == "locai-link-meetily"


def test_cli_asset_name_overrides_profile():
    base = load_profile("meetily", PROFILES_DIR)
    merged = merge_cli(base, _cli(asset_name="locai-link-test"), KNOWN_PLUGINS)
    assert merged.asset_name == "locai-link-test"
    # plugins untouched
    assert "language_model" in merged.plugins


def test_cli_display_and_description_override():
    base = load_profile("safechat", PROFILES_DIR)
    merged = merge_cli(base, _cli(display_name="Custom Name", description="Custom description"), KNOWN_PLUGINS)
    assert merged.display_name == "Custom Name"
    assert merged.description == "Custom description"


def test_no_cli_overrides_passes_profile_through():
    base = load_profile("meetily", PROFILES_DIR)
    merged = merge_cli(base, _cli(), KNOWN_PLUGINS)
    assert merged == base  # frozen dataclass equality


def test_all_plugins_short_circuits_to_full_set():
    base = load_profile("meetily", PROFILES_DIR)
    merged = merge_cli(base, _cli(all_plugins=True), KNOWN_PLUGINS)
    assert set(merged.plugins) == set(KNOWN_PLUGINS)


def test_pure_cli_no_profile():
    spec = merge_cli(empty_spec(), _cli(plugins=["language_model"], asset_name="locai-link-x"), KNOWN_PLUGINS)
    assert spec.plugins == ("language_model",)
    assert spec.asset_name == "locai-link-x"
    assert spec.name == "custom"  # empty_spec's default


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_accepts_real_profiles():
    for name in ("standalone", "meetily", "safechat"):
        validate(load_profile(name, PROFILES_DIR), KNOWN_PLUGINS)


def test_validate_rejects_unknown_plugin():
    bad = BundleSpec(name="x", display_name="X", description="x", asset_name="x", plugins=("not_real",))
    with pytest.raises(SystemExit) as exc:
        validate(bad, KNOWN_PLUGINS)
    assert "not_real" in str(exc.value)


def test_validate_rejects_bad_asset_name():
    bad = BundleSpec(name="x", display_name="X", description="x", asset_name="bad name with spaces", plugins=())
    with pytest.raises(SystemExit) as exc:
        validate(bad, KNOWN_PLUGINS)
    assert "asset_name" in str(exc.value)


# ---------------------------------------------------------------------------
# Manifest writing
# ---------------------------------------------------------------------------


def test_write_manifest_produces_expected_shape(tmp_path):
    spec = load_profile("meetily", PROFILES_DIR)
    manifest_path = write_manifest(tmp_path, spec, REPO_ROOT)
    data = json.loads(manifest_path.read_text())

    assert data["manifest_version"] == MANIFEST_VERSION
    assert data["profile"] == "meetily"
    assert data["display_name"] == "Link for Meetily"
    assert data["asset_name"] == "locai-link-meetily"

    # version comes from root pyproject.toml — accept any non-empty string
    assert data["version"] and isinstance(data["version"], str)

    # built_at is ISO-ish UTC; accept anything ending Z so we don't pin the format too tightly
    assert data["built_at"].endswith("Z")

    plugin_names = [p["name"] for p in data["plugins"]]
    assert "language_model" in plugin_names
    assert "audio_transcriber" in plugin_names
    # plugin versions read from plugin pyproject.toml
    assert all(p["version"] for p in data["plugins"])


def test_write_manifest_empty_plugins(tmp_path):
    spec = load_profile("standalone", PROFILES_DIR)
    manifest_path = write_manifest(tmp_path, spec, REPO_ROOT)
    data = json.loads(manifest_path.read_text())
    assert data["plugins"] == []
    assert data["profile"] == "standalone"
