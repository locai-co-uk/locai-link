# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Headless Locai Link installer for Windows, single line:
#
#     irm https://get.locai.co.uk/install.ps1 | iex
#
# The script (this file) is served from get.locai.co.uk; the builds +
# checksums.txt it pulls live on GitHub Releases.
#
# Installs the stripped headless build (supervisor only, no tray/setup, NO engines
# bundled) per-user, registers it to run in the background, and leaves engines to
# be pulled on demand from the artifact store at first use. Mirrors
# install.sh (Linux/macOS). Uses a per-user Scheduled Task so no admin is
# needed; a real SCM service (`sc.exe create`, admin) is the alternative for a
# machine that must serve before any user logs in.
#
# Config (env overrides, all optional): LOCAI_HEADLESS_URL, LOCAI_BINARY_BASE,
# LOCAI_CHECKSUMS_URL, LOCAI_ARTIFACT_BASE, LOCAI_INSTALL_ROOT, LOCAI_FORCE=1
# (reinstall in place over an existing install), LOCAI_REGISTRATION_KEY /
# LOCAI_FLEET_KEY (unattended registration; read from the environment, never
# passed on a command line).
#
# Re-run behaviour mirrors install.sh: a re-run on an installed device does NOT
# clobber; it reports status or re-surfaces the register steps. LOCAI_FORCE=1
# reinstalls in place (the session is preserved).

$ErrorActionPreference = "Stop"

$BinaryBase  = if ($env:LOCAI_BINARY_BASE)  { $env:LOCAI_BINARY_BASE }  else { "https://github.com/locai-co-uk/locai-link/releases/latest/download" }
$InstallRoot = if ($env:LOCAI_INSTALL_ROOT) { $env:LOCAI_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "Locai" }
$Label       = "uk.co.locai.link.headless"

function Log($m) { Write-Host $m }
function Die($m) { Write-Error $m; exit 1 }

$bin   = Join-Path $InstallRoot "locai-link.exe"
$Force = ($env:LOCAI_FORCE -eq "1")

function Test-LocaiSession { [bool](Test-Path (Join-Path $InstallRoot "configs\session_*.json")) }

# One-shot out-of-band registration for unattended installs. The key reaches
# `register` via the inherited environment, never argv (command lines are
# readable by other local processes).
function Invoke-LocaiRegistration {
    if ($env:LOCAI_REGISTRATION_KEY -or $env:LOCAI_FLEET_KEY) {
        & $bin register
        if ($LASTEXITCODE -ne 0) { Die "registration failed; re-run: locai register (with your key in the env)" }
        Log "This device is now connected."
    } else {
        Log ""
        Log "This device isn't connected yet. Register it with a key from Control:"
        Log "  locai register --registration-key <KEY>     # single device"
        Log "  locai register --fleet-key <KEY|file:PATH>  # fleet enrollment"
    }
}

function Show-LocaiFooter {
    Log ""
    Log "Check it with 'locai status', or 'locai --help' for all commands."
    Log "Service logs: $InstallRoot\logs\service.log"
    Log "To uninstall: locai uninstall"
}

# Register + start the per-user Scheduled Task (idempotent): run at logon,
# restart on failure, no admin. The logon trigger and principal are scoped to
# the installing user - an any-user trigger needs elevation, which this
# per-user installer must never require. Keyless (`run`): the supervisor idles
# until a session exists; registration is out-of-band via `locai register`.
function Install-LocaiTask {
    $me = "$env:USERDOMAIN\$env:USERNAME"
    # The headless exe keeps a console subsystem (CLI output), so an interactive
    # task would pop a console window at every logon. A hidden PowerShell parent
    # suppresses the window while keeping the exe inside the task's process
    # tree, so stop/restart (schtasks /End) still terminate it.
    # wscript is a GUI-subsystem host, so no console is ever allocated - this
    # holds even where Windows Terminal is the default host and ignores
    # -WindowStyle Hidden. The cmd layer appends all output to the service log
    # (journald/launchd cover this elsewhere), and both layers wait, so the
    # task's process tree still owns the exe and stop/restart terminate it.
    $log = Join-Path $InstallRoot "logs\service.log"
    $vbs = Join-Path $InstallRoot "run-hidden.vbs"
    $vbsContent = @"
' Launches the headless service with a hidden console (written by install.ps1).
Q = Chr(34)
bin = "$bin"
logf = "$log"
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /s /c " & Q & Q & bin & Q & " run >> " & Q & logf & Q & " 2>&1" & Q, 0, True
"@
    Set-Content -Path $vbs -Value $vbsContent -Encoding ASCII
    $action    = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$vbs`"" -WorkingDirectory $InstallRoot
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $me
    $settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive
    # -Force replaces only the definition; a running instance (old action)
    # survives it. Stop it so the new action takes over immediately.
    Stop-ScheduledTask -TaskName $Label -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Label -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $Label -ErrorAction SilentlyContinue
}

# Re-run guard (parity with install.sh): don't clobber an existing install,
# but ensure the task exists and is up (self-heals a failed registration).
if ((Test-Path $bin) -and -not $Force) {
    Log "Locai Link is already installed at $InstallRoot."
    Install-LocaiTask
    if (Test-LocaiSession) {
        Log "Already registered (set LOCAI_FORCE=1 to reinstall in place)."
    } else {
        Invoke-LocaiRegistration
    }
    Show-LocaiFooter
    return
}

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
    # Exact filename match on field 2 (sha256sum format, with or without the
    # binary-mode `*` prefix); a substring match could pick another asset's hash.
    $want = $null
    foreach ($line in Get-Content $sums) {
        $fields = $line.Trim() -split '\s+'
        if ($fields.Count -ge 2 -and ($fields[1] -eq $asset -or $fields[1] -eq "*$asset")) {
            $want = $fields[0].ToLower()
            break
        }
    }
    if (-not $want) { Die "no checksum for $asset in checksums.txt" }
    $got  = (Get-FileHash -Algorithm SHA256 $tarball).Hash.ToLower()
    if ($want -ne $got) { Die "checksum mismatch for $asset (want $want, got $got)" }
    Log "checksum verified"

    New-Item -ItemType Directory -Force -Path $InstallRoot, (Join-Path $InstallRoot "logs"), (Join-Path $InstallRoot "engines") | Out-Null
    # A running instance locks locai-link.exe; stop the task first so a --force
    # reinstall can overwrite it (no-op on a fresh install). Stop-ScheduledTask
    # is asynchronous, so poll until the task has actually stopped.
    Stop-ScheduledTask -TaskName $Label -ErrorAction SilentlyContinue
    $task = $null
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $task = Get-ScheduledTask -TaskName $Label -ErrorAction SilentlyContinue
        if (-not $task -or $task.State -notin @("Running", "Queued")) { break }
        Start-Sleep -Milliseconds 250
    }
    if ($task -and $task.State -in @("Running", "Queued")) {
        Die "scheduled task $Label did not stop; aborting before file replacement"
    }
    # Stopping the task only terminates the scheduler-launched launcher; the
    # service exe can survive it and would hold the file lock. Path-scoped so
    # other checkouts' binaries are untouched.
    Get-Process -Name locai-link, locai-link-runtime -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$InstallRoot*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    # tar ships with Windows 10+. --strip-components=1 drops the <name>/ wrapper so
    # locai-link.exe + versions/ + boot.json land at the install-root top (matches install.sh).
    tar -xzf $tarball -C $InstallRoot --strip-components=1
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

if (-not (Test-Path $bin)) { Die "headless binary not found at $bin after extract" }

# Put `locai` on PATH: a shim that calls the binary, plus the install dir on the
# user PATH so `locai ...` works from a new shell.
$shim = Join-Path $InstallRoot "locai.cmd"
Set-Content -Path $shim -Value '@"%~dp0locai-link.exe" %*' -Encoding ASCII
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$InstallRoot*") {
    # An unset user Path must not become ";C:\..." - an empty PATH entry
    # resolves to the current directory.
    $newPath = if ([string]::IsNullOrEmpty($userPath)) { $InstallRoot } else { "$userPath;$InstallRoot" }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    # Also update this session so `locai` works immediately when the script
    # runs in-process (irm | iex). A -File child can't reach its parent shell,
    # hence the new-terminal note.
    if ($env:Path -notlike "*$InstallRoot*") { $env:Path = "$env:Path;$InstallRoot" }
    Log "added $InstallRoot to your PATH (open a new terminal to pick up 'locai')"
}
Log "CLI: locai -> $bin"

# Bake the engine store base into the task so on-demand fetches resolve.
if ($env:LOCAI_ARTIFACT_BASE) {
    [Environment]::SetEnvironmentVariable("LOCAI_ARTIFACT_BASE", $env:LOCAI_ARTIFACT_BASE, "User")
}

Install-LocaiTask

Log ""
Log "Locai Link is installed and the service is running."

# Unattended installs can supply a key via env -> one-shot register now (the idle
# service picks up the session). Otherwise show how to register.
if (Test-LocaiSession) {
    Log "Existing registration preserved."
} else {
    Invoke-LocaiRegistration
}

Show-LocaiFooter
