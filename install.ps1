# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

param (
    [string]$RepoUrl = "https://github.com/locai-co-uk/locai-link.git",
    [string]$Branch = "main",
    [string]$DeviceName,
    [string]$Username,
    [string]$RegistrationKey,
    [string]$DeviceType = "edge_device",
    [string]$ApiUrl = "",
    [switch]$StartRunning
)

$PythonVersion = "3.11.8"

Write-Host "=== LocAI Edge Agent Installer ===" -ForegroundColor Cyan

# --- Check if already inside the repository ---
if (Test-Path ".\pyproject.toml") {
    Write-Host "Detected running inside repository." -ForegroundColor Green
    $InstallDir = Get-Location
    $SkipGit = $true
} else {
    $InstallDir = Join-Path (Get-Location) "locai-link"
    $SkipGit = $false
}

# --- Interactive Prompts ---
if ([string]::IsNullOrWhiteSpace($DeviceName)) { $DeviceName = Read-Host "Enter Device Name" }
if ([string]::IsNullOrWhiteSpace($Username)) { $Username = Read-Host "Enter Username" }
if ([string]::IsNullOrWhiteSpace($RegistrationKey)) { $RegistrationKey = Read-Host "Enter Registration Key" }

# 1. Check/Install Git
if (-not $SkipGit) {
    try { git --version | Out-Null } catch {
        Write-Error "Git is not installed. Please install Git for Windows first."
        exit 1
    }
}

# 2. Check/Install uv
if (-not (Get-Command "uv" -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found. Installing..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:PATH += ";$HOME\.local\bin;$HOME\.cargo\bin"
} else {
    Write-Host "✔ uv is already installed." -ForegroundColor Green
}

# 3. Clone/Update Repo
if (-not $SkipGit) {
    if (Test-Path $InstallDir) {
        Write-Host "Updating repository..."
        Set-Location $InstallDir
        git pull origin $Branch
    } else {
        Write-Host "Cloning repository..."
        git clone --depth 1 --branch $Branch $RepoUrl $InstallDir
        Set-Location $InstallDir
    }
} else {
    Set-Location $InstallDir
}

# --- Load Defaults ---
$DefaultsPath = Join-Path $InstallDir "defaults.env"
$EnvDefaults = @{}
if (Test-Path $DefaultsPath) {
    Get-Content $DefaultsPath | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $parts = $line.Split("=", 2)
            $EnvDefaults[$parts[0].Trim()] = $parts[1].Trim()
        }
    }
}
$DefaultProdUrl = if ($EnvDefaults["DEFAULT_API_URL"]) { $EnvDefaults["DEFAULT_API_URL"] } else { "https://api.locai.co.uk/api/v1" }
$DefaultLocalUrl = if ($EnvDefaults["LOCAL_API_URL"]) { $EnvDefaults["LOCAL_API_URL"] } else { "http://localhost:8001/api/v1" }

# --- API URL Selection ---
if ([string]::IsNullOrWhiteSpace($ApiUrl)) {
    Write-Host "`nSelect API Environment:" -ForegroundColor Cyan
    Write-Host "1) Production ($DefaultProdUrl)"
    Write-Host "2) Localhost ($DefaultLocalUrl)"
    Write-Host "3) Custom URL"
    $ApiChoice = Read-Host "Choice [1]"
    switch ($ApiChoice) {
        "2" { $ApiUrl = $DefaultLocalUrl }
        "3" { $ApiUrl = Read-Host "Enter Custom API URL" }
        Default { $ApiUrl = $DefaultProdUrl }
    }
}

# 4. Environment Setup (Python 3.11.8 & LlamaCPP)
Write-Host "`nInitializing Environment (Python $PythonVersion)..." -ForegroundColor Cyan

# Install specific python version
uv python install $PythonVersion
# Recreate venv to ensure clean state
if (Test-Path ".venv") { Remove-Item ".venv" -Recurse -Force }
uv venv --python $PythonVersion

# --- Install Llama-CPP-Python (Windows Logic) ---
Write-Host "Checking GPU availability..."
if (Get-Command "nvidia-smi" -ErrorAction SilentlyContinue) {
    Write-Host "✔ NVIDIA GPU detected. Installing CUDA-enabled inference engine." -ForegroundColor Green
    # Using pre-built wheels for Windows + CUDA 12.1 compatibility
    $LlamaUrl = "https://abetlen.github.io/llama-cpp-python/whl/cu121"
} else {
    Write-Host "⚠ No NVIDIA GPU detected. Installing CPU inference engine." -ForegroundColor Yellow
    $LlamaUrl = "https://abetlen.github.io/llama-cpp-python/whl/cpu"
}

Write-Host "Installing llama-cpp-python from $LlamaUrl..."
uv pip install llama-cpp-python --extra-index-url $LlamaUrl

# 5. Install Project Dependencies
Write-Host "Installing project dependencies..."
uv pip install -e .

# 6. Run Internal Setup
Write-Host "`nRunning internal setup..." -ForegroundColor Cyan
uv run manager.py setup

# 7. Register
Write-Host "`nRegistering device..." -ForegroundColor Cyan
$ArgsList = @("register", "--device-name", $DeviceName, "--username", $Username, "--registration-key", $RegistrationKey, "--device-type", $DeviceType, "--api-url", $ApiUrl)
uv run manager.py $ArgsList

if ($LASTEXITCODE -eq 0) {
    Write-Host "✔ Device Registered Successfully!" -ForegroundColor Green
    
    $ShouldStart = $StartRunning.IsPresent
    if (-not $ShouldStart) {
        $Confirm = Read-Host "`nDo you want to start the agent now? [Y/n]"
        if ($Confirm -match "^[Yy]*$") { $ShouldStart = $true }
    }

    if ($ShouldStart) {
        Write-Host "Starting Agent..." -ForegroundColor Cyan
        uv run manager.py run
    } else {
        Write-Host "`nSetup complete. To run the agent later:"
        Write-Host "cd $InstallDir"
        Write-Host "uv run manager.py run"
    }
} else {
    Write-Error "Registration failed."
}