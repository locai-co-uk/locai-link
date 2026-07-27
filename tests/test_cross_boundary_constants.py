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

from link import constants

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "link"
CRATES = REPO_ROOT / "crates"
LAUNCH_AGENTS = REPO_ROOT / "bundling" / "pkg" / "LaunchAgents"


def test_repo_url_derives_from_repo_slug():
    """REPO_URL must be the clone URL for REPO_SLUG -- one fact now, both consumed
    from constants by main.py (clone) and updater.py (release discovery)."""
    assert constants.REPO_URL == f"https://github.com/{constants.REPO_SLUG}.git"


def test_agent_loopback_port_matches_rust_health_url():
    """Python serves health on HEALTH_PORT; the Rust companion polls a hardcoded
    loopback URL. If the ports drift, the tray can never see the agent."""
    m = re.search(
        r'DEFAULT_HEALTH_URL[^"]*"http://([\d.]+):(\d+)/',
        (CRATES / "shared" / "src" / "health.rs").read_text(encoding="utf-8"),
    )
    assert m, "DEFAULT_HEALTH_URL not found in health.rs"
    assert (constants.HEALTH_HOST, str(constants.HEALTH_PORT)) == (m.group(1), m.group(2))


def test_companion_label_matches_plist_and_bundle_id():
    """The label the updater kickstarts after an OTA, the companion LaunchAgent's
    Label, and the companion app's bundle identifier must all agree, or the
    relaunch targets a service that isn't the running app."""
    plist_label = plistlib.loads((LAUNCH_AGENTS / "uk.co.locai.link.companion.plist").read_bytes())["Label"]
    bundle_id = json.loads((CRATES / "companion" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))[
        "identifier"
    ]
    assert constants.COMPANION_LABEL == plist_label == bundle_id


def test_install_root_matches_rust_and_pkg_scripts():
    """The macOS install root is declared in Python, the Rust shared crate, the
    uninstaller, and the LaunchAgent plists. A drifted copy strands the updater,
    the tray, or the uninstaller on a directory nothing else uses."""
    root = constants.MACOS_INSTALL_ROOT

    endpoints = (CRATES / "shared" / "src" / "endpoints.rs").read_text(encoding="utf-8")
    m = re.search(r'"(/[^"]+)"\.to_string\(\)', endpoints)
    assert m, "macOS install root not found in endpoints.rs"
    assert m.group(1) == root

    uninstall = (REPO_ROOT / "bundling" / "pkg" / "uninstall.sh").read_text(encoding="utf-8")
    m = re.search(r'^INSTALL_ROOT="([^"]+)"', uninstall, re.MULTILINE)
    assert m, "INSTALL_ROOT not found in uninstall.sh"
    assert m.group(1) == root

    for plist in LAUNCH_AGENTS.glob("*.plist"):
        text = plist.read_text(encoding="utf-8")
        assert root in text, f"{plist.name} does not reference {root}"


def test_companion_running_version_marker_matches_rust():
    """The companion (Rust) writes its running version to <root>/state/<marker>;
    the updater's post-OTA drift check reads the same path. If the components
    drift, the check silently reports every companion as stale/pre-fix."""
    lib = (CRATES / "companion" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    # Assert the sequential state/<marker> join, not the two literals independently,
    # so unrelated paths containing either literal cannot satisfy the test.
    marker_path = re.compile(
        rf'\.join\("{re.escape(constants.STATE_SUBDIR)}"\)\s*'
        rf'\.join\("{re.escape(constants.COMPANION_RUNNING_VERSION_MARKER)}"\)'
    )
    assert marker_path.search(lib), "companion running-version marker path not found in lib.rs"


def test_default_api_url_is_single_sourced_in_python():
    """main.py and updater.py both need the Control API base; each held its own
    copy before. Neither may re-hardcode it now that constants owns the fact."""
    for rel in ("main.py", "app/updater.py"):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert constants.DEFAULT_API_URL not in text, f"{rel} re-hardcodes DEFAULT_API_URL"


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
