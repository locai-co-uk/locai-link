#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

set -e

REPO_URL="${LOCAI_REPO_URL:-https://github.com/locai-co-uk/locai-link.git}"
BRANCH="${LOCAI_BRANCH:-main}"

# 1. Check for uv, install if missing
if ! command -v uv &> /dev/null; then
    echo "Installing uv package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh > /dev/null

    if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env";
    elif [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env";
    else export PATH="$HOME/.local/bin:$PATH"; fi
fi

# 2. git is required — main.py is a thin shim that imports `link.*` from ./src,
#    so the full repo must be on disk before we can run it.
if ! command -v git &> /dev/null; then
    echo "Error: git is required for installation but was not found." >&2
    exit 1
fi

# 3. Locate or clone the repo
if [ -f "main.py" ] && [ -d "src/link" ]; then
    echo "Found local repository"
    INSTALL_DIR="$(pwd)"
else
    INSTALL_DIR="$(pwd)/locai-link"
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "Updating existing clone at $INSTALL_DIR..."
        git -C "$INSTALL_DIR" pull --ff-only || true
    else
        echo "Cloning $REPO_URL ($BRANCH) into $INSTALL_DIR..."
        git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
fi

# 4. Launch Installer from inside the repo
echo "Launching Installer..."
cd "$INSTALL_DIR"
uv run main.py install "$@"
