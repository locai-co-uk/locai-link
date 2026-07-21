# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Single source of truth for constants shared across the codebase.

These facts also appear on the Rust, pkg/shell, plist, and CI sides, which keep their
own copies; the drift guards in tests/test_cross_boundary_constants.py enforce that
all copies stay equal. This module is the Python source.

Holds runtime facts only. Packaging identifiers the runtime never uses -- app bundle
ids, the pkg receipt, the agent plist label -- stay with the packaging scripts
(uninstall.sh, the plists, bundling/), not here.

Dependency-free: import anywhere without cycles.
"""

from __future__ import annotations

# --- Repository ------------------------------------------------------------
REPO_SLUG = "locai-co-uk/locai-link"
REPO_URL = f"https://github.com/{REPO_SLUG}.git"
DEFAULT_BRANCH = "main"

# --- Reverse-DNS identity ---------------------------------------------------
# Runtime uses only the companion label (the updater relaunches the companion by this
# launchd label after an OTA). Bundle ids, the pkg receipt, and the agent plist label
# are packaging facts and live with the packaging scripts, not here.
REVERSE_DNS = "uk.co.locai.link"
COMPANION_LABEL = f"{REVERSE_DNS}.companion"

# --- Loopback --------------------------------------------------------------
# Agent health/models/update server. The Rust companion polls this host:port; the
# loopback drift guard pins both to crates/shared/src/health.rs.
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 20505
