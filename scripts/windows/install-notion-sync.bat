@echo off
setlocal
cd /d "%~dp0..\.."

echo Installing or updating Laxinwen-Notion-Sync ...
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found. Run uv sync --extra dev first.
    pause
    exit /b 1
)

schtasks /Create /TN "Laxinwen-Notion-Sync" /SC HOURLY /MO 1 /F ^
  /TR "\"%CD%\.venv\Scripts\python.exe\" -m news notion-sync"
if errorlevel 1 (
    echo [ERROR] Could not install the Windows task.
    pause
    exit /b 1
)
echo Task installed: Laxinwen-Notion-Sync
endlocal
