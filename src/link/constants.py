# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Single source of truth for constants shared across the codebase."""

from __future__ import annotations

# --- Repository ------------------------------------------------------------
REPO_SLUG = "locai-co-uk/locai-link"
REPO_URL = f"https://github.com/{REPO_SLUG}.git"
DEFAULT_BRANCH = "main"

# --- Reverse-DNS identity ---------------------------------------------------
# Runtime uses only the companion label (the updater relaunches the companion by this
# launchd label after an OTA).
REVERSE_DNS = "uk.co.locai.link"
COMPANION_LABEL = f"{REVERSE_DNS}.companion"

# --- Loopback --------------------------------------------------------------
# Agent health/models/update server. The Rust companion polls this host:port; the
# loopback drift guard pins both to crates/companion/src-tauri/src/shared/health.rs.
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 20505

# --- Control API -------------------------------------------------------------
# Default Control API base for onboarding and the update check.
DEFAULT_API_URL = "https://api.locai.co.uk/api/v1"

# --- macOS install layout ----------------------------------------------------
# Packaged install root; also hardcoded in crates/companion/src-tauri/src/shared/endpoints.rs, the
# pkg scripts, and the plists (drift-guarded).
MACOS_INSTALL_ROOT = "/Library/Locai"
# Mutable runtime state under the install root. The companion writes its running
# version marker here (crates/companion); the updater's drift check reads it.
STATE_SUBDIR = "state"
COMPANION_RUNNING_VERSION_MARKER = "companion-running-version"
