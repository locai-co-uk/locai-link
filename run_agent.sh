#!/bin/bash
# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

set -e

# 1. Install uv if not present (no system python required)
if ! command -v uv &> /dev/null; then
    echo "uv not found. Installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    
    # Source the env to make uv available in this session immediately
    if [ -f "$HOME/.local/bin/env" ]; then
        . "$HOME/.local/bin/env"
    elif [ -f "$HOME/.cargo/env" ]; then
        . "$HOME/.cargo/env"
    else
        # Fallback: add standard location to PATH manually for this run
        export PATH="$HOME/.local/bin:$PATH"
    fi
fi

# 2. Run manager.py using uv
# 'uv run' will automatically download a Python interpreter if one is missing
# "$@" passes all arguments from this script to manager.py
uv run manager.py "$@"