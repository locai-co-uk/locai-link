# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Headless Locai Link installer for Windows, single line:
#
#     irm https://get.locai.co.uk/headless.ps1 | iex
#
# Installs the stripped headless build (supervisor only, no tray/setup, NO engines
# bundled) per-user, registers it to run in the background, and leaves engines to
# be pulled on demand from the artifact store at first use. Mirrors
# install-headless.sh (Linux/macOS). Uses a per-user Scheduled Task so no admin is
# needed; a real SCM service (`sc.exe create`, admin) is the alternative for a
# machine that must serve before any user logs in.
#
# Config (env overrides, all optional): LOCAI_HEADLESS_URL, LOCAI_BINARY_BASE,
# LOCAI_ARTIFACT_BASE, LOCAI_INSTALL_ROOT.

$ErrorActionPreference = "Stop"

$BinaryBase  = if ($env:LOCAI_BINARY_BASE)  { $env:LOCAI_BINARY_BASE }  else { "https://get.locai.co.uk/headless" }
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
$tarballUrl = if ($env:LOCAI_HEADLESS_URL) { $env:LOCAI_HEADLESS_URL } else { "$BinaryBase/locai-link-headless-$platform.tar.gz" }
Log "platform: $platform"
Log "install root: $InstallRoot"

# Fetch + checksum-verify (bootstrap trust): the pulled binary is what everything
# else trusts, so verify its sha256 against the published .sha256 first.
$tmp = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([System.Guid]::NewGuid()))
try {
    $tarball = Join-Path $tmp "headless.tar.gz"
    Log "downloading $tarballUrl"
    Invoke-WebRequest -Uri $tarballUrl -OutFile $tarball
    try { Invoke-WebRequest -Uri "$tarballUrl.sha256" -OutFile "$tarball.sha256" }
    catch { Die "no .sha256 for $tarballUrl - refusing to install an unverified binary" }
    $want = (Get-Content "$tarball.sha256").Split(" ")[0].Trim()
    $got  = (Get-FileHash -Algorithm SHA256 $tarball).Hash.ToLower()
    if ($want -ne $got) { Die "checksum mismatch (want $want, got $got)" }
    Log "checksum verified"

    New-Item -ItemType Directory -Force -Path $InstallRoot, (Join-Path $InstallRoot "logs"), (Join-Path $InstallRoot "engines") | Out-Null
    # tar ships with Windows 10+; extract the payload (binary + no-engine runtime).
    tar -xzf $tarball -C $InstallRoot
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

$bin = Join-Path $InstallRoot "locai-link.exe"
if (-not (Test-Path $bin)) { Die "headless binary not found at $bin after extract" }

# Bake the engine store base into the task so on-demand fetches resolve.
if ($env:LOCAI_ARTIFACT_BASE) {
    [Environment]::SetEnvironmentVariable("LOCAI_ARTIFACT_BASE", $env:LOCAI_ARTIFACT_BASE, "User")
}

# Register a per-user Scheduled Task: run at logon, restart on failure, no admin.
$action  = New-ScheduledTaskAction -Execute $bin -Argument "run --headless" -WorkingDirectory $InstallRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $Label -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $Label

Log "installed. Onboard this device by following the login prompt in the log:"
Log "  Get-Content -Wait (Join-Path '$InstallRoot' 'logs\*.log')"
