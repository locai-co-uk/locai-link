#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Removes Locai Link from the machine.
#
# Invoked in two ways:
#   1. From the Setup Assistant "Uninstall" splash action — the SA
#      shells out to `osascript` which runs this script via
#      `do shell script with administrator privileges` (so the script
#      always executes as root).
#   2. Directly from Terminal: `sudo /Library/Locai/uninstall.sh`.
#
# The script refuses to run without root — see the `$EUID` check
# below. Prior versions swallowed permission-denied errors and exited
# 0, leaving the app installed while claiming success.
#
# Because Approach 1 runs as root but the LaunchAgents and Tauri user
# caches live in the console user's home, we resolve the actual user
# via `stat -f "%Su" /dev/console` and drop back with `sudo -u` for
# user-domain `launchctl` operations.
#
# Coverage:
#   • LaunchAgents (bootout + plist rm)
#   • Live processes (pkill)
#   • LaunchServices registration (lsregister -u) so Spotlight /
#     `open -a` don't keep stale entries
#   • /Library/Locai + /Applications/Locai Link.app + /Applications/
#     Locai Setup Assistant.app + /usr/local/bin/locai symlink
#   • Per-user Tauri data: caches, WebKit storage, HTTPStorages,
#     Preferences plists, Saved Application State
#   • Pinned Dock tiles (best-effort — PlistBuddy on com.apple.dock.plist)
#   • pkgutil receipt
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
SA_APP_IN_APPLICATIONS="/Applications/Locai Setup Assistant.app"
PKG_RECEIPT="uk.co.locai.link.runtime"
# Bundle identifiers for the two Tauri apps — used to clean per-user
# caches / prefs / WebKit storage that live outside $INSTALL_ROOT.
COMPANION_BUNDLE_ID="uk.co.locai.link.companion"
SA_BUNDLE_ID="uk.co.locai.link.setup"

log() {
    echo "[uninstall] $*"
}

# Refuse to run as a non-root user. Every step below writes into
# /Library, /Applications, /usr/local/bin, or the console user's
# Library — without root the rm's fail silently (permission denied
# swallowed by `|| true`) and the script would exit 0 while leaving
# the app in place. That's a footgun: the user thinks uninstall
# succeeded and later finds Locai Link still runnable.
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
# covered. Match against `/<name>.app/` (with leading + trailing slashes)
# so we don't hit macOS's own /System/.../Setup Assistant.app.
# SA is killed LAST because it's typically the process that invoked us
# via osascript — killing it earlier cuts off our own error path.
pkill -f "$INSTALL_ROOT/locai-link"                 2>/dev/null || true
pkill -f "/Locai Link.app/"                         2>/dev/null || true
pkill -f "/Locai Setup Assistant.app/"              2>/dev/null || true

# --- 3. Unregister the .apps from LaunchServices --------------------
# Even after removing the .app bundle, LaunchServices can keep an
# indexed entry pointing at the deleted path — `open -a "Locai Link"`
# from Terminal or Spotlight then briefly shows a stale hit. Explicit
# `lsregister -u` drops those entries now instead of waiting for the
# next background scan.
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
# Tauri apps write to a handful of paths under the console user's
# ~/Library that never touch $INSTALL_ROOT. Without cleaning these,
# a "clean" reinstall would inherit stale window positions, cookies,
# and WebKit databases — and the freshly-registered device would boot
# with the previous device's cached state.
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
# Users who dragged Locai Link / Setup Assistant to the Dock leave a
# ghost tile after the .app is deleted. Iterate persistent-apps in
# reverse order (deletions don't shift lower indices), then reload the
# Dock. Best-effort — PlistBuddy edits directly bypass cfprefsd, so a
# `killall cfprefsd` follows to drop the cached values.
if [[ -n "$CONSOLE_USER" && "$CONSOLE_USER" != "root" ]]; then
    DOCK_PLIST="/Users/$CONSOLE_USER/Library/Preferences/com.apple.dock.plist"
    PB="/usr/libexec/PlistBuddy"
    if [[ -f "$DOCK_PLIST" && -x "$PB" ]]; then
        count=$(sudo -u "$CONSOLE_USER" "$PB" -c "Print :persistent-apps" "$DOCK_PLIST" 2>/dev/null | grep -c "^    Dict {")
        removed=0
        for ((i = count - 1; i >= 0; i--)); do
            url=$(sudo -u "$CONSOLE_USER" "$PB" -c "Print :persistent-apps:$i:tile-data:file-data:_CFURLString" "$DOCK_PLIST" 2>/dev/null)
            if [[ "$url" == *"Locai%20Link.app"* || "$url" == *"Locai%20Setup%20Assistant.app"* || "$url" == *"Locai Link.app"* || "$url" == *"Locai Setup Assistant.app"* ]]; then
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
