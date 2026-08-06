#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Locai Link installer for Linux (per-user, no sudo).
#
# Lays down the single locai-link binary + bundled runtime, boot.json, and the
# service unit under ~/.local/share/locai/, then starts the companion unit so the
# setup wizard opens. Autostart (`systemctl --user enable`) is deferred to the
# wizard on Finish (per the "Start at login" toggle); the unit runs regardless.
#
# Layout after install:
#
#     ~/.local/share/locai/
#     ├── locai-link                     (single binary: supervisor + tray + setup)
#     ├── versions/vX.Y.Z/…              (PyInstaller runtime + plugins)
#     ├── current -> versions/vX.Y.Z     (OTA-swappable symlink)
#     ├── manifest.json                  (from the runtime bundle)
#     ├── boot.json                      (channel config)
#     ├── configs/                       (session state; setup writes here)
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

# --apply lays the payload unconditionally: fresh install, the pre-merge
# migration self-heal, and the splash's "Update" all pass it. Without it, a
# re-run over an already-registered merged install is non-destructive — it
# stages the payload and shows the manage splash so the user chooses to update.
APPLY=0
for arg in "$@"; do
    [[ "$arg" == "--apply" ]] && APPLY=1
done

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
IS_TARBALL=0       # 1 when the payload is a self-contained extracted release

resolve_paths() {
    # Case 1 (extracted tarball): install.sh sits next to bundle/ (which already
    # contains the merged `locai-link` binary) + boot.json + systemd/ +
    # applications/ + icons/. Fall through if no match.
    if [[ -d "$SCRIPT_DIR/bundle" && -f "$SCRIPT_DIR/bundle/locai-link" ]]; then
        BUNDLE_DIR="$SCRIPT_DIR/bundle"
        UNITS_DIR="$SCRIPT_DIR/systemd"
        DESKTOPS_DIR="$SCRIPT_DIR/applications"
        ICONS_SRC="$SCRIPT_DIR/icons"
        BOOT_JSON="$SCRIPT_DIR/boot.json"
        IS_TARBALL=1
        return
    fi

    # Case 2 (local repo checkout). Expects `build.py` (runtime bundle under
    # dist/locai-link/) + `cargo tauri build --no-bundle` (the locai-link binary).
    local repo_root
    repo_root="$(cd "$SCRIPT_DIR/../.." && pwd)"
    if [[ -d "$repo_root/dist/locai-link/versions" && -f "$repo_root/crates/target/release/locai-link" ]]; then
        BUNDLE_DIR="$repo_root/dist/locai-link"
        MERGED_BIN="$repo_root/crates/target/release/locai-link"
        UNITS_DIR="$SCRIPT_DIR/systemd"
        DESKTOPS_DIR="$SCRIPT_DIR/applications"
        ICONS_SRC="$repo_root/crates/link/src-tauri/icons"
        BOOT_JSON="$repo_root/bundling/boot.json"
        return
    fi

    err "couldn't locate build artefacts. Either extract a release tarball and run its install.sh, or from a repo checkout run \`uv run python bundling/build.py --plugins language_model audio_transcriber\` + \`cargo tauri build --no-bundle\` in crates/link first."
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

# --- Mode: apply now, or defer to the manage splash -------------------

existing_registered() {
    compgen -G "$INSTALL_ROOT/configs/session_*.json" >/dev/null 2>&1
}
# A pre-merge install must migrate (apply), not defer — its old UI can't show the
# new splash, and the transition is a hard boundary.
is_pre_merge() {
    [[ -e "$INSTALL_ROOT/companion" || -e "$INSTALL_ROOT/setup-assistant" \
       || -e "$HOME/.config/systemd/user/locai-link-agent.service" ]]
}

defer_to_splash() {
    # A registered merged install is already running; don't clobber it. Stage the
    # payload so the app can apply it on the user's say-so, record the incoming
    # version, drop the manage marker, and restart so the app opens the splash
    # (Update / Open Preferences / Uninstall).
    local incoming pending
    incoming="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' \
        "$BUNDLE_DIR/current/manifest.json" 2>/dev/null || echo unknown)"
    pending="$INSTALL_ROOT/pending"
    rm -rf "$pending"
    mkdir -p "$pending" "$INSTALL_ROOT/state"
    cp -a "$SCRIPT_DIR"/. "$pending"/   # bundle/ + install.sh + systemd/ + applications/ + icons/ + boot.json
    printf '%s' "$incoming" > "$INSTALL_ROOT/state/pending-version"
    : > "$INSTALL_ROOT/state/show-manage-on-start"
    log "existing install found; staged v$incoming to pending/ (not applied)"
    # Restart so the running app re-reads the marker and shows the manage splash.
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user restart locai-link-companion.service 2>/dev/null || true
    log "restarted locai-link-companion; the app will show the manage screen."
}

# Defer only for a self-contained release re-run over a registered merged install.
# Everything else applies: fresh install, --apply (self-heal / splash Update),
# a pre-merge migration, or a repo checkout (dev iteration).
if [[ $APPLY -eq 0 && $IS_TARBALL -eq 1 ]] && existing_registered && ! is_pre_merge; then
    defer_to_splash
    exit 0
fi

# --- Layout: create install root + copy payload -----------------------

mkdir -p "$INSTALL_ROOT/configs" "$INSTALL_ROOT/logs" "$INSTALL_ROOT/systemd"

# 1. Runtime bundle: versions/ + current + manifest (+ locai-link in a tarball).
# `cp -a …/.` preserves the `current` symlink and copies CONTENTS into
# INSTALL_ROOT rather than nesting a locai-link/ subdir. `--remove-destination`
# unlinks each target first so an upgrade over a *running* locai-link / runtime /
# engine binary doesn't fail with ETXTBSY ("Text file busy") — unlinking a busy
# exe is allowed (the live process keeps the old inode; a fresh file lands here).
cp -a --remove-destination "$BUNDLE_DIR"/. "$INSTALL_ROOT"/
log "runtime bundle copied to $INSTALL_ROOT"

# 2. Repo checkout: the locai-link binary is built separately, so install it.
# (In a tarball it already rode in via bundle/ above.)
if [[ -n "$MERGED_BIN" ]]; then
    # rm first: can't overwrite a running locai-link in place (ETXTBSY).
    rm -f "$INSTALL_ROOT/locai-link"
    install -m 0755 "$MERGED_BIN" "$INSTALL_ROOT/locai-link"
fi

# 3. boot.json: channel config, read by the app on first start.
install -m 0644 "$BOOT_JSON" "$INSTALL_ROOT/boot.json"

# 4. Uninstaller (invoked by the tray Preferences → Advanced button
# and by hand from the terminal).
install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$INSTALL_ROOT/uninstall.sh"

# LEGACY-SA-CLEANUP: drop the pre-merge standalone UI binaries left by an older
# install (onboarding + tray are now the one merged binary). Remove once no
# pre-merge install remains.
rm -f "$INSTALL_ROOT/setup-assistant"
rm -f "$INSTALL_ROOT/companion"

log "locai-link binary + boot.json + uninstall.sh installed"

# --- systemd unit (staged, not activated) -----------------------------
# Stage the single .service under $INSTALL_ROOT/systemd/; the app copies it into
# the user's systemd domain on Finish, per the "Start at login" toggle. Staged
# (not written to ~/.config/systemd/user/ now) so the toggle controls behaviour;
# enabling at install time would ignore it.
install -m 0644 "$UNITS_DIR/locai-link-companion.service" "$INSTALL_ROOT/systemd/"
log "systemd unit staged at $INSTALL_ROOT/systemd/"

# --- .desktop entries -------------------------------------------------
# Menu integration so "Locai Link" is discoverable in the app launcher.
# Exec= is `systemctl --user start locai-link-companion.service`,
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

# --- Launch Locai Link (via the service) ------------------------------
# Install + start the single unit NOW so systemd owns the one process and
# `Restart=` keeps it alive. This is the ONLY launch path (no nohup) so there's
# never a second, un-managed instance to fight over (that orphaned the
# supervisor before). The app opens the setup wizard on first run (no registered
# device); the supervisor idles until Finish registers + re-arms it. The setup
# wizard's "start at login" toggle controls `enable` (autostart) on Finish; the
# unit is already running regardless.
log "installing + starting locai-link-companion.service…"
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 "$INSTALL_ROOT/systemd/locai-link-companion.service" \
    "$HOME/.config/systemd/user/locai-link-companion.service"
# LEGACY-SA-CLEANUP: pre-merge separate agent unit (the merged binary supervises
# now). Disable it AND remove the unit file so an upgrade-in-place leaves one unit.
systemctl --user disable --now locai-link-agent.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/locai-link-agent.service"
# `systemctl --user` needs a user D-Bus session; over SSH without linger, or in a
# container, it fails. Guard it so a completed install isn't reported as failed
# under set -e — every artefact is already on disk by this point.
# `restart` (not start) so an upgrade over a running old companion unit actually
# picks up the new ExecStart (start is a no-op on an already-active unit); on a
# fresh install the unit isn't running, so restart just starts it.
if ! systemctl --user daemon-reload 2>/dev/null \
   || ! systemctl --user restart locai-link-companion.service; then
    log "WARNING: couldn't reach the user systemd session (no D-Bus?)."
    log "         Log in graphically, or run:"
    log "           loginctl enable-linger \$USER"
    log "           systemctl --user restart locai-link-companion.service"
fi

# Applied: clear any staged pending payload + manage markers, unless we're running
# FROM pending (the splash's Update) — deleting our own dir mid-run would break
# bash, so the app clears pending after this process exits.
case "$SCRIPT_DIR" in
    "$INSTALL_ROOT/pending"*) : ;;
    *)
        rm -rf "$INSTALL_ROOT/pending" 2>/dev/null || true
        rm -f "$INSTALL_ROOT/state/pending-version" \
              "$INSTALL_ROOT/state/show-manage-on-start" \
              "$INSTALL_ROOT/state/legacy-heal-attempted" 2>/dev/null || true
        ;;
esac

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
