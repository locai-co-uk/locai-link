@echo off
:: SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
:: SPDX-License-Identifier: BUSL-1.1

echo === LocAI Edge Agent Installer ===

:: 1. Check if uv is installed
where uv >nul 2>nul
if errorlevel 1 (
    echo uv not found. Installing...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

:: 2. Determine source and Launch
if exist manager.py (
    echo ✔ Found local manager.py
    uv run manager.py install %*
) else (
    echo ⬇ Downloading remote manager.py...
    uv run https://raw.githubusercontent.com/locai-co-uk/locai-link/main/manager.py install %*
)