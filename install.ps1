# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

[CmdletBinding(PositionalBinding=$false)]
param(
    [string]$DeviceName,
    [string]$Email,
    [string]$Password,
    [string]$Token,
    [string]$RegistrationKey,
    [string]$DeviceType,
    [string]$ApiUrl,
    [string]$RepoUrl,
    [string]$Branch,
    [switch]$StartRunning,
    [switch]$Dev,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"

# Resolve repo source — explicit param wins, then env var, then default.
if (-not $RepoUrl) { $RepoUrl = if ($env:LOCAI_REPO_URL) { $env:LOCAI_REPO_URL } else { "https://github.com/locai-co-uk/locai-link.git" } }
if (-not $Branch)  { $Branch  = if ($env:LOCAI_BRANCH)   { $env:LOCAI_BRANCH }   else { "main" } }

# Translate PowerShell-style params into argparse kebab-case for `main.py install`.
$InstallArgs = @()
if ($DeviceName)      { $InstallArgs += @("--device-name",      $DeviceName) }
if ($Email)           { $InstallArgs += @("--email",            $Email) }
if ($Password)        { $InstallArgs += @("--password",         $Password) }
if ($Token)           { $InstallArgs += @("--token",            $Token) }
if ($RegistrationKey) { $InstallArgs += @("--registration-key", $RegistrationKey) }
if ($DeviceType)      { $InstallArgs += @("--device-type",      $DeviceType) }
if ($ApiUrl)          { $InstallArgs += @("--api-url",          $ApiUrl) }
if ($StartRunning)    { $InstallArgs += "--start-running" }
if ($Dev)             { $InstallArgs += "--dev" }
# Anything else (e.g. raw `--flag value` pairs) passes through untouched.
if ($RemainingArgs)   { $InstallArgs += $RemainingArgs }

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
    $Base = (Get-Location).Path
    $SystemRoots = @($env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($root in $SystemRoots) {
        if ($Base.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Host "Current directory ($Base) is a system path - installing to $HOME instead." -ForegroundColor Yellow
            $Base = $HOME
            break
        }
    }
    $InstallDir = Join-Path $Base "locai-link"
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
    uv run main.py install @InstallArgs
} finally {
    Pop-Location
}
