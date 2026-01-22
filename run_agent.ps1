# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# 1. Install uv if not present
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    irm https://astral.sh/uv/install.ps1 | iex
    
    # Add uv to path for current session
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# 2. Run manager.py using uv
# $args contains all arguments passed to this script
uv run manager.py $args