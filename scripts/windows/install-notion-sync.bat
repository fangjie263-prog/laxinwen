@echo off
setlocal
cd /d "%~dp0..\.."

echo Installing or updating Laxinwen-Notion-Sync ...
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found. Run uv sync --extra dev first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m news scheduler install notion-sync --project-root "%CD%"
if errorlevel 1 (
    echo [ERROR] Could not install the Windows task.
    pause
    exit /b 1
)
echo Task installed: Laxinwen-Notion-Sync
endlocal
