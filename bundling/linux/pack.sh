#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Locally assemble the Linux release tarball.
#
# Produces the same shape CI produces on tag push, so you can exercise the
# "extract + install.sh" path without cutting a release.
#
# Prereqs (run these first in the repo root):
#     uv run python bundling/build.py --plugins <plugin-set>
#     ( cd crates/link && npm run tauri build -- --no-bundle )
#
# Plugin selection is read from dist/locai-link/manifest.json (written by
# build.py) — the tarball is labelled for whatever plugins the bundle has;
# no separate --plugins flag here.
#
# Output layout (inside the tarball):
#     locai-link-<code>-linux-x86_64-<version>/
#     ├── bundle/                       (dist/locai-link/ + the locai-link binary)
#     ├── boot.json
#     ├── systemd/*.service
#     ├── applications/*.desktop
#     ├── install.sh
#     └── uninstall.sh
#
# Usage:
#     ./bundling/linux/pack.sh                        # → dist/locai-link-<code>-linux-x86_64-<ver>-DEV.tar.gz
#     ./bundling/linux/pack.sh --output /tmp/foo.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

OUTPUT=""
RELEASE=0

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
BOOT_JSON="$REPO_ROOT/bundling/pkg/boot.json"
MANIFEST="$BUNDLE_DIR/current/manifest.json"

[[ -f "$MANIFEST" ]]             || err "manifest.json not at $MANIFEST — run \`uv run python bundling/build.py --plugins …\` first."
[[ -f "$TAURI_DIR/locai-link" ]] || err "locai-link binary not at $TAURI_DIR — run \`cargo tauri build --no-bundle\` in crates/link."
[[ -f "$BOOT_JSON" ]]            || err "boot.json not at $BOOT_JSON."

# --- Derive the asset name from manifest.json ------------------------
# manifest.json is the single source of truth for bundle contents (build.py
# wrote it from the compiled plugin set), so the tarball label can't diverge
# from what's inside — a separate flag-based list could mislabel.

read -r ASSET_STEM VERSION < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
print(m["asset_name"], "v" + m["version"])
' "$MANIFEST")

[[ -n "$ASSET_STEM" && -n "$VERSION" ]] || err "manifest.json missing asset_name/version fields"

# Local packs get a -DEV suffix so a hand-built tarball can't be mistaken
# for the canonical CI release output. --release drops the suffix.
if [[ $RELEASE -eq 1 ]]; then
    NAME="${ASSET_STEM}-linux-x86_64-${VERSION}"
else
    NAME="${ASSET_STEM}-linux-x86_64-${VERSION}-DEV"
fi
OUTPUT="${OUTPUT:-$REPO_ROOT/dist/${NAME}.tar.gz}"

log "asset name:  $NAME"
log "output:      $OUTPUT"

# --- Stage into a temp dir + tar --------------------------------------

STAGE="$(mktemp -d --tmpdir "locai-pack-XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

ROOT="$STAGE/$NAME"
mkdir -p "$ROOT"

# 1. Runtime bundle (versions/ + current + manifest) from build.py.
mkdir -p "$ROOT/bundle"
cp -a "$BUNDLE_DIR"/. "$ROOT/bundle/"

# 2. The single `locai-link` binary (supervisor + tray) lives at the install
# root; stage it inside bundle/ so install.sh's `cp -a bundle/.` lays it down.
install -m 0755 "$TAURI_DIR/locai-link" "$ROOT/bundle/locai-link"

# 2b. App content hashes for whole-app OTA — written to the tarball's
# manifest so swap_bundle re-swaps a UI app only when its source changed.
python3 "$REPO_ROOT/bundling/inject_app_hashes.py" \
    --manifest "$ROOT/bundle/current/manifest.json" --repo-root "$REPO_ROOT"

# 3. boot.json + systemd + .desktop entries + icons + install/uninstall.
# plugin_set is injected from manifest.json so the launcher's first-launch
# fetch targets this build's asset, not the static template.
python3 "$REPO_ROOT/bundling/gen_boot_json.py" \
    --manifest "$MANIFEST" --template "$BOOT_JSON" --output "$ROOT/boot.json"
chmod 0644 "$ROOT/boot.json"
mkdir -p "$ROOT/systemd" "$ROOT/applications" "$ROOT/icons"
install -m 0644 "$SCRIPT_DIR/systemd/"*.service         "$ROOT/systemd/"
install -m 0644 "$SCRIPT_DIR/applications/"*.desktop    "$ROOT/applications/"
# Icons come from the companion crate. The install.sh reshuffles these into
# hicolor sizes at install time.
ICONS_SRC="$REPO_ROOT/crates/link/src-tauri/icons"
for name in 32x32.png 128x128.png 128x128@2x.png; do
    [[ -f "$ICONS_SRC/$name" ]] && install -m 0644 "$ICONS_SRC/$name" "$ROOT/icons/$name"
done
install -m 0755 "$SCRIPT_DIR/install.sh"                "$ROOT/install.sh"
install -m 0755 "$SCRIPT_DIR/uninstall.sh"              "$ROOT/uninstall.sh"

# --- Tar ---------------------------------------------------------------

mkdir -p "$(dirname "$OUTPUT")"
tar -czf "$OUTPUT" -C "$STAGE" "$NAME"

log "wrote $OUTPUT"
log "size: $(du -sh "$OUTPUT" | cut -f1)"
