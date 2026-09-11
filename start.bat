@echo off
chcp 65001 >nul
title Dotori Launcher
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b
)

python -c "import requests" >nul 2>nul
if %errorlevel% equ 0 goto menu

echo [INFO] Installing required Python package: requests
python -m pip install --quiet requests
python -c "import requests" >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install "requests". Run "pip install requests" manually and retry.
    pause
    exit /b
)

:menu
cls
echo ============================================================
echo   Dotori Launcher
echo ============================================================
echo   [1] Install / Setup Wizard   (first run or reconfigure)
echo   [2] Start / Resume Dotori     (no rebuild)
echo   [3] Stop / Pause Dotori       (preserve containers and cache)
echo   [4] Change LLM Model
echo   [5] Change Embedding Model
echo   [6] Show Server Status
echo   [7] Maintenance
echo   [8] Advanced Network Settings
echo   [9] Exit
echo ============================================================
set "choice="
set /p choice="Select an option (1-9): "

if "%choice%"=="1" goto install
if "%choice%"=="2" goto run
if "%choice%"=="3" goto stop
if "%choice%"=="4" goto change_llm
if "%choice%"=="5" goto change_embedding
if "%choice%"=="6" goto status
if "%choice%"=="7" goto maintenance_menu
if "%choice%"=="8" goto network_menu
if "%choice%"=="9" exit /b
echo.
echo [ERROR] Invalid option: %choice%
pause
goto menu

:install
python install.py
if %errorlevel% neq 0 (
    echo [ERROR] Installation failed.
)
pause
goto menu

:run
echo Starting Dotori from existing images and resuming the configured LLM runtime...
python install.py --run
if %errorlevel% neq 0 (
    echo [ERROR] Failed to start Dotori.
)
pause
goto menu

:change_llm
python install.py --change-llm
if %errorlevel% neq 0 (
    echo [ERROR] Failed to change the LLM model.
)
pause
goto menu

:change_embedding
python install.py --change-embedding
if %errorlevel% neq 0 (
    echo [ERROR] Failed to change the embedding model.
)
pause
goto menu

:stop
python install.py --stop
if %errorlevel% neq 0 (
    echo [ERROR] Failed to pause Dotori services. Is Docker Desktop running?
)
pause
goto menu

:status
python install.py --status
pause
goto menu

:maintenance_menu
cls
echo ============================================================
echo   Maintenance
echo ============================================================
echo   [1] Restart Services            (no rebuild)
echo   [2] Rebuild and Restart         (application + LLM runtime)
echo   [3] Full Shutdown               (remove containers, keep data/cache)
echo   [4] Remove LLM Runtime and Model Cache
echo   [5] Toggle Login Requirement
echo   [6] Change Embedding Model
echo   [7] Back
echo ============================================================
set "maintenance_choice="
set /p maintenance_choice="Select an option (1-7): "

if "%maintenance_choice%"=="1" goto restart
if "%maintenance_choice%"=="2" goto rebuild
if "%maintenance_choice%"=="3" goto shutdown
if "%maintenance_choice%"=="4" goto remove_llm
if "%maintenance_choice%"=="5" goto toggle_login
if "%maintenance_choice%"=="6" goto change_embedding
if "%maintenance_choice%"=="7" goto menu
echo.
echo [ERROR] Invalid option: %maintenance_choice%
pause
goto maintenance_menu

:restart
python install.py --restart
if %errorlevel% neq 0 (
    echo [ERROR] Failed to restart Dotori without rebuilding.
)
pause
goto maintenance_menu

:rebuild
python install.py --rebuild
if %errorlevel% neq 0 (
    echo [ERROR] Failed to rebuild and restart Dotori.
)
pause
goto maintenance_menu

:shutdown
python install.py --shutdown
if %errorlevel% neq 0 (
    echo [ERROR] Failed to shut down Dotori completely.
)
pause
goto maintenance_menu

:remove_llm
python install.py --remove-llm
if %errorlevel% neq 0 (
    echo [ERROR] Failed to remove the LLM runtime and model cache.
)
pause
goto maintenance_menu

:toggle_login
echo   [1] Require real sign-in (external/multi-user access)
echo   [2] No login, auto sign-in as local admin (personal/local use)
set "login_choice="
set /p login_choice="Select an option (1-2): "
if "%login_choice%"=="1" (
    python install.py --login enable
) else if "%login_choice%"=="2" (
    python install.py --login disable
) else (
    echo [ERROR] Invalid option: %login_choice%
)
pause
goto maintenance_menu

:network_menu
cls
echo ============================================================
echo   Advanced Network Settings
echo ============================================================
echo   [1] Create external access configuration files
echo   [2] Open external access configuration folder
echo   [3] Connect external access module
echo   [4] Disconnect external access module
echo   [5] Show external access status
echo   [6] Back
echo ============================================================
set "network_choice="
set /p network_choice="Select an option (1-6): "

if "%network_choice%"=="1" goto network_create
if "%network_choice%"=="2" goto network_open
if "%network_choice%"=="3" goto network_connect
if "%network_choice%"=="4" goto network_disconnect
if "%network_choice%"=="5" goto network_status
if "%network_choice%"=="6" goto menu
echo.
echo [ERROR] Invalid option: %network_choice%
pause
goto network_menu

:network_create
python install.py --network-access-create
pause
goto network_menu

:network_open
python install.py --network-access-open
if %errorlevel% neq 0 (
    echo [ERROR] Failed to open the configuration folder.
    pause
)
goto network_menu

:network_connect
python install.py --network-access-connect
if %errorlevel% neq 0 (
    echo [ERROR] External access was not connected. Review the configuration files.
)
pause
goto network_menu

:network_disconnect
python install.py --network-access-disconnect
if %errorlevel% neq 0 (
    echo [ERROR] Failed to disconnect external access.
)
pause
goto network_menu

:network_status
python install.py --network-access-status
pause
goto network_menu
