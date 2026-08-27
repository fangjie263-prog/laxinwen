@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"

echo ========================================
echo Laxinwen - Notion Sync
echo ========================================
echo.
echo Project root:
echo %PROJECT_ROOT%
echo.
echo Python:
echo %PYTHON%
echo.

if not exist "%PYTHON%" (
    echo [INFO] Laxinwen virtual environment not found. Trying uv sync ...
    where uv >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] uv was not found on PATH.
        echo Please install uv or run "uv sync" from the project directory.
        pause
        exit /b 1
    )

    pushd "%PROJECT_ROOT%"
    call uv sync
    set "SYNC_EXIT=%ERRORLEVEL%"
    popd
    if not "%SYNC_EXIT%"=="0" (
        echo [ERROR] uv sync failed.
        pause
        exit /b 1
    )
)

if not exist "%PYTHON%" (
    echo [ERROR] Laxinwen virtual environment was not created:
    echo %PYTHON%
    echo Please run "uv sync" from the project directory and try again.
    pause
    exit /b 1
)

echo Uploading pending artifacts to Notion ...
set "ATTEMPT=1"

:retry_sync
echo [INFO] Notion sync attempt %ATTEMPT% of 3 ...
pushd "%PROJECT_ROOT%"
"%PYTHON%" -m news notion-sync
set "SYNC_EXIT=%ERRORLEVEL%"
popd

if "%SYNC_EXIT%"=="0" (
    if "%ATTEMPT%"=="1" echo [SUCCESS] Notion sync completed.
    if "%ATTEMPT%"=="2" echo [SUCCESS] Notion sync completed on retry 2.
    if "%ATTEMPT%"=="3" echo [SUCCESS] Notion sync completed on retry 3.
    pause
    exit /b 0
)

if "%ATTEMPT%"=="1" (
    echo [RETRY] Waiting 10 seconds before retry 2...
    timeout /t 10 /nobreak >nul
    set "ATTEMPT=2"
    goto retry_sync
)

if "%ATTEMPT%"=="2" (
    echo [RETRY] Waiting 30 seconds before retry 3...
    timeout /t 30 /nobreak >nul
    set "ATTEMPT=3"
    goto retry_sync
)

echo [ERROR] Notion sync failed after 3 attempts.
pause
exit /b %SYNC_EXIT%
