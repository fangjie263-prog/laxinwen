@echo off
setlocal
cd /d "%~dp0..\.."
echo Deleting Laxinwen-Notion-Sync ...
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found.
    exit /b 1
)
".venv\Scripts\python.exe" -m news scheduler delete notion-sync
if errorlevel 1 (
    echo [ERROR] Could not delete the Windows task.
    pause
    exit /b 1
)
echo Task deleted.
endlocal
