# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Single source of truth for constants shared across the codebase."""

from __future__ import annotations

import os

# --- Repository ------------------------------------------------------------
# GitHub repo the frozen OTA downloads release assets from (updater DEFAULT_RELEASES_REPO).
REPO_SLUG = "locai-co-uk/locai-link"

# --- Reverse-DNS identity ---------------------------------------------------
# Runtime uses only the companion label (the updater relaunches the companion by this
# launchd label after an OTA).
REVERSE_DNS = "uk.co.locai.link"
COMPANION_LABEL = f"{REVERSE_DNS}.companion"

# --- Loopback --------------------------------------------------------------
# Agent health/models/update server. The Rust companion polls this host:port; the
# loopback drift guard pins both to crates/link/src-tauri/src/shared/health.rs.
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 20505

# --- Control API -------------------------------------------------------------
# Default Control API base for onboarding and the update check. LOCAI_API_URL
# overrides it (the supervisor injects a build-time-baked value on dev builds);
# `or` so an empty value falls back rather than producing relative URLs.
DEFAULT_API_URL = os.environ.get("LOCAI_API_URL") or "https://api.locai.co.uk/api/v1"

# --- macOS install layout ----------------------------------------------------
# Packaged install root; also hardcoded in crates/link/src-tauri/src/shared/endpoints.rs, the
# pkg scripts, and the plists (drift-guarded).
MACOS_INSTALL_ROOT = "/Library/Locai"
# Mutable runtime state under the install root. The companion writes its running
# version marker here (crates/link); the updater's drift check reads it.
STATE_SUBDIR = "state"
COMPANION_RUNNING_VERSION_MARKER = "companion-running-version"
