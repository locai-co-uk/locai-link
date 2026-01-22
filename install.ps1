# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

param (
    # Infrastructure
    [string]$RepoUrl = "https://github.com/locai-co-uk/locai-link.git",
    [string]$InstallDir = Join-Path (Get-Location) "locai-link",
    [string]$Branch = "main"

    # User inputs (Required)
    [string]$DeviceName,
    [string]$Username,
    [string]$RegistrationKey,
    [string]$DeviceType = "edge_device",
    [string]$ApiUrl = "",
)

Write-Host "=== LocAI Edge Agent Installer ===" -ForegroundColor Cyan

# 1. Check/Install Git
try {
    git --version | Out-Null
} catch {
    Write-Error "Git is not installed. Please install Git for Windows first."
    exit 1
}

# 2. Check/Install uv
if (-not (Get-Command "uv" -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found. Installing..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:PATH += ";$HOME\.local\bin;$HOME\.cargo\bin"
} else {
    Write-Host "✔ uv is already installed." -ForegroundColor Green
}

# 3. Update or clone repo
if (Test-Path $InstallDir) {
    Write-Host "Updating repository..."
    Set-Location $InstallDir
    git pull origin $Branch
} else {
    Write-Host "Cloning repository..."
    # CHANGE: Added --depth 1 for shallow clone and --branch
    git clone --depth 1 --branch $Branch $RepoUrl $InstallDir
    Set-Location $InstallDir
}

# 4. Run Setup via uv
Write-Host "Initializing Environment..." -ForegroundColor Cyan
uv run manager.py setup

# 5. Register
if ($DeviceName -and $Username -and $RegistrationKey) {
    Write-Host "Registering device..." -ForegroundColor Cyan
    $ArgsList = @("register", "--device-name", $DeviceName, "--username", $Username, "--registration-key", $RegistrationKey, "--device-type", $DeviceType)
    if ($ApiUrl) { $ArgsList += "--api-url"; $ArgsList += $ApiUrl }

    uv run manager.py $ArgsList

    if ($LASTEXITCODE -eq 0) {
        Write-Host "✔ Device Registered Successfully!" -ForegroundColor Green
        Write-Host "Starting Agent..." -ForegroundColor Cyan
        uv run manager.py run
    } else {
        Write-Error "Registration failed."
    }
} else {
    Write-Warning "Missing registration arguments."
}