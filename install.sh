#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

set -e

# 1. Check for uv, install if missing
if ! command -v uv &> /dev/null; then
    echo "Installing uv package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh > /dev/null
    
    if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; 
    elif [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; 
    else export PATH="$HOME/.local/bin:$PATH"; fi
fi

# 2. Determine source (Local vs Remote)
if [ -f "manager.py" ]; then
    echo "Found local manager.py"
    MANAGER_TARGET="manager.py"
else
    echo "Downloading remote manager.py..."
    MANAGER_TARGET="https://raw.githubusercontent.com/locai-co-uk/locai-link/main/manager.py"
fi

# 3. Launch Installer
echo "Launching Installer..."
uv run "$MANAGER_TARGET" install "$@"
