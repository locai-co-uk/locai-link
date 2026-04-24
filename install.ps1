# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:LOCAI_REPO_URL) { $env:LOCAI_REPO_URL } else { "https://github.com/locai-co-uk/locai-link.git" }
$Branch  = if ($env:LOCAI_BRANCH)   { $env:LOCAI_BRANCH }   else { "main" }

# 1. Check for uv, install if missing
if (-not (Get-Command "uv" -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv package manager..." -ForegroundColor Cyan
    irm https://astral.sh/uv/install.ps1 | iex
    $env:PATH += ";$HOME\.local\bin;$HOME\.cargo\bin"
}

# 2. git is required — main.py is a thin shim that imports `link.*` from .\src,
#    so the full repo must be on disk before we can run it.
if (-not (Get-Command "git" -ErrorAction SilentlyContinue)) {
    Write-Host "Error: git is required for installation but was not found." -ForegroundColor Red
    exit 1
}

# 3. Locate or clone the repo
if ((Test-Path ".\main.py") -and (Test-Path ".\src\link")) {
    Write-Host "Found local repository" -ForegroundColor Green
    $InstallDir = (Get-Location).Path
} else {
    $InstallDir = Join-Path (Get-Location).Path "locai-link"
    if (Test-Path (Join-Path $InstallDir ".git")) {
        Write-Host "Updating existing clone at $InstallDir..." -ForegroundColor Cyan
        git -C $InstallDir pull --ff-only
    } else {
        Write-Host "Cloning $RepoUrl ($Branch) into $InstallDir..." -ForegroundColor Cyan
        git clone --depth 1 -b $Branch $RepoUrl $InstallDir
    }
}

# 4. Launch Installer from inside the repo
Write-Host "Launching Installer..." -ForegroundColor Cyan
Push-Location $InstallDir
try {
    uv run main.py install @args
} finally {
    Pop-Location
}
