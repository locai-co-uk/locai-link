#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Locai Link installer for Linux (per-user, no sudo).
#
# Lays down the single locai-link binary + bundled runtime, boot.json, and the
# service unit under ~/.local/share/locai/. Service activation is deferred
# to the setup wizard on Finish (per the "Start at login" toggle).
#
# Layout after install:
#
#     ~/.local/share/locai/
#     ├── locai-link                     (single binary: supervisor + tray + setup)
#     ├── versions/vX.Y.Z/…              (PyInstaller runtime + plugins)
#     ├── current -> versions/vX.Y.Z     (OTA-swappable symlink)
#     ├── manifest.json                  (from the runtime bundle)
#     ├── boot.json                      (channel config)
#     ├── configs/                       (session state — setup writes here)
#     ├── logs/                          (runtime + tray stdout/stderr)
#     ├── systemd/*.service              (staged; activated on Finish)
#     └── uninstall.sh
#
#     ~/.config/systemd/user/locai-link-companion.service
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

BUNDLE_DIR=""      # versions/ + current + manifest (+ the locai-link binary in a tarball)
MERGED_BIN=""      # repo-checkout only: the locai-link binary to install separately
UNITS_DIR=""       # locai-link-companion.service (the single unit)
DESKTOPS_DIR=""    # locai-link.desktop
ICONS_SRC=""       # dir with 32x32.png / 128x128.png / 128x128@2x.png
BOOT_JSON=""

resolve_paths() {
    # Case 1 — extracted tarball: install.sh sits next to bundle/ (which already
    # contains the merged `locai-link` binary) + boot.json + systemd/ +
    # applications/ + icons/. Fall through if no match.
    if [[ -d "$SCRIPT_DIR/bundle" && -f "$SCRIPT_DIR/bundle/locai-link" ]]; then
        BUNDLE_DIR="$SCRIPT_DIR/bundle"
        UNITS_DIR="$SCRIPT_DIR/systemd"
        DESKTOPS_DIR="$SCRIPT_DIR/applications"
        ICONS_SRC="$SCRIPT_DIR/icons"
        BOOT_JSON="$SCRIPT_DIR/boot.json"
        return
    fi

    # Case 2 — local repo checkout. Expects `build.py` (runtime bundle under
    # dist/locai-link/) + `cargo tauri build --no-bundle` (the locai-link binary).
    local repo_root
    repo_root="$(cd "$SCRIPT_DIR/../.." && pwd)"
    if [[ -d "$repo_root/dist/locai-link/versions" && -f "$repo_root/crates/target/release/locai-link" ]]; then
        BUNDLE_DIR="$repo_root/dist/locai-link"
        MERGED_BIN="$repo_root/crates/target/release/locai-link"
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
log "locai-link binary:  ${MERGED_BIN:-$BUNDLE_DIR/locai-link}"
log "systemd units:      $UNITS_DIR"
log "desktop entries:    $DESKTOPS_DIR"
log "icons source:       $ICONS_SRC"

# --- Sanity checks ----------------------------------------------------

command -v systemctl >/dev/null 2>&1 || err "systemctl not found — this installer requires systemd."

# The merged binary is either in the bundle (tarball) or built separately (repo).
if [[ -n "$MERGED_BIN" ]]; then
    [[ -f "$MERGED_BIN" ]] || err "locai-link binary not at $MERGED_BIN"
else
    [[ -f "$BUNDLE_DIR/locai-link" ]] || err "locai-link binary not at $BUNDLE_DIR/locai-link"
fi
[[ -f "$BOOT_JSON" ]]    || err "boot.json not at $BOOT_JSON"
[[ -d "$UNITS_DIR" ]]    || err "systemd units dir not at $UNITS_DIR"
[[ -d "$DESKTOPS_DIR" ]] || err ".desktop dir not at $DESKTOPS_DIR"

# --- Layout: create install root + copy payload -----------------------

mkdir -p "$INSTALL_ROOT/configs" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/systemd"

# 1. Runtime bundle: versions/ + current + manifest (+ locai-link in a tarball).
# `cp -a …/.` preserves the `current` symlink and copies CONTENTS into
# INSTALL_ROOT rather than nesting a locai-link/ subdir.
cp -a "$BUNDLE_DIR"/. "$INSTALL_ROOT"/
log "runtime bundle copied to $INSTALL_ROOT"

# 2. Repo checkout: the locai-link binary is built separately, so install it.
# (In a tarball it already rode in via bundle/ above.)
if [[ -n "$MERGED_BIN" ]]; then
    install -m 0755 "$MERGED_BIN" "$INSTALL_ROOT/locai-link"
fi

# 3. boot.json — channel config, read by the app on first start.
install -m 0644 "$BOOT_JSON" "$INSTALL_ROOT/boot.json"

# 4. Uninstaller (invoked by the tray Preferences → Advanced button
# and by hand from the terminal).
install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$INSTALL_ROOT/uninstall.sh"

# LEGACY-SA-CLEANUP: drop a pre-merge standalone setup-assistant binary left by
# an older install (onboarding is now part of the companion). Remove once no
# pre-merge install remains.
rm -f "$INSTALL_ROOT/setup-assistant"

log "locai-link binary + boot.json + uninstall.sh installed"

# --- systemd unit (staged, not activated) -----------------------------
# Stage the single .service under $INSTALL_ROOT/systemd/; the app copies it into
# the user's systemd domain on Finish, per the "Start at login" toggle. Staged
# (not written to ~/.config/systemd/user/ now) so the toggle controls behaviour —
# enabling at install time would ignore it.
install -m 0644 "$UNITS_DIR/locai-link-companion.service" "$INSTALL_ROOT/systemd/"
log "systemd unit staged at $INSTALL_ROOT/systemd/"

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
# Intel); GDK_BACKEND=x11 sidesteps it. Also set in the unit.
WEBKIT_DISABLE_DMABUF_RENDERER=1 GDK_BACKEND=x11 \
    nohup "$INSTALL_ROOT/locai-link" >/dev/null 2>&1 &
disown

cat <<EOF

$LOG_PREFIX Install complete.

Next:
  * The setup window should have opened. Complete the wizard to
    register this device with Control. On Finish, the service is
    enabled + started.
  * "Locai Link" is now in your Applications menu — click to bring
    the tray back up if you ever close it.

Manual controls:
  systemctl --user status locai-link-companion
  systemctl --user restart locai-link-companion
  ~/.local/share/locai/uninstall.sh                            # remove

EOF
