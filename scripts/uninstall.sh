#!/bin/sh
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Headless Locai Link uninstaller (Linux + macOS). Reverses scripts/install.sh:
# stops + removes the background service, removes the `locai` CLI symlink, and
# deletes the install root (binary, runtime, fetched engines, logs, state, session).
#
#     curl -fsSL https://raw.githubusercontent.com/locai-co-uk/locai-link/main/scripts/uninstall.sh | sh
#
# No sudo on Linux; macOS may prompt only if the CLI symlink lives in /usr/local/bin.
#
# The device stays registered in Control after uninstall; remove its row in Control
# to fully deregister (device self-deregister endpoint pending).
set -u

INSTALL_ROOT="${LOCAI_INSTALL_ROOT:-$HOME/.local/share/locai}"
LABEL="uk.co.locai.link.headless"

log() { printf '[locai-uninstall] %s\n' "$*"; }

# Guard: only rm the root if it looks like a real headless install (has the binary).
# A positive check beats a blocklist for arbitrary LOCAI_INSTALL_ROOT paths.
if [ ! -f "$INSTALL_ROOT/locai-link" ]; then
    log "refusing to remove '$INSTALL_ROOT': not a Locai headless install (no locai-link binary)"
    exit 1
fi

os="$(uname -s)"

# --- 1. Stop + remove the service ------------------------------------
if [ "$os" = "Linux" ]; then
    systemctl --user disable --now "$LABEL.service" 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/$LABEL.service"
    systemctl --user daemon-reload 2>/dev/null || true
elif [ "$os" = "Darwin" ]; then
    uid="$(id -u)"
    launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
fi
# Belt-and-braces: kill anything still running from the install root.
pkill -f "$INSTALL_ROOT/locai-link" 2>/dev/null || true

# --- 2. Remove the `locai` CLI symlink (only if it points into our root) ---
for d in "/usr/local/bin" "$HOME/.local/bin"; do
    link="$d/locai"
    if [ -L "$link" ]; then
        case "$(readlink "$link")" in
            "$INSTALL_ROOT"/*) rm -f "$link" && log "removed CLI symlink $link" ;;
        esac
    fi
done

# --- 3. Remove the install root --------------------------------------
rm -rf "$INSTALL_ROOT"

log "Locai Link (headless) removed. The device may still appear in Control; remove its row there to deregister."
exit 0
