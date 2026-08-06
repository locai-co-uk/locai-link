# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for bundling/manifest.py — shape/platform-tag helpers + manifest writing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from manifest import (
    MANIFEST_VERSION,
    PLUGIN_CODES,
    PLUGIN_ORDER,
    SHAPES,
    asset_stem,
    platform_tag,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# asset_stem + platform_tag  (the single source of truth for asset naming)
# ---------------------------------------------------------------------------


def test_asset_stem_by_shape():
    assert asset_stem("desktop") == "locai-link-desktop"
    assert asset_stem("headless") == "locai-link-headless"


def test_asset_stem_rejects_unknown_shape():
    with pytest.raises(SystemExit) as exc:
        asset_stem("bogus")
    assert "shape" in str(exc.value).lower()


@pytest.mark.parametrize(
    "os_name,machine,expected",
    [
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "amd64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Windows", "AMD64", "windows-x64"),
    ],
)
def test_platform_tag(os_name, machine, expected):
    assert platform_tag(os_name, machine) == expected


def test_platform_tag_rejects_unknown_arch():
    # armv7l must NOT silently label as x64 (would ship an unrunnable bundle).
    with pytest.raises(SystemExit) as exc:
        platform_tag("Linux", "armv7l")
    assert "architecture" in str(exc.value).lower()


def test_platform_tag_rejects_unknown_os():
    with pytest.raises(SystemExit):
        platform_tag("Plan9", "x86_64")


def test_shapes_known():
    assert set(SHAPES) == {"desktop", "headless"}


def test_plugin_codes_and_order_are_consistent():
    """Every plugin in PLUGIN_ORDER has a code, and vice versa (still used for
    the manifest plugin_set metadata + boot.json)."""
    assert set(PLUGIN_CODES.keys()) == set(PLUGIN_ORDER)


# ---------------------------------------------------------------------------
# write_manifest
# ---------------------------------------------------------------------------


def test_write_manifest_headless(tmp_path):
    target = write_manifest(tmp_path, ["language_model"], REPO_ROOT, "headless")
    data = json.loads(target.read_text())
    assert data["manifest_version"] == MANIFEST_VERSION
    assert data["asset_name"] == "locai-link-headless"
    assert data["shape"] == "headless"
    assert [p["name"] for p in data["plugins"]] == ["language_model"]
    assert data["version"] and isinstance(data["version"], str)
    assert data["built_at"].endswith("Z")


def test_write_manifest_desktop_two_plugins(tmp_path):
    target = write_manifest(tmp_path, ["language_model", "audio_transcriber"], REPO_ROOT, "desktop")
    data = json.loads(target.read_text())
    assert data["asset_name"] == "locai-link-desktop"
    assert data["shape"] == "desktop"
    plugin_names = [p["name"] for p in data["plugins"]]
    assert "language_model" in plugin_names
    assert "audio_transcriber" in plugin_names
    assert all(p["version"] for p in data["plugins"])


def test_write_manifest_rejects_unknown_shape(tmp_path):
    with pytest.raises(SystemExit):
        write_manifest(tmp_path, ["language_model"], REPO_ROOT, "bogus")


# ---------------------------------------------------------------------------
# restructure_to_versioned_layout
# ---------------------------------------------------------------------------


def test_restructure_to_versioned_layout(tmp_path):
    """Flat PyInstaller output gets reshaped into install_root/versions/<v>/."""
    from build import restructure_to_versioned_layout

    bundle = tmp_path / "locai-link"
    bundle.mkdir()
    (bundle / "locai-link-runtime").write_text("#!/fake/binary\n")
    internal = bundle / "_internal"
    internal.mkdir()
    (internal / "payload.txt").write_text("payload\n")

    target = restructure_to_versioned_layout(bundle, "1.0.15")

    assert target == bundle / "versions" / "1.0.15"
    assert (target / "locai-link-runtime").read_text() == "#!/fake/binary\n"
    assert (target / "_internal" / "payload.txt").read_text() == "payload\n"

    current = bundle / "current"
    pointer = bundle / "CURRENT"
    assert current.is_symlink() or pointer.exists(), "neither current symlink nor CURRENT pointer was created"
    if current.is_symlink():
        # Relative symlink resolves against the install_root.
        assert (bundle / current.readlink()).resolve() == target.resolve()
    else:
        assert pointer.read_text().strip() == "1.0.15"


def test_restructure_to_versioned_layout_overwrites_stale_staging(tmp_path):
    """A stale `_staged_<version>/` from a previous failed run is wiped, not merged."""
    from build import restructure_to_versioned_layout

    stale = tmp_path / "_staged_1.0.15"
    stale.mkdir()
    (stale / "from_previous_run.txt").write_text("should be gone after rebuild\n")

    bundle = tmp_path / "locai-link"
    bundle.mkdir()
    (bundle / "locai-link-runtime").write_text("# fresh\n")

    target = restructure_to_versioned_layout(bundle, "1.0.15")

    assert (target / "locai-link-runtime").read_text() == "# fresh\n"
    assert not (target / "from_previous_run.txt").exists()
