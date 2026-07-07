#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Removes Locai Link from the machine.
#
# Invoked in two ways:
#   1. From the companion tray menu: "Uninstall Locai Link…" — the
#      companion shells out to `osascript` which runs this script via
#      `do shell script with administrator privileges` (so the script
#      always executes as root).
#   2. Directly from Terminal: `sudo /Library/Locai/uninstall.sh`.
#
# Because Approach 1 runs as root but the LaunchAgents live in the
# console user's GUI domain, we resolve the actual user via
# `stat -f "%Su" /dev/console` (same trick used by the postinstall
# handoff) and drop back with `sudo -u` for user-domain `launchctl`
# operations.
#
# The device stays registered in Control after uninstall — operators
# who want it fully gone should delete the device row in the Control
# UI. No user Keychain items are touched (we don't stash anything
# there today).
set -uo pipefail

INSTALL_ROOT="/Library/Locai"
CLI_SYMLINK="/usr/local/bin/locai"
# Historically a symlink into $INSTALL_ROOT; now a real `ditto`d copy
# (see postinstall). Uninstall handles both by using `rm -rf`.
COMPANION_APP_IN_APPLICATIONS="/Applications/Locai Link.app"
PKG_RECEIPT="uk.co.locai.link.runtime"

log() {
    echo "[uninstall] $*"
}

# --- 1. Stop + unload LaunchAgents (user domain) ---------------------
# LaunchAgents are per-user; `launchctl bootout gui/$UID/...` targets
# the console user's aqua session. Best-effort — bootout on an already
# stopped service returns non-zero, ignore.
CONSOLE_USER=$(stat -f "%Su" /dev/console 2>/dev/null || echo "")
if [[ -z "$CONSOLE_USER" || "$CONSOLE_USER" == "root" ]]; then
    log "no console user detected; skipping user-domain launchctl steps"
else
    USER_UID=$(id -u "$CONSOLE_USER" 2>/dev/null || echo "")
    if [[ -n "$USER_UID" ]]; then
        log "unloading LaunchAgents from $CONSOLE_USER (uid $USER_UID)"
        sudo -u "$CONSOLE_USER" launchctl bootout \
            "gui/$USER_UID/uk.co.locai.link.companion" 2>/dev/null || true
        sudo -u "$CONSOLE_USER" launchctl bootout \
            "gui/$USER_UID/uk.co.locai.link.agent" 2>/dev/null || true
    fi
    # Delete the plists themselves (bootout only unloads, doesn't
    # remove the file — a subsequent login would re-load them).
    USER_LA_DIR="/Users/$CONSOLE_USER/Library/LaunchAgents"
    rm -f "$USER_LA_DIR/uk.co.locai.link.agent.plist"
    rm -f "$USER_LA_DIR/uk.co.locai.link.companion.plist"
fi

# --- 2. Kill any stragglers -----------------------------------------
# Belt-and-braces: bootout SIGTERMs each service, but a runtime spawned
# outside launchd (e.g. `locai run` from a terminal) wouldn't be
# covered. Match on install path so we don't hit unrelated processes.
pkill -f "$INSTALL_ROOT/locai-link" 2>/dev/null || true
pkill -f "Locai Link.app"          2>/dev/null || true

# --- 3. Remove payload + symlinks -----------------------------------
rm -rf "$INSTALL_ROOT"
rm -rf "$COMPANION_APP_IN_APPLICATIONS"
rm -f  "$CLI_SYMLINK"
log "removed $INSTALL_ROOT + symlinks"

# --- 4. Forget the pkg receipt --------------------------------------
# So `pkgutil --pkgs` no longer lists us and a future reinstall doesn't
# see stale ownership records.
pkgutil --forget "$PKG_RECEIPT" 2>/dev/null || true

log "Locai Link removed"
exit 0
