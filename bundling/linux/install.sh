#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Locai Link installer for Linux (per-user, no sudo).
#
# Lays down the launcher + bundled runtime, the GUI binary, boot.json, and
# service units under ~/.local/share/locai/. Service activation is deferred
# to the setup wizard on Finish (per the "Start at login" toggle).
#
# Layout after install:
#
#     ~/.local/share/locai/
#     ├── locai-link                     (Rust launcher, ELF)
#     ├── versions/vX.Y.Z/…              (PyInstaller runtime + plugins)
#     ├── current -> versions/vX.Y.Z     (OTA-swappable symlink)
#     ├── manifest.json                  (from the runtime bundle)
#     ├── companion                      (Tauri ELF: tray + first-run setup)
#     ├── boot.json                      (channel config)
#     ├── configs/                       (session state — setup writes here)
#     ├── logs/                          (agent + companion stdout/stderr)
#     ├── systemd/*.service              (staged; activated on Finish)
#     └── uninstall.sh
#
#     ~/.config/systemd/user/locai-link-{agent,companion}.service
#     ~/.local/share/applications/locai-link.desktop
#
# Payload discovery (see resolve_paths): either an extracted release tarball
# (install.sh next to bundle/ + binaries) or a local repo checkout (picks up
# dist/locai-link/ + crates/target/release/ for iteration without packing).
#
# Curl-from-URL note: self-contained once the payload is next to it. Real
# `curl … | bash` still needs CI packing on tag push (see pack.sh) + a stable
# hosted URL for install.sh. Neither is set up yet.
set -euo pipefail

INSTALL_ROOT="${LOCAI_INSTALL_ROOT:-$HOME/.local/share/locai}"
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
COMPANION_BIN=""   # companion Tauri binary (tray + first-run setup)
UNITS_DIR=""       # locai-link-{agent,companion}.service
DESKTOPS_DIR=""    # locai-link.desktop
ICONS_SRC=""       # dir with 32x32.png / 128x128.png / 128x128@2x.png
BOOT_JSON=""

resolve_paths() {
    # Case 1 — extracted tarball: install.sh sits next to bundle/ + the Tauri
    # binary renamed companion (no locai-link- prefix) + boot.json + systemd/ +
    # applications/ + icons/. Fall through if no match.
    if [[ -d "$SCRIPT_DIR/bundle" && -f "$SCRIPT_DIR/companion" ]]; then
        BUNDLE_DIR="$SCRIPT_DIR/bundle"
        COMPANION_BIN="$SCRIPT_DIR/companion"
        UNITS_DIR="$SCRIPT_DIR/systemd"
        DESKTOPS_DIR="$SCRIPT_DIR/applications"
        ICONS_SRC="$SCRIPT_DIR/icons"
        BOOT_JSON="$SCRIPT_DIR/boot.json"
        return
    fi

    # Case 2 — local repo checkout. Expects build.py + `cargo tauri build
    # --no-bundle` run first; cargo target names carry the crate prefix.
    local repo_root
    repo_root="$(cd "$SCRIPT_DIR/../.." && pwd)"
    if [[ -f "$repo_root/dist/locai-link/locai-link" ]]; then
        BUNDLE_DIR="$repo_root/dist/locai-link"
        COMPANION_BIN="$repo_root/crates/target/release/locai-link-companion"
        UNITS_DIR="$SCRIPT_DIR/systemd"
        DESKTOPS_DIR="$SCRIPT_DIR/applications"
        ICONS_SRC="$repo_root/crates/companion/src-tauri/icons"
        BOOT_JSON="$repo_root/bundling/pkg/boot.json"
        return
    fi

    err "couldn't locate build artefacts. Either extract a release tarball and run its install.sh, or from a repo checkout run \`uv run python bundling/build.py --plugins language_model audio_transcriber\` + \`cargo tauri build --no-bundle\` in crates/companion first."
}

resolve_paths

log "install root:       $INSTALL_ROOT"
log "runtime bundle:     $BUNDLE_DIR"
log "companion:          $COMPANION_BIN"
log "systemd units:      $UNITS_DIR"
log "desktop entries:    $DESKTOPS_DIR"
log "icons source:       $ICONS_SRC"

# --- Sanity checks ----------------------------------------------------

command -v systemctl >/dev/null 2>&1 || err "systemctl not found — this installer requires systemd."

LAUNCHER_BIN="$BUNDLE_DIR/locai-link"

[[ -f "$LAUNCHER_BIN" ]]  || err "runtime launcher not at $LAUNCHER_BIN"
[[ -f "$COMPANION_BIN" ]] || err "companion binary not at $COMPANION_BIN"
[[ -f "$BOOT_JSON" ]]     || err "boot.json not at $BOOT_JSON"
[[ -d "$UNITS_DIR" ]]     || err "systemd units dir not at $UNITS_DIR"
[[ -d "$DESKTOPS_DIR" ]]  || err ".desktop dir not at $DESKTOPS_DIR"

# --- Layout: create install root + copy payload -----------------------

mkdir -p "$INSTALL_ROOT/configs" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/systemd"

# 1. Runtime bundle: launcher + versions/ + current + manifest etc.
# `cp -a …/.` preserves the `current` symlink and copies CONTENTS into
# INSTALL_ROOT rather than nesting a locai-link/ subdir.
cp -a "$BUNDLE_DIR"/. "$INSTALL_ROOT"/
log "runtime bundle copied to $INSTALL_ROOT"

# 2. Tauri binary — the user-facing GUI app (tray + first-run setup).
install -m 0755 "$COMPANION_BIN" "$INSTALL_ROOT/companion"

# 3. boot.json — channel config, read by the launcher on first start.
install -m 0644 "$BOOT_JSON" "$INSTALL_ROOT/boot.json"

# 4. Uninstaller (invoked by the tray Preferences → Advanced button
# and by hand from the terminal).
install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$INSTALL_ROOT/uninstall.sh"

# LEGACY-SA-CLEANUP: drop a pre-merge standalone setup-assistant binary left by
# an older install (onboarding is now part of the companion). Remove once no
# pre-merge install remains.
rm -f "$INSTALL_ROOT/setup-assistant"

log "tauri binary + boot.json + uninstall.sh installed"

# --- systemd units (staged, not activated) ----------------------------
# Stage .service files under $INSTALL_ROOT/systemd/; the Setup Assistant
# copies them into the user's systemd domain on Finish, per the "Start at
# login" toggle. Staged (not written to ~/.config/systemd/user/ now) so the
# toggle controls behavior — enabling at install time would ignore it.
install -m 0644 "$UNITS_DIR/locai-link-agent.service"     "$INSTALL_ROOT/systemd/"
install -m 0644 "$UNITS_DIR/locai-link-companion.service" "$INSTALL_ROOT/systemd/"
log "systemd units staged at $INSTALL_ROOT/systemd/"

# --- .desktop entries -------------------------------------------------
# Menu integration so "Locai Link" is discoverable in the app launcher.
# Exec= is `systemctl --user start locai-link-companion.service` —
# idempotent: starts the companion if down, no-op if already running.
mkdir -p "$DESKTOP_DIR"
# Substitute `@HOME@` with the real home dir at copy time: the .desktop spec
# has no portable home-dir field code (`%h` is KDE-only), so an absolute path
# baked in per-user is the only reliable option. `install /dev/stdin` avoids
# leaving a tmp file behind.
sed "s|@HOME@|$HOME|g" "$DESKTOPS_DIR/locai-link.desktop" \
    | install -m 0644 /dev/stdin "$DESKTOP_DIR/locai-link.desktop"
# LEGACY-SA-CLEANUP: remove the pre-merge setup-assistant menu entry.
rm -f "$DESKTOP_DIR/locai-setup-assistant.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi
log "menu entries installed to $DESKTOP_DIR"

# --- Icons ------------------------------------------------------------
# `.desktop` files reference `Icon=locai-link` (themed name); without a
# matching PNG in the hicolor theme, launchers show a placeholder. Copy the
# companion-crate icons into the user hicolor tree at the sizes launchers look up.
ICON_ROOT="$HOME/.local/share/icons/hicolor"
if [[ -d "$ICONS_SRC" ]]; then
    for size in 32 128; do
        src="$ICONS_SRC/${size}x${size}.png"
        [[ -f "$src" ]] || continue
        dest_dir="$ICON_ROOT/${size}x${size}/apps"
        mkdir -p "$dest_dir"
        install -m 0644 "$src" "$dest_dir/locai-link.png"
    done
    # Tauri names the 256x256 file `128x128@2x.png` (retina naming);
    # hicolor wants it under 256x256/.
    if [[ -f "$ICONS_SRC/128x128@2x.png" ]]; then
        mkdir -p "$ICON_ROOT/256x256/apps"
        install -m 0644 "$ICONS_SRC/128x128@2x.png" "$ICON_ROOT/256x256/apps/locai-link.png"
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "$ICON_ROOT" 2>/dev/null || true
    fi
    log "icons installed to $ICON_ROOT"
else
    log "WARN: no icons source at $ICONS_SRC — menu entries will show a placeholder icon"
fi

# --- Launch Locai Link -----------------------------------------------
# The app opens the setup wizard on first run (no registered device): sign-in,
# models, permissions incl. the "start at login" toggle, Finish. Backgrounded so
# this script returns. On Finish the app activates the services per the toggle.

log "launching Locai Link…"
# WebKit's DMABUF renderer breaks on many Wayland setups (Nvidia, some
# Intel); GDK_BACKEND=x11 sidesteps it. Also set in the companion service.
WEBKIT_DISABLE_DMABUF_RENDERER=1 GDK_BACKEND=x11 \
    nohup "$INSTALL_ROOT/companion" >/dev/null 2>&1 &
disown

cat <<EOF

$LOG_PREFIX Install complete.

Next:
  * The setup window should have opened. Complete the wizard to
    register this device with Control. On Finish, the runtime and
    companion services are enabled + started.
  * "Locai Link" is now in your Applications menu — click to bring
    the tray back up if you ever close it.

Manual controls:
  systemctl --user status locai-link-agent locai-link-companion
  systemctl --user restart locai-link-agent
  ~/.local/share/locai/uninstall.sh                            # remove

EOF
