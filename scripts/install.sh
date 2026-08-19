#!/bin/sh
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Headless Locai Link installer (Linux + macOS), single line:
#
#     curl -fsSL https://get.locai.co.uk/install.sh | sh
#
# The script (this file) is served from get.locai.co.uk; the builds +
# checksums.txt it pulls live on GitHub Releases.
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
LABEL="uk.co.locai.link.headless"      # macOS launchd label (reverse-DNS)
UNIT="locai-link-headless.service"     # Linux systemd unit (kebab, parity with desktop)
BIN="$INSTALL_ROOT/locai-link"
LOCAI_CMD="locai"                      # CLI name for hints; link_cli() may switch to a full path when off-PATH

log() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; exit 1; }

FORCE=0
# `if` (not `&& ...`) so a non-matching last argument can't exit 1 under set -e.
for a in "$@"; do if [ "$a" = "--force" ]; then FORCE=1; fi; done

# Optional registration key (one-time) or fleet key (reusable), from Control.
# The key itself is never passed on argv (argv is world-readable via /proc);
# `locai register` reads it from the environment. REG_KIND is hint text only.
REG_KIND=""; REG_KEY=""
if [ -n "${LOCAI_REGISTRATION_KEY:-}" ]; then REG_KIND="--registration-key"; REG_KEY="$LOCAI_REGISTRATION_KEY"; fi
if [ -n "${LOCAI_FLEET_KEY:-}" ];        then REG_KIND="--fleet-key";        REG_KEY="$LOCAI_FLEET_KEY"; fi
export LOCAI_REGISTRATION_KEY LOCAI_FLEET_KEY

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
    # Pick the hash tool first: a `cmd | cut || fallback` pipeline takes cut's
    # exit status, so the fallback would never run (and macOS has no sha256sum).
    if command -v sha256sum >/dev/null 2>&1; then
        got="$(sha256sum "$dest" | cut -d' ' -f1)"
    elif command -v shasum >/dev/null 2>&1; then
        got="$(shasum -a 256 "$dest" | cut -d' ' -f1)"
    else
        err "neither sha256sum nor shasum found - cannot verify $asset"
    fi
    [ "$want" = "$got" ] || err "checksum mismatch for $asset (want $want, got $got)"
    log "checksum verified"
}

# Put the `locai` CLI on PATH (idempotent). Linux -> ~/.local/bin; macOS ->
# /usr/local/bin when writable, else ~/.local/bin.
link_cli() {
    if [ "$(uname -s)" = "Darwin" ] && [ -w /usr/local/bin ]; then CLI_DIR="/usr/local/bin"; else CLI_DIR="$HOME/.local/bin"; fi
    mkdir -p "$CLI_DIR"
    ln -sf "$BIN" "$CLI_DIR/locai"
    # macOS non-admin falls back to ~/.local/bin, which the default PATH omits, so
    # bare `locai` won't resolve. Flag it so the closing output shows the fix and
    # prints runnable full-path hints instead of a soft, easy-to-miss note.
    if on_path "$CLI_DIR"; then
        CLI_OFF_PATH=0; LOCAI_CMD="locai"
    else
        CLI_OFF_PATH=1; LOCAI_CMD="$CLI_DIR/locai"
    fi
}

# Install + (re)start the per-user service (idempotent). The unit is keyless
# (`locai run`): the supervisor idles until a session exists and then runs the
# agent, so registration happens out-of-band via `locai register` (no key is ever
# written into the unit).
install_service() {
    os="$(uname -s)"
    if [ "$os" = "Linux" ]; then
        command -v systemctl >/dev/null 2>&1 || err "systemctl not found - headless Linux requires systemd (--user)"
        UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
        {
            echo "[Unit]"; echo "Description=Locai Link (headless)"
            # Wants= actually pulls the target in; After= alone is a no-op here.
            echo "Wants=network-online.target"; echo "After=network-online.target"; echo ""
            echo "[Service]"
            echo "ExecStart=$BIN run"
            [ -n "${LOCAI_ARTIFACT_BASE:-}" ] && echo "Environment=LOCAI_ARTIFACT_BASE=$LOCAI_ARTIFACT_BASE"
            echo "Restart=on-failure"; echo "WorkingDirectory=$INSTALL_ROOT"; echo ""
            echo "[Install]"; echo "WantedBy=default.target"
        } > "$UNIT_DIR/$UNIT"
        systemctl --user daemon-reload
        # >/dev/null: hide systemctl's "Created symlink ..." line; keep stderr
        # so a failure surfaces its cause.
        systemctl --user enable --now "$UNIT" >/dev/null || err "failed to enable the service"
        loginctl enable-linger "$(id -un)" 2>/dev/null || true
    elif [ "$os" = "Darwin" ]; then
        PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"; mkdir -p "$HOME/Library/LaunchAgents"
        {
            echo '<?xml version="1.0" encoding="UTF-8"?>'
            echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
            echo '<plist version="1.0"><dict>'
            echo "  <key>Label</key><string>$LABEL</string>"
            printf '  <key>ProgramArguments</key><array><string>%s</string><string>run</string></array>\n' "$BIN"
            [ -n "${LOCAI_ARTIFACT_BASE:-}" ] && echo "  <key>EnvironmentVariables</key><dict><key>LOCAI_ARTIFACT_BASE</key><string>$LOCAI_ARTIFACT_BASE</string></dict>"
            echo "  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
            # Without these the service stream goes nowhere (Linux has journald).
            echo "  <key>StandardOutPath</key><string>$INSTALL_ROOT/logs/service.log</string>"
            echo "  <key>StandardErrorPath</key><string>$INSTALL_ROOT/logs/service.log</string>"
            echo "</dict></plist>"
        } > "$PLIST"
        uid="$(id -u)"
        launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$uid" "$PLIST"
    fi
}

# One-shot out-of-band registration (unattended installs that supply a key via
# env). Writes the session; the idle service auto-picks it up within a poll.
register_now() {
    log "registering this device with your key..."
    # The key reaches `register` via the exported env, never argv.
    "$BIN" register || err "registration failed; re-run: locai register $REG_KIND <KEY>"
}

installed_note() {
    log ""
    log "Locai Link is installed and the service is running."
}

# Shown only when the device still needs a key.
register_hint() {
    log ""
    log "This device isn't connected yet. Register it with a key from Control:"
    log "  $LOCAI_CMD register --registration-key <KEY>     # single device"
    log "  $LOCAI_CMD register --fleet-key <KEY|file:PATH>  # fleet enrollment"
}

# Loud PATH fix when `locai` landed off-PATH (macOS ~/.local/bin fallback). The
# hints already use the full path; this is how to make the short `locai` work.
cli_path_warning() {
    [ "${CLI_OFF_PATH:-0}" = 1 ] || return 0
    log ""
    log "IMPORTANT: 'locai' was installed to $CLI_DIR, which is not on your PATH."
    log "Add it (zsh), then open a new terminal so 'locai' works everywhere:"
    log "  echo 'export PATH=\"$CLI_DIR:\$PATH\"' >> ~/.zshrc"
}

footer() {
    cli_path_warning
    log ""
    log "Check it with '$LOCAI_CMD status', or '$LOCAI_CMD --help' for all commands."
    if [ "$(uname -s)" = "Linux" ]; then
        log "Service logs: journalctl --user -u $UNIT -f"
    else
        log "Service logs: $INSTALL_ROOT/logs/service.log"
    fi
    log "To uninstall: $LOCAI_CMD uninstall"
}

# --- re-run handling: do not clobber an existing install -------------
if [ -f "$BIN" ] && [ "$FORCE" != 1 ]; then
    link_cli
    install_service   # idempotent: ensure it's up
    if has_session; then
        installed_note
        log "Already registered (re-run with --force to reinstall in place)."
    elif [ -n "$REG_KEY" ]; then
        register_now
        installed_note
        log "This device is now connected."
    else
        installed_note
        register_hint
    fi
    footer
    exit 0
fi

# --- fresh install (or --force) --------------------------------------
PLATFORM="$(detect_platform)"
ASSET="locai-link-headless-$PLATFORM.tar.gz"
TARBALL_URL="${LOCAI_HEADLESS_URL:-$BINARY_BASE/$ASSET}"
CHECKSUMS_URL="${LOCAI_CHECKSUMS_URL:-$BINARY_BASE/checksums.txt}"
log "platform: $PLATFORM"
log "install root: $INSTALL_ROOT"

# A running service holds the binary open (Linux tar hits ETXTBSY, macOS keeps
# executing the old file); stop it before replacing files. No-op on a fresh
# install; install_service below starts it again.
stop_service() {
    # No-op when the service isn't there, but a live one that won't stop must
    # abort: extracting over a running binary leaves a mixed install.
    case "$(uname -s)" in
        Linux)
            if systemctl --user is-active --quiet "$UNIT" 2>/dev/null; then
                systemctl --user stop "$UNIT" || err "could not stop $UNIT before replacing files"
            fi
            ;;
        Darwin)
            if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
                launchctl bootout "gui/$(id -u)/$LABEL" || err "could not stop $LABEL before replacing files"
            fi
            ;;
    esac
}

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fetch_verified "$TARBALL_URL" "$TMP/headless.tar.gz" "$ASSET"

mkdir -p "$INSTALL_ROOT" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/state" "$INSTALL_ROOT/engines"
stop_service
# The headless tarball wraps a flat install-root under a top <name>/ dir;
# strip it so locai-link + versions/ + current + boot.json land at the root.
tar -xzf "$TMP/headless.tar.gz" -C "$INSTALL_ROOT" --strip-components=1
[ -f "$BIN" ] || err "headless binary not found at $BIN after extract"
[ -x "$BIN" ] || chmod +x "$BIN" 2>/dev/null || true

link_cli
install_service

if has_session; then
    installed_note
    log "Existing registration preserved."
elif [ -n "$REG_KEY" ]; then
    register_now
    installed_note
    log "This device is now connected."
else
    installed_note
    register_hint
fi
footer
