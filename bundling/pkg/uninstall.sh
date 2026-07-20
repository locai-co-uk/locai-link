#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Removes Locai Link from the machine.
#
# Invoked two ways: (1) from the Setup Assistant "Uninstall" action, which
# runs this via osascript `with administrator privileges` (always root); or
# (2) directly: `sudo /Library/Locai/uninstall.sh`.
#
# Refuses to run without root (see the $EUID check) — prior versions
# swallowed permission-denied errors and exited 0, falsely claiming success.
#
# Approach 1 runs as root but LaunchAgents + Tauri caches live in the console
# user's home, so we resolve that user via `stat -f "%Su" /dev/console` and
# drop to `sudo -u` for user-domain launchctl ops.
#
# Coverage: LaunchAgents, live processes, LaunchServices entries, /Library/
# Locai + /Applications copies + CLI symlink, per-user Tauri data (caches /
# WebKit / HTTPStorages / prefs / saved state), pinned Dock tiles, pkg receipt.
#
# The device stays registered in Control after uninstall — delete the device
# row in Control to fully remove it. No Keychain items are touched.
set -uo pipefail

INSTALL_ROOT="/Library/Locai"
CLI_SYMLINK="/usr/local/bin/locai"
# Historically a symlink into $INSTALL_ROOT; now a real `ditto`d copy
# (see postinstall). Uninstall handles both by using `rm -rf`.
COMPANION_APP_IN_APPLICATIONS="/Applications/Locai Link.app"
SA_APP_IN_APPLICATIONS="/Applications/Locai Setup Assistant.app"
PKG_RECEIPT="uk.co.locai.link.runtime"
# Bundle identifiers for the two Tauri apps — used to clean per-user
# caches / prefs / WebKit storage that live outside $INSTALL_ROOT.
COMPANION_BUNDLE_ID="uk.co.locai.link.companion"
SA_BUNDLE_ID="uk.co.locai.link.setup"

log() {
    echo "[uninstall] $*"
}

# Refuse non-root. Every step writes into /Library, /Applications,
# /usr/local/bin, or the console user's Library — without root the rm's fail
# silently (`|| true`) and the script exits 0 while leaving the app installed.
if [[ $EUID -ne 0 ]]; then
    cat >&2 <<EOF
[uninstall] This script must run as root.

  Option A (recommended): open the Locai Link Setup Assistant and
                          click "Uninstall" — it invokes this script
                          via osascript with an admin prompt.

  Option B (Terminal):    sudo /Library/Locai/uninstall.sh

Aborting.
EOF
    exit 1
fi

# --- 1. Stop + unload LaunchAgents (user domain) ---------------------
# Per-user; `launchctl bootout gui/$UID/...` targets the console user's aqua
# session. Best-effort — bootout on a stopped service returns non-zero.
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
# bootout SIGTERMs each service, but a runtime spawned outside launchd (e.g.
# `locai run` from a terminal) isn't covered. Match `/<name>.app/` (leading +
# trailing slashes) so we don't hit macOS's own /System .../Setup Assistant.app.
# SA is killed LAST — it typically invoked us via osascript, so killing it
# earlier cuts off our own error path.
pkill -f "$INSTALL_ROOT/locai-link"                 2>/dev/null || true
pkill -f "/Locai Link.app/"                         2>/dev/null || true
pkill -f "/Locai Setup Assistant.app/"              2>/dev/null || true
# Install-root SA copy ("Setup Assistant.app", no "Locai " prefix); the
# full path skips macOS's own /CoreServices copy.
pkill -f "$INSTALL_ROOT/Setup Assistant.app/"       2>/dev/null || true

# --- 3. Unregister the .apps from LaunchServices --------------------
# After removing a .app, LaunchServices can keep a stale entry pointing at the
# deleted path (`open -a` / Spotlight briefly hit it). `lsregister -u` drops
# those now instead of waiting for the next background scan.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [[ -x "$LSREGISTER" ]]; then
    "$LSREGISTER" -u "$COMPANION_APP_IN_APPLICATIONS" 2>/dev/null || true
    "$LSREGISTER" -u "$SA_APP_IN_APPLICATIONS"        2>/dev/null || true
    log "LaunchServices entries unregistered"
fi

# --- 4. Remove payload + symlinks -----------------------------------
rm -rf "$INSTALL_ROOT"
rm -rf "$COMPANION_APP_IN_APPLICATIONS"
rm -rf "$SA_APP_IN_APPLICATIONS"
rm -f  "$CLI_SYMLINK"
log "removed $INSTALL_ROOT + /Applications copies + symlinks"

# --- 5. Wipe per-user data (caches / prefs / WebKit / saved state) --
# Tauri apps write under the console user's ~/Library, outside $INSTALL_ROOT.
# Without cleaning these, a "clean" reinstall inherits stale window positions,
# cookies, and WebKit state from the previous device.
if [[ -n "$CONSOLE_USER" && "$CONSOLE_USER" != "root" ]]; then
    USER_HOME="/Users/$CONSOLE_USER"
    for bundle_id in "$COMPANION_BUNDLE_ID" "$SA_BUNDLE_ID"; do
        rm -rf "$USER_HOME/Library/Caches/$bundle_id"
        rm -rf "$USER_HOME/Library/WebKit/$bundle_id"
        rm -rf "$USER_HOME/Library/HTTPStorages/$bundle_id"
        rm -rf "$USER_HOME/Library/HTTPStorages/$bundle_id.binarycookies"
        rm -f  "$USER_HOME/Library/Preferences/$bundle_id.plist"
        rm -rf "$USER_HOME/Library/Saved Application State/$bundle_id.savedState"
        rm -rf "$USER_HOME/Library/Application Support/$bundle_id"
    done
    # `Application Support/Locai` is the user-facing config/state dir the
    # runtime falls back to when $INSTALL_ROOT/state isn't writeable.
    rm -rf "$USER_HOME/Library/Application Support/Locai"
    log "wiped per-user Tauri caches for $CONSOLE_USER"
fi

# --- 6. Remove pinned Dock entries ----------------------------------
# A dragged-to-Dock tile ghosts after its .app is deleted. Iterate
# persistent-apps in reverse (deletions don't shift lower indices), then
# reload. PlistBuddy bypasses cfprefsd, so `killall cfprefsd` follows to drop
# cached values.
if [[ -n "$CONSOLE_USER" && "$CONSOLE_USER" != "root" ]]; then
    DOCK_PLIST="/Users/$CONSOLE_USER/Library/Preferences/com.apple.dock.plist"
    PB="/usr/libexec/PlistBuddy"
    if [[ -f "$DOCK_PLIST" && -x "$PB" ]]; then
        count=$(sudo -u "$CONSOLE_USER" "$PB" -c "Print :persistent-apps" "$DOCK_PLIST" 2>/dev/null | grep -c "^    Dict {")
        removed=0
        for ((i = count - 1; i >= 0; i--)); do
            url=$(sudo -u "$CONSOLE_USER" "$PB" -c "Print :persistent-apps:$i:tile-data:file-data:_CFURLString" "$DOCK_PLIST" 2>/dev/null)
            # Match both SA copies: /Applications ("Locai Setup Assistant.app")
            # and install-root ("Locai/Setup Assistant.app"); the "Locai/"
            # segment skips macOS's own /CoreServices copy.
            if [[ "$url" == *"Locai%20Link.app"* || "$url" == *"Locai Link.app"* \
               || "$url" == *"Locai%20Setup%20Assistant.app"* || "$url" == *"Locai Setup Assistant.app"* \
               || "$url" == *"Locai/Setup%20Assistant.app"* || "$url" == *"Locai/Setup Assistant.app"* ]]; then
                sudo -u "$CONSOLE_USER" "$PB" -c "Delete :persistent-apps:$i" "$DOCK_PLIST" 2>/dev/null && removed=$((removed + 1))
            fi
        done
        if (( removed > 0 )); then
            sudo -u "$CONSOLE_USER" killall cfprefsd 2>/dev/null || true
            sudo -u "$CONSOLE_USER" killall Dock     2>/dev/null || true
            log "removed $removed pinned Dock entries and reloaded Dock"
        fi
    fi
fi

# --- 7. Forget the pkg receipt --------------------------------------
# So `pkgutil --pkgs` no longer lists us and a future reinstall doesn't
# see stale ownership records.
pkgutil --forget "$PKG_RECEIPT" 2>/dev/null || true

log "Locai Link removed"
exit 0
