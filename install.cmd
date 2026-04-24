@echo off
:: SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
:: SPDX-License-Identifier: BUSL-1.1
setlocal EnableExtensions EnableDelayedExpansion

echo === Loc.ai Agent Installer ===

if not defined LOCAI_REPO_URL set "LOCAI_REPO_URL=https://github.com/locai-co-uk/locai-link.git"
if not defined LOCAI_BRANCH   set "LOCAI_BRANCH=main"

:: 1. uv
where uv >nul 2>nul
if errorlevel 1 (
    echo uv not found. Installing...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%USERPROFILE%\.cargo\bin;%PATH%"
)

:: 2. git is required — main.py is a thin shim that imports `link.*` from .\src,
::    so the full repo must be on disk before we can run it.
where git >nul 2>nul
if errorlevel 1 (
    echo Error: git is required for installation but was not found.
    exit /b 1
)

:: 3. Locate or clone the repo
set "INSTALL_DIR=%CD%"
set "IS_LOCAL=0"
if exist "%CD%\main.py" if exist "%CD%\src\link" set "IS_LOCAL=1"

if "!IS_LOCAL!"=="1" (
    echo Found local repository
) else (
    set "BASE=%CD%"
    echo !BASE! | findstr /I /B "%SystemRoot% %ProgramFiles% %ProgramFiles(x86)%" >nul
    if not errorlevel 1 (
        echo Current directory ^(!BASE!^) is a system path - installing to %USERPROFILE% instead.
        set "BASE=%USERPROFILE%"
    )
    set "INSTALL_DIR=!BASE!\locai-link"
    if exist "!BASE!\locai-link\.git" (
        echo Updating existing clone at !INSTALL_DIR!...
        git -C "!BASE!\locai-link" pull --ff-only
    ) else (
        echo Cloning !LOCAI_REPO_URL! ^(!LOCAI_BRANCH!^) into !INSTALL_DIR!...
        git clone --depth 1 -b !LOCAI_BRANCH! !LOCAI_REPO_URL! "!BASE!\locai-link"
    )
)

:: 4. Launch Installer from inside the repo
echo Launching Installer...
cd /d "!INSTALL_DIR!"
uv run main.py install %*
