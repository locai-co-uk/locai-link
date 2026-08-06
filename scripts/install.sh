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
# bundled) per-user, wires it as a background service + the `locai` CLI, and leaves
# engines to be pulled on demand at first use. POSIX sh (no bashisms).
#
# Re-run behaviour: a re-run on an already-installed device does NOT clobber. If the
# device is registered it just reports status; if not, it re-surfaces the register
# steps. Pass --force to reinstall/update in place (the session is preserved).
#
# Config (env overrides, all optional):
#   LOCAI_HEADLESS_URL     full URL to the headless tarball (default: from BINARY_BASE)
#   LOCAI_BINARY_BASE      base for the tarball + checksums.txt (default: GitHub Releases latest/download)
#   LOCAI_CHECKSUMS_URL    full URL to checksums.txt (default: LOCAI_BINARY_BASE/checksums.txt)
#   LOCAI_ARTIFACT_BASE    engine artifact-store base, baked into the service env
#   LOCAI_INSTALL_ROOT     install dir (default: ~/.local/share/locai)
#   LOCAI_REGISTRATION_KEY one-time key from Control -> auto-register (no browser)
#   LOCAI_FLEET_KEY        reusable fleet key from Control -> unattended fleet enroll
set -eu

BINARY_BASE="${LOCAI_BINARY_BASE:-https://github.com/locai-co-uk/locai-link/releases/latest/download}"
INSTALL_ROOT="${LOCAI_INSTALL_ROOT:-$HOME/.local/share/locai}"
UNINSTALL_URL="https://raw.githubusercontent.com/locai-co-uk/locai-link/main/scripts/uninstall.sh"
LABEL="uk.co.locai.link.headless"
BIN="$INSTALL_ROOT/locai-link"

log() { printf '[locai-headless] %s\n' "$*"; }
err() { printf '[locai-headless] ERROR: %s\n' "$*" >&2; exit 1; }

FORCE=0
for a in "$@"; do [ "$a" = "--force" ] && FORCE=1; done

# Optional registration key (one-time) or fleet key (reusable), from Control.
REG_KIND=""; REG_KEY=""
if [ -n "${LOCAI_REGISTRATION_KEY:-}" ]; then REG_KIND="--registration-key"; REG_KEY="$LOCAI_REGISTRATION_KEY"; fi
if [ -n "${LOCAI_FLEET_KEY:-}" ];        then REG_KIND="--fleet-key";        REG_KEY="$LOCAI_FLEET_KEY"; fi

has_session() { for f in "$INSTALL_ROOT"/configs/session_*.json; do [ -f "$f" ] && return 0; done; return 1; }
on_path()     { case ":$PATH:" in *":$1:"*) return 0 ;; *) return 1 ;; esac; }

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

# fetch + checksum-verify (bootstrap trust): verify the tarball against the
# release-wide checksums.txt before unpacking.
fetch_verified() {
    url="$1"; dest="$2"; asset="$3"
    log "downloading $url"
    curl -fsSL "$url" -o "$dest" || err "download failed: $url"
    sums="$TMP/checksums.txt"
    curl -fsSL "$CHECKSUMS_URL" -o "$sums" 2>/dev/null \
        || err "no checksums.txt at $CHECKSUMS_URL - refusing to install unverified"
    want="$(awk -v f="$asset" '$2 == f || $2 == "*" f {print $1}' "$sums" | head -1)"
    [ -n "$want" ] || err "no checksum for $asset in checksums.txt"
    got="$(sha256sum "$dest" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$dest" | cut -d' ' -f1)"
    [ "$want" = "$got" ] || err "checksum mismatch for $asset (want $want, got $got)"
    log "checksum verified against checksums.txt"
}

# Put the `locai` CLI on PATH (idempotent). Linux -> ~/.local/bin; macOS ->
# /usr/local/bin when writable, else ~/.local/bin.
link_cli() {
    if [ "$(uname -s)" = "Darwin" ] && [ -w /usr/local/bin ]; then CLI_DIR="/usr/local/bin"; else CLI_DIR="$HOME/.local/bin"; fi
    mkdir -p "$CLI_DIR"
    ln -sf "$BIN" "$CLI_DIR/locai"
    on_path "$CLI_DIR" || log "note: $CLI_DIR is not on PATH; add it, or run $CLI_DIR/locai directly."
}

# Install + (re)start the per-user service (idempotent). Carries the engine base
# and, on a first registration, any provided key (consumed once; a session then
# takes precedence, so the key is inert afterwards).
install_service() {
    os="$(uname -s)"
    if [ "$os" = "Linux" ]; then
        command -v systemctl >/dev/null 2>&1 || err "systemctl not found - headless Linux requires systemd (--user)"
        UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
        {
            echo "[Unit]"; echo "Description=Locai Link (headless)"; echo "After=network-online.target"; echo ""
            echo "[Service]"
            echo "ExecStart=$BIN run --headless${REG_KIND:+ $REG_KIND $REG_KEY}"
            [ -n "${LOCAI_ARTIFACT_BASE:-}" ] && echo "Environment=LOCAI_ARTIFACT_BASE=$LOCAI_ARTIFACT_BASE"
            echo "Restart=on-failure"; echo "WorkingDirectory=$INSTALL_ROOT"; echo ""
            echo "[Install]"; echo "WantedBy=default.target"
        } > "$UNIT_DIR/$LABEL.service"
        systemctl --user daemon-reload
        systemctl --user enable --now "$LABEL.service"
        loginctl enable-linger "$(id -un)" 2>/dev/null || true
    elif [ "$os" = "Darwin" ]; then
        PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"; mkdir -p "$HOME/Library/LaunchAgents"
        {
            echo '<?xml version="1.0" encoding="UTF-8"?>'
            echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
            echo '<plist version="1.0"><dict>'
            echo "  <key>Label</key><string>$LABEL</string>"
            printf '  <key>ProgramArguments</key><array><string>%s</string><string>run</string><string>--headless</string>' "$BIN"
            [ -n "$REG_KIND" ] && printf '<string>%s</string><string>%s</string>' "$REG_KIND" "$REG_KEY"
            echo '</array>'
            [ -n "${LOCAI_ARTIFACT_BASE:-}" ] && echo "  <key>EnvironmentVariables</key><dict><key>LOCAI_ARTIFACT_BASE</key><string>$LOCAI_ARTIFACT_BASE</string></dict>"
            echo "  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
            echo "</dict></plist>"
        } > "$PLIST"
        uid="$(id -u)"
        launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$uid" "$PLIST"
    fi
}

logs_cmd() {
    if [ "$(uname -s)" = "Linux" ]; then echo "journalctl --user -u $LABEL -f"; else echo "tail -f $INSTALL_ROOT/logs/*.log"; fi
}

register_steps() {
    if [ -n "$REG_KEY" ]; then
        log "Registering with your key (no browser needed). Confirm with:  locai status"
    else
        log "This device is not registered yet. Register it with a key from Control:"
        log "  locai register --registration-key <KEY>     # single device"
        log "  locai register --fleet-key <KEY|file:PATH>  # fleet enrollment"
        log "then confirm:  locai status"
    fi
}

summary() {
    log ""
    log "Locai Link (headless) is installed and the service is running."
    log "  cli:       locai --help   |   locai status"
    log "  update:    locai update"
    log "  logs:      $(logs_cmd)"
    log "  uninstall: curl -fsSL $UNINSTALL_URL | sh"
}

# --- re-run handling: do not clobber an existing install -------------
if [ -f "$BIN" ] && [ "$FORCE" != 1 ]; then
    link_cli
    install_service   # idempotent: ensure it's up
    if has_session; then
        log "Locai Link is already installed and registered; the service is running."
        log "  update:  locai update    (or re-run with --force to reinstall in place)"
        summary
    else
        log "Locai Link is installed but not registered yet."
        register_steps
        summary
    fi
    exit 0
fi

# --- fresh install (or --force) --------------------------------------
PLATFORM="$(detect_platform)"
ASSET="locai-link-headless-$PLATFORM.tar.gz"
TARBALL_URL="${LOCAI_HEADLESS_URL:-$BINARY_BASE/$ASSET}"
CHECKSUMS_URL="${LOCAI_CHECKSUMS_URL:-$BINARY_BASE/checksums.txt}"
log "platform: $PLATFORM"
log "install root: $INSTALL_ROOT"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fetch_verified "$TARBALL_URL" "$TMP/headless.tar.gz" "$ASSET"

mkdir -p "$INSTALL_ROOT" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/state" "$INSTALL_ROOT/engines"
tar -xzf "$TMP/headless.tar.gz" -C "$INSTALL_ROOT"
[ -x "$BIN" ] || chmod +x "$BIN" 2>/dev/null || true
[ -f "$BIN" ] || err "headless binary not found at $BIN after extract"

link_cli
install_service

if has_session; then
    log "reinstalled; existing registration preserved."
    summary
else
    register_steps
    summary
fi
