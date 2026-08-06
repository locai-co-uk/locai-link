#!/bin/sh
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Headless Locai Link installer (Linux + macOS), single line:
#
#     curl -fsSL https://raw.githubusercontent.com/locai-co-uk/locai-link/main/scripts/install.sh | sh
#
# The script (this file) is served from the repo via raw.githubusercontent; the
# builds + checksums.txt it pulls live on GitHub Releases.
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
#   LOCAI_BINARY_BASE     base for the tarball + checksums.txt (default: the GitHub
#                         Releases latest/download path)
#   LOCAI_CHECKSUMS_URL   full URL to checksums.txt (default: LOCAI_BINARY_BASE/checksums.txt)
#   LOCAI_ARTIFACT_BASE   engine artifact store base, baked into the service env so
#                         on-demand engine fetches resolve (default: the prod CDN)
#   LOCAI_INSTALL_ROOT    install dir (default: ~/.local/share/locai)
set -eu

BINARY_BASE="${LOCAI_BINARY_BASE:-https://github.com/locai-co-uk/locai-link/releases/latest/download}"
INSTALL_ROOT="${LOCAI_INSTALL_ROOT:-$HOME/.local/share/locai}"
LABEL="uk.co.locai.link.headless"

log() { printf '[locai-headless] %s\n' "$*"; }
err() { printf '[locai-headless] ERROR: %s\n' "$*" >&2; exit 1; }

# --- detect platform-arch (matches the store/tarball naming) ---------
detect_platform() {
    os="$(uname -s)"; machine="$(uname -m)"
    case "$os" in
        Linux)  plat="linux" ;;
        Darwin) plat="macos" ;;
        *) err "unsupported OS: $os (headless supports Linux and macOS; Windows uses install.ps1)" ;;
    esac
    case "$machine" in
        x86_64|amd64) arch="x64" ;;
        arm64|aarch64) arch="arm64" ;;
        *) err "unsupported architecture: $machine" ;;
    esac
    echo "${plat}-${arch}"
}

# --- fetch + checksum-verify (bootstrap trust) -----------------------
# The tarball is what everything else trusts, so verify its sha256 against the
# release-wide checksums.txt before unpacking. (Signing/notarisation layers on
# per-OS; this is the transport-integrity floor.)
fetch_verified() {
    url="$1"; dest="$2"; asset="$3"
    log "downloading $url"
    curl -fsSL "$url" -o "$dest" || err "download failed: $url"
    sums="$TMP/checksums.txt"
    curl -fsSL "$CHECKSUMS_URL" -o "$sums" 2>/dev/null \
        || err "no checksums.txt at $CHECKSUMS_URL - refusing to install unverified"
    # sha256sum format: "<hash>  <name>" (or "<hash> *<name>" in binary mode).
    want="$(awk -v f="$asset" '$2 == f || $2 == "*" f {print $1}' "$sums" | head -1)"
    [ -n "$want" ] || err "no checksum for $asset in checksums.txt"
    got="$(sha256sum "$dest" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$dest" | cut -d' ' -f1)"
    [ "$want" = "$got" ] || err "checksum mismatch for $asset (want $want, got $got)"
    log "checksum verified against checksums.txt"
}

PLATFORM="$(detect_platform)"
ASSET="locai-link-headless-$PLATFORM.tar.gz"
TARBALL_URL="${LOCAI_HEADLESS_URL:-$BINARY_BASE/$ASSET}"
CHECKSUMS_URL="${LOCAI_CHECKSUMS_URL:-$BINARY_BASE/checksums.txt}"
log "platform: $PLATFORM"
log "install root: $INSTALL_ROOT"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fetch_verified "$TARBALL_URL" "$TMP/headless.tar.gz" "$ASSET"

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
