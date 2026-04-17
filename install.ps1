# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

$ErrorActionPreference = "Stop"

# 1. Check for uv, install if missing
if (-not (Get-Command "uv" -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv package manager..." -ForegroundColor Cyan
    irm https://astral.sh/uv/install.ps1 | iex
    $env:PATH += ";$HOME\.local\bin;$HOME\.cargo\bin"
}

# 2. Determine source (Local vs Remote)
if (Test-Path ".\main.py") {
    Write-Host "Found local main.py" -ForegroundColor Green
    $MainTarget = "main.py"
} else {
    Write-Host "Downloading remote main.py..." -ForegroundColor Cyan
    $MainTarget = "https://raw.githubusercontent.com/locai-co-uk/locai-link/main/main.py"
}

# 3. Launch Installer
Write-Host "Launching Installer..." -ForegroundColor Cyan
uv run $MainTarget install @args
