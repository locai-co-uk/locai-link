#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Locai Link installer for Linux (per-user, no sudo).
#
# Lays down the Rust launcher + PyInstaller-bundled runtime under
# versions/ + a `current` symlink + the Setup Assistant and companion
# binaries + boot.json + service units, all under ~/.local/share/locai/.
# Service activation is deferred to the Setup Assistant on Finish —
# it enables the systemd user units according to the "Start at login"
# toggle.
#
# Layout after install:
#
#     ~/.local/share/locai/
#     ├── locai-link                     (Rust launcher, ELF)
#     ├── versions/vX.Y.Z/…              (PyInstaller runtime + plugins)
#     ├── current -> versions/vX.Y.Z     (OTA-swappable symlink)
#     ├── manifest.json                  (from the runtime bundle)
#     ├── setup-assistant                (Tauri ELF)
#     ├── companion                      (Tauri ELF)
#     ├── boot.json                      (channel config)
#     ├── configs/                       (session state — SA writes here)
#     ├── logs/                          (agent + companion stdout/stderr)
#     ├── systemd/*.service              (staged; SA activates them)
#     └── uninstall.sh
#
#     ~/.config/systemd/user/locai-link-{agent,companion}.service
#     ~/.local/share/applications/locai-{link,setup-assistant}.desktop
#
# Payload discovery (see resolve_paths):
#
#   * Extracted release tarball — install.sh sits next to `bundle/`,
#     Tauri binaries, boot.json, systemd/, applications/.
#   * Local repo checkout — this script is at bundling/linux/install.sh
#     and picks up dist/locai-link/ + crates/target/release/ from the
#     repo root. Enables `./install.sh` iteration without packing.
#
# Curl-from-URL note: this script is self-contained once the payload
# is next to it. Real `curl … | bash` still needs (a) CI that packs
# the tarball on tag push (see bundling/linux/pack.sh) and uploads to
# GitHub Releases, and (b) a stable hosted URL for install.sh itself.
# Neither is set up yet.
set -euo pipefail

INSTALL_ROOT="${LOCAI_INSTALL_ROOT:-$HOME/.local/share/locai}"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
LOG_PREFIX="[locai-install]"

log() {
    echo "$LOG_PREFIX $*"
}
err() {
    echo "$LOG_PREFIX ERROR: $*" >&2
    exit 1
}

# --- Locate the payload -----------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BUNDLE_DIR=""      # Rust launcher + versions/ + current + manifest.json
TAURI_DIR=""       # setup-assistant + companion binaries
UNITS_DIR=""       # locai-link-{agent,companion}.service
DESKTOPS_DIR=""    # locai-{link,setup-assistant}.desktop
BOOT_JSON=""

resolve_paths() {
    # Case 1 — extracted tarball. pack.sh writes install.sh next to a
    # `bundle/` subdir + the Tauri binaries + boot.json + systemd/ +
    # applications/. If any of that shape is missing, this isn't a
    # tarball layout and we fall through to the repo-source case.
    if [[ -d "$SCRIPT_DIR/bundle" && -f "$SCRIPT_DIR/setup-assistant" ]]; then
        BUNDLE_DIR="$SCRIPT_DIR/bundle"
        TAURI_DIR="$SCRIPT_DIR"
        UNITS_DIR="$SCRIPT_DIR/systemd"
        DESKTOPS_DIR="$SCRIPT_DIR/applications"
        BOOT_JSON="$SCRIPT_DIR/boot.json"
        return
    fi

    # Case 2 — local repo checkout, install.sh at bundling/linux/.
    # Expect `bundling/build.py` and `cargo tauri build --no-bundle`
    # to have been run first.
    local repo_root
    repo_root="$(cd "$SCRIPT_DIR/../.." && pwd)"
    if [[ -f "$repo_root/dist/locai-link/locai-link" ]]; then
        BUNDLE_DIR="$repo_root/dist/locai-link"
        TAURI_DIR="$repo_root/crates/target/release"
        UNITS_DIR="$SCRIPT_DIR/systemd"
        DESKTOPS_DIR="$SCRIPT_DIR/applications"
        BOOT_JSON="$repo_root/bundling/pkg/boot.json"
        return
    fi

    err "couldn't locate build artefacts. Either extract a release tarball and run its install.sh, or from a repo checkout run \`uv run python bundling/build.py --plugins language_model audio_transcriber\` + \`cargo tauri build --no-bundle\` on both crates first."
}

resolve_paths

log "install root:       $INSTALL_ROOT"
log "runtime bundle:     $BUNDLE_DIR"
log "tauri binaries:     $TAURI_DIR"
log "systemd units:      $UNITS_DIR"
log "desktop entries:    $DESKTOPS_DIR"

# --- Sanity checks ----------------------------------------------------

command -v systemctl >/dev/null 2>&1 || err "systemctl not found — this installer requires systemd."

SA_BIN="$TAURI_DIR/locai-link-setup-assistant"
COMPANION_BIN="$TAURI_DIR/locai-link-companion"
LAUNCHER_BIN="$BUNDLE_DIR/locai-link"

[[ -f "$LAUNCHER_BIN" ]]  || err "runtime launcher not at $LAUNCHER_BIN"
[[ -f "$SA_BIN" ]]        || err "setup-assistant binary not at $SA_BIN"
[[ -f "$COMPANION_BIN" ]] || err "companion binary not at $COMPANION_BIN"
[[ -f "$BOOT_JSON" ]]     || err "boot.json not at $BOOT_JSON"
[[ -d "$UNITS_DIR" ]]     || err "systemd units dir not at $UNITS_DIR"
[[ -d "$DESKTOPS_DIR" ]]  || err ".desktop dir not at $DESKTOPS_DIR"

# --- Layout: create install root + copy payload -----------------------

mkdir -p "$INSTALL_ROOT/configs" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/systemd"

# 1. Runtime bundle: launcher + versions/ + current + manifest etc.
# Straight `cp -a` preserves the `current` symlink and everything
# else about the bundle exactly. --no-target-directory + trailing /.
# variant to copy CONTENTS of BUNDLE_DIR into INSTALL_ROOT rather than
# nesting a locai-link/ subdir.
cp -a "$BUNDLE_DIR"/. "$INSTALL_ROOT"/
log "runtime bundle copied to $INSTALL_ROOT"

# 2. Tauri binaries — user-facing GUI apps.
install -m 0755 "$SA_BIN"        "$INSTALL_ROOT/setup-assistant"
install -m 0755 "$COMPANION_BIN" "$INSTALL_ROOT/companion"

# 3. boot.json — channel config, read by the launcher on first start.
install -m 0644 "$BOOT_JSON" "$INSTALL_ROOT/boot.json"

# 4. Uninstaller (invoked by the tray Preferences → Advanced button
# and by hand from the terminal).
install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$INSTALL_ROOT/uninstall.sh"

log "tauri binaries + boot.json + uninstall.sh installed"

# --- systemd units (staged, not activated) ----------------------------
# Stage the source .service files under $INSTALL_ROOT/systemd/; the
# Setup Assistant copies them into the user's systemd domain on Finish
# and enables them per the "Start at login" toggle. Staging here (vs
# writing directly to ~/.config/systemd/user/) means the toggle
# actually controls behavior — if we enabled at install time, unchecking
# on Finish would be too late.
install -m 0644 "$UNITS_DIR/locai-link-agent.service"     "$INSTALL_ROOT/systemd/"
install -m 0644 "$UNITS_DIR/locai-link-companion.service" "$INSTALL_ROOT/systemd/"
log "systemd units staged at $INSTALL_ROOT/systemd/"

# --- .desktop entries -------------------------------------------------
# Menu integration so "Locai Link" is discoverable in the user's app
# launcher (GNOME activities overview, KDE Kickoff, dmenu, rofi, etc.).
# Locai Link's Exec= is `systemctl --user start locai-link-companion.service`
# — idempotent by design. If the companion isn't running, systemd
# starts it; if it is, the call is a no-op and the existing tray
# instance keeps going.
mkdir -p "$DESKTOP_DIR"
install -m 0644 "$DESKTOPS_DIR/locai-link.desktop"            "$DESKTOP_DIR/"
install -m 0644 "$DESKTOPS_DIR/locai-setup-assistant.desktop" "$DESKTOP_DIR/"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi
log "menu entries installed to $DESKTOP_DIR"

# --- Launch Setup Assistant ------------------------------------------
# The wizard picks up from here — sign-in, models, permissions
# (including the "start at login" toggle that enables the systemd
# units), and Finish. Backgrounded so this script returns immediately.

log "launching Setup Assistant…"
# WebKit's DMABUF renderer errors out on many Wayland setups (Nvidia,
# some Intel), and forcing GDK_BACKEND=x11 sidesteps the DE-dependent
# Wayland window story. Also baked into locai-link-companion.service.
WEBKIT_DISABLE_DMABUF_RENDERER=1 GDK_BACKEND=x11 \
    nohup "$INSTALL_ROOT/setup-assistant" >/dev/null 2>&1 &
disown

cat <<EOF

$LOG_PREFIX Install complete.

Next:
  * The Setup Assistant window should have opened. Complete the wizard
    to register this device with Control. On Finish, the runtime and
    companion services are enabled + started.
  * "Locai Link" is now in your Applications menu — click to bring
    the companion tray back up if you ever close it.

Manual controls:
  systemctl --user status locai-link-agent locai-link-companion
  systemctl --user restart locai-link-agent
  ~/.local/share/locai/uninstall.sh                            # remove

EOF
