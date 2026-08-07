# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1
#
# Windows counterpart of bundling/linux/pack.sh for the HEADLESS shape: assemble
# the flat install-root tarball that scripts/install.ps1 extracts. Shape + asset
# name are read from dist/locai-link/current/manifest.json (written by build.py);
# this then builds the matching --no-default-features locai-link.exe, so the
# feature can't diverge from the bundle's shape.
#
# Output layout (inside the tarball, under a <name>/ wrapper install.ps1 strips):
#     locai-link-headless-windows-<x64|arm64>-<version>/
#     ├── locai-link.exe
#     ├── boot.json
#     └── versions/  current|CURRENT  (the runtime bundle)
#
# Prereq (repo root):  uv run python bundling/build.py --shape headless --plugins <set>
#
#     pwsh bundling/windows/pack.ps1            # -> dist/...-DEV.tar.gz
#     pwsh bundling/windows/pack.ps1 -Release   # drop the -DEV suffix
#     pwsh bundling/windows/pack.ps1 -Dev       # bake dev Control + dev artifact store

[CmdletBinding()]
param(
    [switch]$Release,
    [switch]$Dev,
    [string]$Output
)
$ErrorActionPreference = "Stop"
# A release-named archive must never carry dev endpoints.
if ($Release -and $Dev) { throw "-Release and -Dev cannot be combined." }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

$BundleDir = Join-Path $RepoRoot "dist\locai-link"
$BootJson  = Join-Path $RepoRoot "bundling\boot.json"

# `current` is a symlink where supported; Windows without symlink rights gets
# build.py's CURRENT text-pointer file instead. Accept both, like the launcher.
$Manifest = Join-Path $BundleDir "current\manifest.json"
if (-not (Test-Path $Manifest)) {
    $pointer = Join-Path $BundleDir "CURRENT"
    if (Test-Path $pointer) {
        $ver = (Get-Content $pointer -Raw).Trim()
        $Manifest = Join-Path $BundleDir "versions\$ver\manifest.json"
    }
}
if (-not (Test-Path $Manifest)) { throw "manifest.json not found under $BundleDir (current\ or CURRENT pointer) - run ``uv run python bundling/build.py --shape headless --plugins ...`` first." }
if (-not (Test-Path $BootJson))  { throw "boot.json not at $BootJson." }

# --- Derive asset name + shape from manifest (single source of truth) ---
$m = Get-Content $Manifest -Raw | ConvertFrom-Json
$assetStem = $m.asset_name
$version   = "v$($m.version)"
$shape     = if ($m.shape) { $m.shape } else { "desktop" }
if (-not $assetStem -or -not $m.version) { throw "manifest.json missing asset_name/version" }
if ($shape -ne "headless") { throw "windows pack.ps1 handles the headless shape only (got '$shape'); desktop Windows is the tauri bundle." }

# --- Build the matching Rust binary (headless = no tray/setup) ---
if ($Dev) {
    $env:LOCAI_CONTROL_URL     = "https://dev.control.locai.co.uk"
    $env:LOCAI_CONTROL_API_URL = "https://dev.api.locai.co.uk/api/v1"
    $env:LOCAI_ARTIFACT_BASE   = "https://storage.googleapis.com/locai-platform-artifacts-dev"
    Write-Host "[pack] DEV build - Control=$env:LOCAI_CONTROL_URL, artifacts=$env:LOCAI_ARTIFACT_BASE"
} else {
    # The bake reads these at compile time: drop inherited overrides so a
    # prod pack can't silently pick up endpoints from the calling shell.
    Remove-Item Env:LOCAI_CONTROL_URL, Env:LOCAI_CONTROL_API_URL, Env:LOCAI_ARTIFACT_BASE -ErrorAction SilentlyContinue
    Write-Host "[pack] PROD build (pass -Dev to bake the dev endpoints)"
}
# Clean, build, and copy must agree on one target dir: pin it so an inherited
# CARGO_TARGET_DIR can't leave a stale binary at the copy path.
$env:CARGO_TARGET_DIR = Join-Path $RepoRoot "crates\target"
Write-Host "[pack] building locai-link.exe (headless)..."
Push-Location (Join-Path $RepoRoot "crates")
try {
    # Always clean the crate: the endpoint bake is compile-time (option_env) and
    # cargo won't recompile on env-only changes, so a cached binary would keep
    # the previous pack's endpoints.
    cargo clean -p locai-link
    if ($LASTEXITCODE -ne 0) { throw "cargo clean failed" }
    cargo build -p locai-link --no-default-features --release
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }
} finally { Pop-Location }
$bin = Join-Path $RepoRoot "crates\target\release\locai-link.exe"
if (-not (Test-Path $bin)) { throw "locai-link.exe not at $bin after build" }

switch ($env:PROCESSOR_ARCHITECTURE) {
    "AMD64" { $arch = "x64" }
    "ARM64" { $arch = "arm64" }
    default { throw "unsupported architecture: $env:PROCESSOR_ARCHITECTURE" }
}
$name = "$assetStem-windows-$arch-$version"
if (-not $Release) { $name = "$name-DEV" }
if (-not $Output) { $Output = Join-Path $RepoRoot "dist\$name.tar.gz" }
Write-Host "[pack] asset name:  $name"
Write-Host "[pack] output:      $Output"

# --- Stage the flat install root, then tar (same <name>/ wrapper as Linux) ---
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("locai-pack-" + [System.Guid]::NewGuid())
$root  = Join-Path $stage $name
New-Item -ItemType Directory -Force -Path $root | Out-Null
try {
    Copy-Item -Recurse -Force (Join-Path $BundleDir "*") $root
    Copy-Item -Force $bin (Join-Path $root "locai-link.exe")
    python (Join-Path $RepoRoot "bundling\gen_boot_json.py") --manifest $Manifest --template $BootJson --output (Join-Path $root "boot.json")
    if ($LASTEXITCODE -ne 0) { throw "gen_boot_json.py failed" }

    # A bare filename (-Output foo.tar.gz) has no parent to create.
    $outDir = Split-Path -Parent $Output
    if ($outDir) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
    tar -czf $Output -C $stage $name
    if ($LASTEXITCODE -ne 0) { throw "tar failed" }
} finally {
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
Write-Host "[pack] wrote $Output"
