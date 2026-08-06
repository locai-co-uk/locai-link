# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Headless Locai Link installer for Windows, single line:
#
#     irm https://raw.githubusercontent.com/locai-co-uk/locai-link/main/scripts/install.ps1 | iex
#
# The script (this file) is served from the repo via raw.githubusercontent; the
# builds + checksums.txt it pulls live on GitHub Releases.
#
# Installs the stripped headless build (supervisor only, no tray/setup, NO engines
# bundled) per-user, registers it to run in the background, and leaves engines to
# be pulled on demand from the artifact store at first use. Mirrors
# install.sh (Linux/macOS). Uses a per-user Scheduled Task so no admin is
# needed; a real SCM service (`sc.exe create`, admin) is the alternative for a
# machine that must serve before any user logs in.
#
# Config (env overrides, all optional): LOCAI_HEADLESS_URL, LOCAI_BINARY_BASE,
# LOCAI_ARTIFACT_BASE, LOCAI_INSTALL_ROOT.

$ErrorActionPreference = "Stop"

$BinaryBase  = if ($env:LOCAI_BINARY_BASE)  { $env:LOCAI_BINARY_BASE }  else { "https://github.com/locai-co-uk/locai-link/releases/latest/download" }
$InstallRoot = if ($env:LOCAI_INSTALL_ROOT) { $env:LOCAI_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "Locai" }
$Label       = "uk.co.locai.link.headless"

function Log($m) { Write-Host "[locai-headless] $m" }
function Die($m) { Write-Error "[locai-headless] ERROR: $m"; exit 1 }

# Detect platform-arch (only Windows x64/arm64 published today).
$machine = $env:PROCESSOR_ARCHITECTURE
switch ($machine) {
    "AMD64" { $arch = "x64" }
    "ARM64" { $arch = "arm64" }
    default { Die "unsupported architecture: $machine" }
}
$platform = "windows-$arch"
$asset = "locai-link-headless-$platform.tar.gz"
$tarballUrl = if ($env:LOCAI_HEADLESS_URL) { $env:LOCAI_HEADLESS_URL } else { "$BinaryBase/$asset" }
$checksumsUrl = if ($env:LOCAI_CHECKSUMS_URL) { $env:LOCAI_CHECKSUMS_URL } else { "$BinaryBase/checksums.txt" }
Log "platform: $platform"
Log "install root: $InstallRoot"

# Fetch + checksum-verify (bootstrap trust): the tarball is what everything else
# trusts, so verify its sha256 against the release-wide checksums.txt first.
$tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([System.Guid]::NewGuid()))
try {
    $tarball = Join-Path $tmp "headless.tar.gz"
    Log "downloading $tarballUrl"
    Invoke-WebRequest -Uri $tarballUrl -OutFile $tarball
    $sums = Join-Path $tmp "checksums.txt"
    try { Invoke-WebRequest -Uri $checksumsUrl -OutFile $sums }
    catch { Die "no checksums.txt at $checksumsUrl - refusing to install unverified" }
    $line = Select-String -Path $sums -Pattern ([regex]::Escape($asset)) | Select-Object -First 1
    if (-not $line) { Die "no checksum for $asset in checksums.txt" }
    $want = ($line.Line -split '\s+')[0].Trim().ToLower()
    $got  = (Get-FileHash -Algorithm SHA256 $tarball).Hash.ToLower()
    if ($want -ne $got) { Die "checksum mismatch for $asset (want $want, got $got)" }
    Log "checksum verified against checksums.txt"

    New-Item -ItemType Directory -Force -Path $InstallRoot, (Join-Path $InstallRoot "logs"), (Join-Path $InstallRoot "engines") | Out-Null
    # tar ships with Windows 10+; extract the payload (binary + no-engine runtime).
    tar -xzf $tarball -C $InstallRoot
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

$bin = Join-Path $InstallRoot "locai-link.exe"
if (-not (Test-Path $bin)) { Die "headless binary not found at $bin after extract" }

# Put `locai` on PATH: a shim that calls the binary, plus the install dir on the
# user PATH so `locai ...` works from a new shell.
$shim = Join-Path $InstallRoot "locai.cmd"
Set-Content -Path $shim -Value '@"%~dp0locai-link.exe" %*' -Encoding ASCII
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$InstallRoot*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$InstallRoot", "User")
    Log "added $InstallRoot to your PATH (open a new terminal to pick up 'locai')"
}
Log "CLI: locai -> $bin"

# Bake the engine store base into the task so on-demand fetches resolve.
if ($env:LOCAI_ARTIFACT_BASE) {
    [Environment]::SetEnvironmentVariable("LOCAI_ARTIFACT_BASE", $env:LOCAI_ARTIFACT_BASE, "User")
}

# Register a per-user Scheduled Task: run at logon, restart on failure, no admin.
# Keyless (`run`): the supervisor idles until a session exists; registration is
# out-of-band via `locai register`, so no key is written into the task.
$action  = New-ScheduledTaskAction -Execute $bin -Argument "run" -WorkingDirectory $InstallRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $Label -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $Label

# Unattended installs can supply a key via env -> one-shot register now (the idle
# service picks up the session). Otherwise print the register command.
if ($env:LOCAI_REGISTRATION_KEY) {
    Log "registering this device with your key..."
    & $bin register --registration-key $env:LOCAI_REGISTRATION_KEY
} elseif ($env:LOCAI_FLEET_KEY) {
    Log "enrolling this device with your fleet key..."
    & $bin register --fleet-key $env:LOCAI_FLEET_KEY
} else {
    Log "installed. Register this device with a key from Control:"
    Log "  locai register --registration-key <KEY>     # single device"
    Log "  locai register --fleet-key <KEY|file:PATH>  # fleet enrollment"
    Log "then confirm:  locai status"
}
