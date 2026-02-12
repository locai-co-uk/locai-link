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
if (Test-Path ".\manager.py") {
    Write-Host "Found local manager.py" -ForegroundColor Green
    $ManagerTarget = "manager.py"
} else {
    Write-Host "Downloading remote manager.py..." -ForegroundColor Cyan
    $ManagerTarget = "https://raw.githubusercontent.com/locai-co-uk/locai-link/main/manager.py"
}

# 3. Launch Installer
Write-Host "Launching Installer..." -ForegroundColor Cyan
# Invoke-Expression is needed to correctly handle @args passing with uv run in some PS versions
uv run $ManagerTarget install @args
