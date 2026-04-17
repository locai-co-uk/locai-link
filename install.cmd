@echo off
:: SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
:: SPDX-License-Identifier: BUSL-1.1

echo === Loc.ai Agent Installer ===

:: 1. Check if uv is installed
where uv >nul 2>nul
if errorlevel 1 (
    echo uv not found. Installing...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

:: 2. Determine source and Launch
if exist main.py (
    echo Found local main.py
    uv run main.py install %*
) else (
    echo Downloading remote main.py...
    uv run https://raw.githubusercontent.com/locai-co-uk/locai-link/main/main.py install %*
)
