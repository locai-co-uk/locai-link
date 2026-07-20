# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Cross-boundary constant drift guards.

The same facts (the repo, the agent loopback port, the companion service label /
bundle id) are declared independently in Python, Rust, and the packaged plists,
kept in sync only by convention. These tests turn those "must match" conventions
into enforced contracts, so a one-sided edit fails CI instead of shipping a silent
mismatch. Part of the single-source-of-truth cleanup.
"""

from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "link"
CRATES = REPO_ROOT / "crates"
LAUNCH_AGENTS = REPO_ROOT / "bundling" / "pkg" / "LaunchAgents"


def _grep1(path: Path, pattern: str) -> str:
    m = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, f"pattern {pattern!r} not found in {path}"
    return m.group(1)


def test_repo_slug_matches_between_clone_url_and_releases_repo():
    """The git clone URL and the releases repo slug must name the same repo, or
    source clone and update discovery point at different places."""
    clone_url = _grep1(SRC / "main.py", r'DEFAULT_REPO_URL\s*=\s*"([^"]+)"')
    releases_repo = _grep1(SRC / "app" / "updater.py", r'DEFAULT_RELEASES_REPO\s*=\s*"([^"]+)"')
    slug = clone_url.removeprefix("https://github.com/").removesuffix(".git")
    assert slug == releases_repo


def test_agent_loopback_port_matches_rust_health_url():
    """Python serves health on HEALTH_PORT; the Rust companion polls a hardcoded
    loopback URL. If the ports drift, the tray can never see the agent."""
    py_port = _grep1(SRC / "infra" / "health_server.py", r"HEALTH_PORT\s*=\s*(\d+)")
    rust_port = _grep1(
        CRATES / "shared" / "src" / "health.rs",
        r'DEFAULT_HEALTH_URL[^"]*"http://127\.0\.0\.1:(\d+)/',
    )
    assert py_port == rust_port


def test_companion_label_matches_plist_and_bundle_id():
    """The label the updater kickstarts after an OTA, the companion LaunchAgent's
    Label, and the companion app's bundle identifier must all agree, or the
    relaunch targets a service that isn't the running app."""
    updater_label = _grep1(SRC / "app" / "updater.py", r'_COMPANION_LABEL\s*=\s*"([^"]+)"')
    plist_label = plistlib.loads((LAUNCH_AGENTS / "uk.co.locai.link.companion.plist").read_bytes())["Label"]
    bundle_id = json.loads((CRATES / "companion" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))[
        "identifier"
    ]
    assert updater_label == plist_label == bundle_id


def test_distribution_archs_are_all_built_for_macos():
    """The installer must only advertise macOS architectures the release actually
    builds; otherwise that Mac installs but can never find an OTA asset."""
    dist = (REPO_ROOT / "bundling" / "pkg" / "Distribution.xml").read_text(encoding="utf-8")
    m = re.search(r'hostArchitectures="([^"]+)"', dist)
    assert m, "hostArchitectures not found in Distribution.xml"
    advertised = {a.strip() for a in m.group(1).split(",")}

    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    tags = re.findall(r"platform_tag:\s*(\S+)", release)
    built_macos = {t.split("macos-", 1)[1] for t in tags if t.startswith("macos-")}
    assert built_macos, "no macOS build lane found in release.yml"

    missing = advertised - built_macos
    assert not missing, f"Distribution.xml advertises {missing} with no macOS build lane"


def test_release_platform_tags_use_updater_vocabulary():
    """Every platform_tag the release workflow builds must be one _platform_tag can
    produce, or the updater can never request an asset the release published."""
    updater = (SRC / "app" / "updater.py").read_text(encoding="utf-8")
    os_map = re.search(r"os_tag = \{([^}]+)\}", updater)
    arch_map = re.search(r"arch_tag = \{([^}]+)\}", updater)
    assert os_map and arch_map, "os_tag/arch_tag maps not found in updater"
    os_values = set(re.findall(r':\s*"([^"]+)"', os_map.group(1)))
    arch_values = set(re.findall(r':\s*"([^"]+)"', arch_map.group(1)))

    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    tags = re.findall(r"platform_tag:\s*(\S+)", release)
    assert tags, "no platform_tag matrix entries found in release.yml"
    for tag in tags:
        os_part, _, arch_part = tag.partition("-")
        assert os_part in os_values, f"{tag}: os '{os_part}' not in updater os map {os_values}"
        assert arch_part in arch_values, f"{tag}: arch '{arch_part}' not in updater arch map {arch_values}"
