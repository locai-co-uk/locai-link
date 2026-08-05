#!/bin/sh
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Headless Locai Link installer (Linux + macOS), single line:
#
#     curl -fsSL https://get.locai.co.uk/headless.sh | sh
#
# Installs the stripped headless build (supervisor only, no tray/setup, NO engines
# bundled) per-user, wires it as a background service, and leaves engines to be
# pulled on demand from the artifact store at first use. Sits alongside the
# desktop .dmg/.pkg/AppImage, which are unchanged. POSIX sh (no bashisms) so the
# one line works on a stock server shell.
#
# Config (env overrides, all optional):
#   LOCAI_HEADLESS_URL    full URL to the headless tarball (default: resolved
#                         from LOCAI_BINARY_BASE + detected platform-arch)
#   LOCAI_BINARY_BASE     base for the tarball + .sha256 (default: the release CDN)
#   LOCAI_ARTIFACT_BASE   engine artifact store base, baked into the service env so
#                         on-demand engine fetches resolve (default: the prod CDN)
#   LOCAI_INSTALL_ROOT    install dir (default: ~/.local/share/locai)
set -eu

BINARY_BASE="${LOCAI_BINARY_BASE:-https://get.locai.co.uk/headless}"
INSTALL_ROOT="${LOCAI_INSTALL_ROOT:-$HOME/.local/share/locai}"
LABEL="uk.co.locai.link.headless"

log() { printf '[locai-headless] %s\n' "$*"; }
err() { printf '[locai-headless] ERROR: %s\n' "$*" >&2; exit 1; }

# --- detect platform-arch (matches the store/tarball naming) ---------
detect_platform() {
    os="$(uname -s)"; machine="$(uname -m)"
    case "$os" in
        Linux)  plat="linux" ;;
        Darwin) plat="darwin" ;;
        *) err "unsupported OS: $os (headless supports Linux and macOS; Windows uses install-headless.ps1)" ;;
    esac
    case "$machine" in
        x86_64|amd64) arch="x64" ;;
        arm64|aarch64) arch="arm64" ;;
        *) err "unsupported architecture: $machine" ;;
    esac
    echo "${plat}-${arch}"
}

# --- fetch + checksum-verify (bootstrap trust) -----------------------
# The binary we pull is what everything else trusts, so verify its sha256 against
# the published .sha256 before running it. (Signing/notarisation is layered on top
# per-OS; this is the transport-integrity floor.)
fetch_verified() {
    url="$1"; dest="$2"
    log "downloading $url"
    curl -fsSL "$url" -o "$dest" || err "download failed: $url"
    if curl -fsSL "$url.sha256" -o "$dest.sha256" 2>/dev/null; then
        want="$(cut -d' ' -f1 < "$dest.sha256")"
        got="$(shasum -a 256 "$dest" 2>/dev/null | cut -d' ' -f1 || sha256sum "$dest" | cut -d' ' -f1)"
        [ "$want" = "$got" ] || err "checksum mismatch for $url (want $want, got $got)"
        log "checksum verified"
    else
        err "no .sha256 for $url — refusing to install an unverified binary"
    fi
}

PLATFORM="$(detect_platform)"
TARBALL_URL="${LOCAI_HEADLESS_URL:-$BINARY_BASE/locai-link-headless-$PLATFORM.tar.gz}"
log "platform: $PLATFORM"
log "install root: $INSTALL_ROOT"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fetch_verified "$TARBALL_URL" "$TMP/headless.tar.gz"

# --- lay the payload (binary + no-engine runtime), no engines --------
mkdir -p "$INSTALL_ROOT" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/state" "$INSTALL_ROOT/engines"
tar -xzf "$TMP/headless.tar.gz" -C "$INSTALL_ROOT"
BIN="$INSTALL_ROOT/locai-link"
[ -x "$BIN" ] || chmod +x "$BIN" 2>/dev/null || true
[ -f "$BIN" ] || err "headless binary not found at $BIN after extract"

# Engine store base handed to the service so on-demand fetches resolve. Empty =>
# the client's built-in prod default.
ART_BASE="${LOCAI_ARTIFACT_BASE:-}"

# --- install + start the per-user service ----------------------------
os="$(uname -s)"
if [ "$os" = "Linux" ]; then
    command -v systemctl >/dev/null 2>&1 || err "systemctl not found — headless Linux requires systemd (--user)"
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    {
        echo "[Unit]"
        echo "Description=Locai Link (headless)"
        echo "After=network-online.target"
        echo ""
        echo "[Service]"
        echo "ExecStart=$BIN run --headless"
        [ -n "$ART_BASE" ] && echo "Environment=LOCAI_ARTIFACT_BASE=$ART_BASE"
        echo "Restart=on-failure"
        echo "WorkingDirectory=$INSTALL_ROOT"
        echo ""
        echo "[Install]"
        echo "WantedBy=default.target"
    } > "$UNIT_DIR/$LABEL.service"
    systemctl --user daemon-reload
    systemctl --user enable --now "$LABEL.service"
    loginctl enable-linger "$(id -un)" 2>/dev/null || true  # survive logout on a server
    log "service started: systemctl --user status $LABEL"
elif [ "$os" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        echo '<plist version="1.0"><dict>'
        echo "  <key>Label</key><string>$LABEL</string>"
        echo "  <key>ProgramArguments</key><array><string>$BIN</string><string>run</string><string>--headless</string></array>"
        [ -n "$ART_BASE" ] && echo "  <key>EnvironmentVariables</key><dict><key>LOCAI_ARTIFACT_BASE</key><string>$ART_BASE</string></dict>"
        echo "  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
        echo "</dict></plist>"
    } > "$PLIST"
    uid="$(id -u)"
    launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$uid" "$PLIST"
    log "service started: launchctl print gui/$uid/$LABEL"
fi

log "installed. Onboard this device by following the login prompt in the service log:"
if [ "$os" = "Linux" ]; then
    log "  journalctl --user -u $LABEL -f"
else
    log "  tail -f $INSTALL_ROOT/logs/*.log"
fi
