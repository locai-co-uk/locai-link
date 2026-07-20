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
