#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Locally assemble the Linux release tarball.
#
# Produces the same shape CI will produce on tag push, so you can
# exercise the "extract + install.sh" path without cutting a release.
#
# Prereqs (run these first in the repo root):
#     uv run python bundling/build.py --plugins <plugin-set>
#     ( cd crates/setup_assistant && npm run tauri build -- --no-bundle )
#     ( cd crates/companion       && npm run tauri build -- --no-bundle )
#
# Plugin selection is picked up from dist/locai-link/manifest.json
# (written by build.py). Whatever plugins ended up in the bundle are
# what the tarball is labelled for — no separate --plugins flag here.
#
# Output layout (inside the tarball):
#     locai-link-<code>-linux-x86_64-<version>/
#     ├── bundle/                       (contents of dist/locai-link/)
#     ├── setup-assistant               (Tauri release binary)
#     ├── companion                     (Tauri release binary)
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
            # Strip the "-DEV" suffix from the asset name — used by CI so
            # release-labelled artefacts don't carry the DEV tag.
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

[[ -f "$BUNDLE_DIR/locai-link" ]]                || err "runtime bundle not at $BUNDLE_DIR — run \`uv run python bundling/build.py --plugins …\` first."
[[ -f "$MANIFEST" ]]                             || err "manifest.json not at $MANIFEST — did build.py finish?"
[[ -f "$TAURI_DIR/locai-link-setup-assistant" ]] || err "setup-assistant binary not at $TAURI_DIR — run \`cargo tauri build --no-bundle\` in crates/setup_assistant."
[[ -f "$TAURI_DIR/locai-link-companion" ]]       || err "companion binary not at $TAURI_DIR — run \`cargo tauri build --no-bundle\` in crates/companion."
[[ -f "$BOOT_JSON" ]]                            || err "boot.json not at $BOOT_JSON."

# --- Derive the asset name from manifest.json ------------------------
# manifest.json is the single source of truth for what's in the bundle
# — build.py wrote it based on the plugin set it just compiled. Reading
# it here means the tarball label can't diverge from the bundle contents
# (previous flag-based flow could mislabel if pack.sh's --plugins list
# didn't match the one passed to build.py).

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

# 1. Runtime bundle (launcher + versions/ + current + manifest etc.)
mkdir -p "$ROOT/bundle"
cp -a "$BUNDLE_DIR"/. "$ROOT/bundle/"

# 2. Tauri release binaries.
install -m 0755 "$TAURI_DIR/locai-link-setup-assistant" "$ROOT/setup-assistant"
install -m 0755 "$TAURI_DIR/locai-link-companion"       "$ROOT/companion"

# 3. boot.json + systemd + .desktop entries + icons + install/uninstall.
# plugin_set is injected from manifest.json so the launcher's first-launch
# fetch targets the asset this build actually is (not the static template).
python3 "$REPO_ROOT/bundling/gen_boot_json.py" \
    --manifest "$MANIFEST" --template "$BOOT_JSON" --output "$ROOT/boot.json"
chmod 0644 "$ROOT/boot.json"
mkdir -p "$ROOT/systemd" "$ROOT/applications" "$ROOT/icons"
install -m 0644 "$SCRIPT_DIR/systemd/"*.service         "$ROOT/systemd/"
install -m 0644 "$SCRIPT_DIR/applications/"*.desktop    "$ROOT/applications/"
# Icons come from the SA crate — companion has the same brand set. The
# install.sh reshuffles these into hicolor sizes at install time.
ICONS_SRC="$REPO_ROOT/crates/setup_assistant/src-tauri/icons"
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
