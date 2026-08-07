#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Assemble a release tarball. Desktop is Linux-only here (systemd + .desktop; the
# desktop macOS .pkg is built in release.yml). Headless is OS-agnostic — a flat
# install-root tarball — so this runs on Linux + macOS CI runners for that shape.
#
# Produces the same shape CI produces on tag push, so you can exercise the
# "extract + install.sh" path without cutting a release.
#
# Prereq (run this first in the repo root):
#     uv run python bundling/build.py --shape <desktop|headless> --plugins <plugin-set>
#
# Shape + asset name are read from dist/locai-link/manifest.json (written by
# build.py); pack.sh then builds the matching Rust binary itself (desktop = the
# Tauri app; headless = --no-default-features), so the feature can't diverge from
# the bundle's shape. No separate --shape/--plugins flag here.
#
# Output layout (inside the tarball):
#     locai-link-<shape>-linux-<x64|arm64>-<version>/
#     ├── bundle/                       (dist/locai-link/ + the locai-link binary)
#     ├── boot.json
#     ├── systemd/*.service
#     ├── applications/*.desktop
#     ├── install.sh
#     └── uninstall.sh
#
# Usage:
#     ./bundling/linux/pack.sh                        # → dist/locai-link-<shape>-linux-<arch>-<ver>-DEV.tar.gz
#     ./bundling/linux/pack.sh --output /tmp/foo.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

OUTPUT=""
RELEASE=0
DEV=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output|-o)
            OUTPUT="$2"
            shift 2
            ;;
        --release)
            # Drop the "-DEV" suffix from the asset name (used by CI so
            # release-labelled artefacts don't carry the DEV tag).
            RELEASE=1
            shift
            ;;
        --dev)
            # Bake the dev endpoints into the Rust binary: Control (sign-in +
            # device auth) and the artifact store (on-demand engines). The
            # supervisor forwards the artifact base to the runtime's env.
            DEV=1
            shift
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

log() {
    echo "[pack] $*"
}
err() {
    echo "[pack] ERROR: $*" >&2
    exit 1
}

# --- Locate inputs ----------------------------------------------------

BUNDLE_DIR="$REPO_ROOT/dist/locai-link"
TAURI_DIR="$REPO_ROOT/crates/target/release"
BOOT_JSON="$REPO_ROOT/bundling/boot.json"
MANIFEST="$BUNDLE_DIR/current/manifest.json"

[[ -f "$MANIFEST" ]]  || err "manifest.json not at $MANIFEST — run \`uv run python bundling/build.py --shape <shape> --plugins …\` first."
[[ -f "$BOOT_JSON" ]] || err "boot.json not at $BOOT_JSON."

# --- Derive the asset name + shape from manifest.json ----------------
# manifest.json is the single source of truth (build.py wrote asset_name from
# the shape), so the tarball label AND the Rust feature we build below both come
# from the one shape and can't diverge.

read -r ASSET_STEM VERSION SHAPE < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
print(m["asset_name"], "v" + m["version"], m.get("shape", "desktop"))
' "$MANIFEST")

[[ -n "$ASSET_STEM" && -n "$VERSION" && -n "$SHAPE" ]] || err "manifest.json missing asset_name/version/shape fields"
case "$SHAPE" in
    desktop|headless) ;;
    *) err "unsupported bundle shape: $SHAPE" ;;
esac

# Build the Rust binary with the feature matching the shape — desktop = the Tauri
# app (tray + setup); headless = supervisor only (--no-default-features). Both
# land at $TAURI_DIR/locai-link, so staging can't pick the wrong feature.
if [[ $DEV -eq 1 ]]; then
    export LOCAI_CONTROL_URL="https://dev.control.locai.co.uk"
    export LOCAI_CONTROL_API_URL="https://dev.api.locai.co.uk/api/v1"
    export LOCAI_ARTIFACT_BASE="https://storage.googleapis.com/locai-platform-artifacts-dev"
    log "DEV build — Control=$LOCAI_CONTROL_URL, artifacts=$LOCAI_ARTIFACT_BASE"
fi
# Always clean the crate: the endpoint bake is option_env! (compile time) and
# cargo does not recompile on env-only changes, so a cached binary would keep
# the PREVIOUS pack's endpoints (e.g. a prod pack silently shipping dev URLs).
( cd "$REPO_ROOT/crates" && cargo clean -p locai-link 2>/dev/null || true )
log "building locai-link ($SHAPE)…"
if [[ "$SHAPE" == "headless" ]]; then
    ( cd "$REPO_ROOT/crates" && cargo build -p locai-link --no-default-features --release )
else
    # npm ci first: a fresh checkout has no crates/link/node_modules for the frontend build.
    ( cd "$REPO_ROOT/crates/link" && npm ci && npm run tauri build -- --no-bundle )
fi
[[ -f "$TAURI_DIR/locai-link" ]] || err "locai-link binary not at $TAURI_DIR after build"

case "$(uname -m)" in
    x86_64|amd64)  ARCH="x64" ;;
    arm64|aarch64) ARCH="arm64" ;;
    *) err "unsupported architecture: $(uname -m)" ;;
esac
case "$(uname -s)" in
    Linux)  OS="linux" ;;
    Darwin) OS="macos" ;;
    # Windows would emit locai-link.exe (not handled here); headless Windows is a follow-up.
    *) err "unsupported OS: $(uname -s) (pack.sh does Linux + macOS-headless)" ;;
esac
# Desktop packaging is Linux-only here (systemd + .desktop); the desktop macOS
# .pkg is built in release.yml. Headless is OS-agnostic (flat install-root tarball).
[[ "$SHAPE" == "desktop" && "$OS" != "linux" ]] && err "desktop pack is Linux-only (macOS uses the .pkg in release.yml)"

# Local packs get a -DEV suffix so a hand-built tarball can't be mistaken
# for the canonical CI release output. --release drops the suffix.
if [[ $RELEASE -eq 1 ]]; then
    NAME="${ASSET_STEM}-${OS}-${ARCH}-${VERSION}"
else
    NAME="${ASSET_STEM}-${OS}-${ARCH}-${VERSION}-DEV"
fi
OUTPUT="${OUTPUT:-$REPO_ROOT/dist/${NAME}.tar.gz}"

log "asset name:  $NAME"
log "output:      $OUTPUT"

# --- Stage into a temp dir + tar --------------------------------------

STAGE="$(mktemp -d --tmpdir "locai-pack-XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

ROOT="$STAGE/$NAME"
mkdir -p "$ROOT"

# boot.json is generated from the manifest for both shapes.
gen_boot() {
    python3 "$REPO_ROOT/bundling/gen_boot_json.py" \
        --manifest "$MANIFEST" --template "$BOOT_JSON" --output "$1"
    chmod 0644 "$1"
}

if [[ "$SHAPE" == "headless" ]]; then
    # Flat install-root layout: scripts/install.sh strips the <NAME>/ wrapper and
    # lays these straight into the install root, then does the service setup
    # itself — so no in-tarball install.sh / systemd / icons here.
    cp -a "$BUNDLE_DIR"/. "$ROOT/"
    install -m 0755 "$TAURI_DIR/locai-link" "$ROOT/locai-link"
    gen_boot "$ROOT/boot.json"
else
    # Desktop: bundle/ (laid down by the in-tarball install.sh) + units + icons.
    mkdir -p "$ROOT/bundle"
    cp -a "$BUNDLE_DIR"/. "$ROOT/bundle/"
    install -m 0755 "$TAURI_DIR/locai-link" "$ROOT/bundle/locai-link"
    # App content hashes for whole-app OTA (UI apps); desktop-only.
    python3 "$REPO_ROOT/bundling/inject_app_hashes.py" \
        --manifest "$ROOT/bundle/current/manifest.json" --repo-root "$REPO_ROOT"
    gen_boot "$ROOT/boot.json"
    mkdir -p "$ROOT/systemd" "$ROOT/applications" "$ROOT/icons"
    install -m 0644 "$SCRIPT_DIR/systemd/"*.service         "$ROOT/systemd/"
    install -m 0644 "$SCRIPT_DIR/applications/"*.desktop    "$ROOT/applications/"
    ICONS_SRC="$REPO_ROOT/crates/link/src-tauri/icons"
    for name in 32x32.png 128x128.png 128x128@2x.png; do
        [[ -f "$ICONS_SRC/$name" ]] && install -m 0644 "$ICONS_SRC/$name" "$ROOT/icons/$name"
    done
    install -m 0755 "$SCRIPT_DIR/install.sh"                "$ROOT/install.sh"
    install -m 0755 "$SCRIPT_DIR/uninstall.sh"              "$ROOT/uninstall.sh"
fi

# --- Tar ---------------------------------------------------------------

mkdir -p "$(dirname "$OUTPUT")"
tar -czf "$OUTPUT" -C "$STAGE" "$NAME"

log "wrote $OUTPUT"
log "size: $(du -sh "$OUTPUT" | cut -f1)"
