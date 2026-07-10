#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Locai Link uninstaller for Linux.
#
# Stops + disables both systemd user units, removes their .service
# files, and deletes the install root. No sudo required (matches the
# no-sudo install).
#
# Invoked either directly (`~/.local/share/locai/uninstall.sh`) or
# from the companion's Preferences → Advanced → Uninstall button.
#
# The device stays registered in Control after uninstall; operators
# who want it fully gone should delete the device row in Control.
set -uo pipefail

INSTALL_ROOT="${LOCAI_INSTALL_ROOT:-$HOME/.local/share/locai}"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"

log() {
    echo "[locai-uninstall] $*"
}

# Refuse to run `rm -rf` unless the directory looks like an actual Locai
# install root — must contain boot.json (dropped by install.sh). Positive
# check catches arbitrary paths like LOCAI_INSTALL_ROOT=$HOME/... that a
# blocklist can't enumerate.
if [[ ! -f "$INSTALL_ROOT/boot.json" ]]; then
    log "refusing to remove '$INSTALL_ROOT': not a Locai install root (no boot.json)"
    exit 1
fi

# --- 1. Stop + disable user services (best-effort) --------------------
# `disable --now` stops and prevents auto-start in one call. Failures
# (unit not enabled, already gone, etc.) are non-fatal — we just want
# to make sure they're not running before we delete the binary.
systemctl --user disable --now locai-link-companion.service 2>/dev/null || true
systemctl --user disable --now locai-link-agent.service     2>/dev/null || true

# Belt-and-braces: bootout equivalent — kill anything that's still
# running under our binary paths.
pkill -f "$INSTALL_ROOT/companion"       2>/dev/null || true
pkill -f "$INSTALL_ROOT/setup-assistant" 2>/dev/null || true

# --- 2. Remove unit files + drop-ins ---------------------------------

rm -f "$SYSTEMD_USER_DIR/locai-link-agent.service"
rm -f "$SYSTEMD_USER_DIR/locai-link-companion.service"
rm -rf "$SYSTEMD_USER_DIR/locai-link-agent.service.d"
rm -rf "$SYSTEMD_USER_DIR/locai-link-companion.service.d"

systemctl --user daemon-reload

# --- 3. Remove .desktop menu entries ----------------------------------

rm -f "$DESKTOP_DIR/locai-link.desktop"
rm -f "$DESKTOP_DIR/locai-setup-assistant.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

# --- 4. Remove icons --------------------------------------------------

ICON_ROOT="$HOME/.local/share/icons/hicolor"
for size in 32 128 256; do
    rm -f "$ICON_ROOT/${size}x${size}/apps/locai-link.png"
done
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$ICON_ROOT" 2>/dev/null || true
fi

# --- 5. Remove install root -------------------------------------------

rm -rf "$INSTALL_ROOT"

log "Locai Link removed."
exit 0
