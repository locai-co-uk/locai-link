# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Headless Locai Link uninstaller for Windows. Reverses scripts/install.ps1:
# unregisters the scheduled task, removes the install root, drops the PATH entry
# and the LOCAI_ARTIFACT_BASE user variable.
#
#     irm https://raw.githubusercontent.com/locai-co-uk/locai-link/main/scripts/uninstall.ps1 | iex
#
# The device stays registered in Control after uninstall; remove its row in Control
# to fully deregister.

$ErrorActionPreference = "SilentlyContinue"
$InstallRoot = if ($env:LOCAI_INSTALL_ROOT) { $env:LOCAI_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "Locai" }
$Label = "uk.co.locai.link.headless"

function Log($m) { Write-Host "[locai-uninstall] $m" }

# Guard: only remove a real headless install (has the binary).
if (-not (Test-Path (Join-Path $InstallRoot "locai-link.exe"))) {
    Write-Error "[locai-uninstall] refusing to remove '$InstallRoot': not a Locai headless install (no locai-link.exe)"
    exit 1
}

# 1. Stop + unregister the scheduled task.
Stop-ScheduledTask -TaskName $Label -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $Label -Confirm:$false -ErrorAction SilentlyContinue
Get-Process locai-link -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$InstallRoot*" } | Stop-Process -Force

# 2. Drop the install dir from the user PATH.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -like "*$InstallRoot*") {
    $cleaned = ($userPath -split ';' | Where-Object { $_ -and $_ -ne $InstallRoot }) -join ';'
    [Environment]::SetEnvironmentVariable("Path", $cleaned, "User")
    Log "removed $InstallRoot from PATH"
}

# 3. Drop the engine-store env var we may have set.
[Environment]::SetEnvironmentVariable("LOCAI_ARTIFACT_BASE", $null, "User")

# 4. Remove the install root (binary, runtime, engines, logs, state, session).
Remove-Item -Recurse -Force $InstallRoot -ErrorAction SilentlyContinue

Log "Locai Link (headless) removed. The device may still appear in Control; remove its row there to deregister."
