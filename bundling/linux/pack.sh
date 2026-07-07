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
#     uv run python bundling/build.py --plugins language_model audio_transcriber
#     ( cd crates/setup_assistant && npm run tauri build -- --no-bundle )
#     ( cd crates/companion       && npm run tauri build -- --no-bundle )
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
#     ./bundling/linux/pack.sh                        # → dist/locai-link-<code>-linux-x86_64-<ver>.tar.gz
#     ./bundling/linux/pack.sh --plugins language_model
#     ./bundling/linux/pack.sh --output /tmp/foo.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PLUGINS=(language_model audio_transcriber)
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --plugins)
            shift
            PLUGINS=()
            while [[ $# -gt 0 && "$1" != --* ]]; do
                PLUGINS+=("$1")
                shift
            done
            ;;
        --output|-o)
            OUTPUT="$2"
            shift 2
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

[[ -f "$BUNDLE_DIR/locai-link" ]]              || err "runtime bundle not at $BUNDLE_DIR — run \`uv run python bundling/build.py --plugins ${PLUGINS[*]}\` first."
[[ -f "$TAURI_DIR/locai-link-setup-assistant" ]] || err "setup-assistant binary not at $TAURI_DIR — run \`cargo tauri build --no-bundle\` in crates/setup_assistant."
[[ -f "$TAURI_DIR/locai-link-companion" ]]       || err "companion binary not at $TAURI_DIR — run \`cargo tauri build --no-bundle\` in crates/companion."
[[ -f "$BOOT_JSON" ]]                            || err "boot.json not at $BOOT_JSON."

# --- Derive the asset name -------------------------------------------
# Reuses bundling/manifest.py so the naming stays in lockstep with CI
# (and with what release-assets.yml uploads).

ASSET_STEM="$(cd "$REPO_ROOT" && uv run python -c '
import sys
sys.path.insert(0, "bundling")
from manifest import derive_asset_name
print(derive_asset_name(sys.argv[1:]))
' "${PLUGINS[@]}")"

VERSION="$(cd "$REPO_ROOT" && uv run python -c '
import tomllib, pathlib
data = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
print("v" + data["project"]["version"])
')"

# Include -DEV suffix when packing outside CI so we don't ever ship a
# locally-built artefact with a canonical release name.
NAME="${ASSET_STEM}-linux-x86_64-${VERSION}-DEV"
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

# 3. boot.json + systemd + .desktop entries + install/uninstall.
install -m 0644 "$BOOT_JSON"                            "$ROOT/boot.json"
mkdir -p "$ROOT/systemd" "$ROOT/applications"
install -m 0644 "$SCRIPT_DIR/systemd/"*.service         "$ROOT/systemd/"
install -m 0644 "$SCRIPT_DIR/applications/"*.desktop    "$ROOT/applications/"
install -m 0755 "$SCRIPT_DIR/install.sh"                "$ROOT/install.sh"
install -m 0755 "$SCRIPT_DIR/uninstall.sh"              "$ROOT/uninstall.sh"

# --- Tar ---------------------------------------------------------------

mkdir -p "$(dirname "$OUTPUT")"
tar -czf "$OUTPUT" -C "$STAGE" "$NAME"

log "wrote $OUTPUT"
log "size: $(du -sh "$OUTPUT" | cut -f1)"
