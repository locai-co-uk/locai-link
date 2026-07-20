# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Single source of truth for cross-boundary constants (Python side).

These facts are shared across Python, Rust, the pkg/shell scripts, the LaunchAgent
plists, and CI. Historically each side hardcoded its own copy, kept in sync only by
"must match" comments. This module is the Python source; the drift guards in
``tests/test_cross_boundary_constants.py`` pin it (and the other languages) together.

Dependency-free by design: import this anywhere without cycles.

WIRING PLAN (done in a verified pass, not yet applied):
    REPO_SLUG            <- src/link/app/updater.py DEFAULT_RELEASES_REPO
    REPO_URL             <- src/link/main.py DEFAULT_REPO_URL
    DEFAULT_BRANCH       <- src/link/main.py:34 AND src/link/app/updater.py:78 (dup)
    HEALTH_PORT          <- src/link/infra/health_server.py HEALTH_PORT
    COMPANION_LABEL      <- src/link/app/updater.py _COMPANION_LABEL
    MACOS_INSTALL_ROOT   <- src/link/app/updater.py "/Library/Locai" literal(s)
    OS_TAGS / ARCH_TAGS  <- src/link/app/updater.py _platform_tag() maps
    STATE_DIRNAME +      <- src/link/app/updater.py "state" / marker filenames
      *_MARKER
Each wiring step replaces a literal with an import here; when it does, the matching
guard in tests/test_cross_boundary_constants.py must switch from scraping the literal
to importing the constant (it still checks against the Rust/plist/CI authorities).
Values below are verified equal to the current literals, so wiring is value-preserving.
"""

from __future__ import annotations

# --- Repository ------------------------------------------------------------
REPO_SLUG = "locai-co-uk/locai-link"
REPO_URL = f"https://github.com/{REPO_SLUG}.git"
DEFAULT_BRANCH = "main"

# --- Reverse-DNS identity (labels / bundle ids) ----------------------------
REVERSE_DNS = "uk.co.locai.link"
AGENT_LABEL = f"{REVERSE_DNS}.agent"
COMPANION_LABEL = f"{REVERSE_DNS}.companion"
COMPANION_BUNDLE_ID = f"{REVERSE_DNS}.companion"
SETUP_ASSISTANT_BUNDLE_ID = f"{REVERSE_DNS}.setup-assistant"
PKG_RECEIPT = f"{REVERSE_DNS}.runtime"

# --- Filesystem ------------------------------------------------------------
# macOS whole-app install root (str; callers wrap in Path). Rust/pkg/plists
# hardcode the same path; the packaging tests guard those.
MACOS_INSTALL_ROOT = "/Library/Locai"
STATE_DIRNAME = "state"
COMPANION_RUNNING_VERSION_MARKER = "companion-running-version"
UI_DRIFT_NOTIFIED_MARKER = "ui-drift-notified"

# --- Loopback --------------------------------------------------------------
# Agent health/models/update server. The Rust companion polls this port; the
# loopback-port drift guard pins it to crates/shared/src/health.rs.
HEALTH_PORT = 20505

# --- Release asset platform tag (<os>-<arch>) ------------------------------
# Mirrors the maps in updater._platform_tag() and the release.yml matrix. Only
# these os/arch values are produced; the platform-tag guard pins them to CI.
OS_TAGS = {"linux": "linux", "darwin": "macos", "win32": "windows"}
ARCH_TAGS = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}
