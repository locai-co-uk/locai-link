@echo off
setlocal EnableDelayedExpansion

:: SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
:: SPDX-License-Identifier: BUSL-1.1

echo === LocAI Edge Agent Installer ===

:: --- Configuration ---
set "REPO_URL=https://github.com/locai-co-uk/locai-link.git"
set "BRANCH=main"
set "PYTHON_VERSION=3.11.8"

:: --- Defaults ---
set "DEVICE_TYPE=edge_device"
set "START_RUNNING=false"
set "SKIP_GIT=false"

:: --- Check if already inside the repository ---
if exist "pyproject.toml" (
    echo Detected running inside repository.
    echo Skipping Git clone/pull. Using current directory.
    set "INSTALL_DIR=%CD%"
    set "SKIP_GIT=true"
) else (
    set "INSTALL_DIR=%CD%\locai-link"
    set "SKIP_GIT=false"
)

:: --- Argument Parsing ---
:parse_args
if "%~1"=="" goto :check_args
if "%~1"=="--device-name" set "DEVICE_NAME=%~2" & shift & shift & goto :parse_args
if "%~1"=="--username" set "USERNAME=%~2" & shift & shift & goto :parse_args
if "%~1"=="--registration-key" set "REG_KEY=%~2" & shift & shift & goto :parse_args
if "%~1"=="--device-type" set "DEVICE_TYPE=%~2" & shift & shift & goto :parse_args
if "%~1"=="--api-url" set "API_URL=%~2" & shift & shift & goto :parse_args
if "%~1"=="--branch" set "BRANCH=%~2" & shift & shift & goto :parse_args
if "%~1"=="--start-running" set "START_RUNNING=true" & shift & goto :parse_args
shift
goto :parse_args

:check_args

:: --- Interactive Prompts ---
if "%DEVICE_NAME%"=="" set /p "DEVICE_NAME=Enter Device Name: "
if "%USERNAME%"=="" set /p "USERNAME=Enter Username: "
if "%REG_KEY%"=="" set /p "REG_KEY=Enter Registration Key: "

:: --- Prerequisites ---
echo.
echo Checking system prerequisites...

if "%SKIP_GIT%"=="false" (
    where git >nul 2>nul
    if errorlevel 1 (
        echo Error: git is not installed. Please install Git for Windows.
        exit /b 1
    )
)

where uv >nul 2>nul
if errorlevel 1 (
    echo uv not found. Installing...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.cargo\bin;%USERPROFILE%\.local\bin;%PATH%"
) else (
    echo uv is already installed.
)

:: --- Clone/Update Repository ---
if "%SKIP_GIT%"=="false" (
    if exist "%INSTALL_DIR%" (
        echo Updating repository in %INSTALL_DIR%...
        cd /d "%INSTALL_DIR%"
        git pull origin %BRANCH%
    ) else (
        echo Cloning repository to %INSTALL_DIR%...
        git clone --depth 1 -b %BRANCH% %REPO_URL% "%INSTALL_DIR%"
        cd /d "%INSTALL_DIR%"
    )
) else (
    cd /d "%INSTALL_DIR%"
)

:: --- Load Defaults ---
if exist "defaults.env" (
    for /f "eol=# tokens=1,2 delims==" %%i in (defaults.env) do (
        set "%%i=%%j"
    )
)
if "%DEFAULT_API_URL%"=="" set "DEFAULT_API_URL=https://api.locai.co.uk/api/v1"
if "%DEV_API_URL%"=="" set "DEV_API_URL=https://dev-api.locai.co.uk/api/v1"
if "%LOCAL_API_URL%"=="" set "LOCAL_API_URL=http://localhost:8001/api/v1"

:: --- API URL Selection ---
if "%API_URL%"=="" (
    echo.
    echo Select API Environment:
    echo 1^) Production ^(%DEFAULT_API_URL%^)
    echo 2^) Dev        ^(%DEV_API_URL%^)
    echo 3^) Localhost  ^(%LOCAL_API_URL%^)
    echo 4^) Custom URL
    set /p "API_CHOICE=Choice [1]: "
    
    if "!API_CHOICE!"=="2" (
        set "API_URL=%DEV_API_URL%"
    ) else if "!API_CHOICE!"=="3" (
        set "API_URL=%LOCAL_API_URL%"
    ) else if "!API_CHOICE!"=="4" (
        set /p "API_URL=Enter Custom API URL: "
    ) else (
        set "API_URL=%DEFAULT_API_URL%"
    )
)

:: Final Check
if "%DEVICE_NAME%"=="" ( echo Error: Device Name is required. & exit /b 1 )
if "%USERNAME%"=="" ( echo Error: Username is required. & exit /b 1 )
if "%REG_KEY%"=="" ( echo Error: Registration Key is required. & exit /b 1 )

:: --- Setup Environment ---
echo.
echo Initializing Environment (Python %PYTHON_VERSION%)...

:: Ensure python version and clean venv
uv python install %PYTHON_VERSION%
if exist ".venv" rmdir /s /q ".venv"
uv venv --python %PYTHON_VERSION%

:: --- Run Manager Setup ---
:: (Manager now handles install_inference_engine for llama-cpp-python and other deps)
echo.
echo Running internal setup...
uv run manager.py setup

:: --- Register ---
echo.
echo Registering device...
uv run manager.py register --device-name "%DEVICE_NAME%" --username "%USERNAME%" --registration-key "%REG_KEY%" --device-type "%DEVICE_TYPE%" --api-url "%API_URL%"

if %errorlevel% equ 0 (
    echo.
    echo Device Registered Successfully!
    
    if "%START_RUNNING%"=="false" (
        echo.
        set /p "START_CONFIRM=Do you want to start the agent now? [Y/n] "
        if /i "!START_CONFIRM!"=="y" set "START_RUNNING=true"
        if "!START_CONFIRM!"=="" set "START_RUNNING=true"
    )

    if "!START_RUNNING!"=="true" (
        echo Starting Agent...
        uv run manager.py run
    ) else (
        echo Setup complete. To run the agent later, use:
        echo   cd "%INSTALL_DIR%"
        echo   uv run manager.py run
    )
) else (
    echo Registration failed.
    exit /b 1
)

endlocal