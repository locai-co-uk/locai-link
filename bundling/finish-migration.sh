#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Finish the pre-merge -> merged transition on macOS, as root.
#
# A frictionless OTA delivers the new runtime + swaps the install-root app, but
# the privileged bits of the merged layout (the /usr/local/bin symlink, the pkg
# receipt) it can't touch. The runtime detects the pre-merge layout after such an
# OTA and runs THIS script via `osascript … with administrator privileges`, so a
# single admin prompt finishes the migration to a clean merged install - the macOS
# twin of the Linux self-heal reinstall. Ships in versions/<v>/ so it is
# version-matched and present at <install_root>/current/finish-migration.sh.
#
# Operates on the already-installed /Library/Locai bundle; lays NO payload.
# Idempotent + best-effort per step: a partial run just re-runs next time. The
# merged binary lives inside the .app (Contents/MacOS/locai-link); there is no
# separate launcher.
set -uo pipefail

INSTALL_ROOT="/Library/Locai"
CLI_SYMLINK="/usr/local/bin/locai"
APP="${INSTALL_ROOT}/Locai Link.app"
APP_IN_APPLICATIONS="/Applications/Locai Link.app"
MERGED_BIN="${APP}/Contents/MacOS/locai-link"
PKG_RECEIPT="uk.co.locai.link.runtime"
# Pre-merge artefacts to clear.
LEGACY_LAUNCHER="${INSTALL_ROOT}/locai-link"
LEGACY_SA="${INSTALL_ROOT}/Setup Assistant.app"
LEGACY_SA_IN_APPLICATIONS="/Applications/Locai Setup Assistant.app"
LEGACY_AGENT_LABEL="uk.co.locai.link.agent"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

log() { echo "[finish-migration] $*"; }

if [[ $EUID -ne 0 ]]; then
    log "must run as root (invoked via osascript admin or pkg postinstall)"
    exit 1
fi
if [[ ! -d "$APP" ]]; then
    log "ERROR: merged app not found at $APP; nothing to finish"
    exit 1
fi

# The console user owns the LaunchAgents + the per-user LS database; this script
# runs as root, so drop to that user for user-domain ops.
CONSOLE_USER=$(stat -f "%Su" /dev/console 2>/dev/null || echo "")
INSTALL_USER="${CONSOLE_USER:-root}"
USER_UID=""
USER_HOME=""
if [[ -n "$CONSOLE_USER" && "$CONSOLE_USER" != "root" ]]; then
    USER_UID=$(id -u "$CONSOLE_USER" 2>/dev/null || echo "")
    USER_HOME=$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
fi

# --- 1. Remove the legacy Setup Assistant (both copies) --------------
for legacy in "$LEGACY_SA" "$LEGACY_SA_IN_APPLICATIONS"; do
    if [[ -d "$legacy" ]]; then
        [[ -x "$LSREGISTER" ]] && "$LSREGISTER" -u "$legacy" 2>/dev/null || true
        rm -rf "$legacy"
        log "removed legacy Setup Assistant: $legacy"
    fi
done

# --- 2. Tear down the pre-merge agent unit + launcher ----------------
# The runtime already stops the agent unit (user domain) after the OTA; repeat it
# here so a direct/pkg invocation is self-sufficient. macOS lets us unlink a
# running executable, so dropping the launcher binary is safe even if it is live.
if [[ -n "$USER_UID" ]]; then
    sudo -u "$CONSOLE_USER" launchctl bootout "gui/${USER_UID}/${LEGACY_AGENT_LABEL}" 2>/dev/null || true
fi
if [[ -n "$USER_HOME" ]]; then
    rm -f "${USER_HOME}/Library/LaunchAgents/${LEGACY_AGENT_LABEL}.plist"
fi
rm -f "$LEGACY_LAUNCHER"
log "removed legacy launcher + agent unit"

# --- 3. Refresh the /Applications copy from the merged app -----------
# The OTA whole-app swap only updated the install-root copy, so Finder/Launchpad
# still points at the stale pre-merge companion. ditto preserves the signature.
rm -rf "$APP_IN_APPLICATIONS"
if ditto "$APP" "$APP_IN_APPLICATIONS" 2>/dev/null; then
    if [[ "$INSTALL_USER" != "root" ]]; then
        chown -R "$INSTALL_USER:staff" "$APP" "$APP_IN_APPLICATIONS" 2>/dev/null || true
    fi
    if [[ -x "$LSREGISTER" && -n "$CONSOLE_USER" && "$CONSOLE_USER" != "root" ]]; then
        sudo -u "$CONSOLE_USER" "$LSREGISTER" -f -R "$APP_IN_APPLICATIONS" 2>/dev/null || true
    fi
    log "refreshed $APP_IN_APPLICATIONS from merged app"
else
    log "WARN: could not refresh $APP_IN_APPLICATIONS"
fi

# --- 4. /usr/local/bin/locai symlink (needs root) --------------------
mkdir -p "$(dirname "$CLI_SYMLINK")"
ln -sf "$MERGED_BIN" "$CLI_SYMLINK"
log "CLI symlink: $CLI_SYMLINK -> $MERGED_BIN"

# --- 5. Point the pkg receipt at this install ------------------------
# Cosmetic (pkgutil metadata); forget the stale pre-merge receipt so a future
# installer doesn't see stale ownership records.
pkgutil --forget "$PKG_RECEIPT" 2>/dev/null || true

# --- 6. Ownership of the install-root subtrees the runtime writes ----
if [[ "$INSTALL_USER" != "root" ]]; then
    chown "$INSTALL_USER:staff" "$INSTALL_ROOT" 2>/dev/null || true
    for d in versions state logs configs models; do
        [[ -e "$INSTALL_ROOT/$d" ]] && chown -R "$INSTALL_USER:staff" "$INSTALL_ROOT/$d" 2>/dev/null || true
    done
fi

# --- 7. Clear the finish marker --------------------------------------
# The runtime writes this when it prompts for the finish; drop it so state/ is
# tidy once the merge is complete (the layout is now merged, so it isn't re-read).
rm -f "${INSTALL_ROOT}/state/migration-finish-prompted"

log "migration finished: merged single-unit layout"
exit 0
