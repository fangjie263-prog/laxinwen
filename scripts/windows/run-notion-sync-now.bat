@echo off
setlocal
cd /d "%~dp0..\.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found. Run uv sync --extra dev first.
    pause
    exit /b 1
)

echo Running Notion sync now ...
".venv\Scripts\python.exe" -m news scheduler run notion-sync
if errorlevel 1 (
    echo [ERROR] Notion sync failed. Check NOTION_TOKEN and NOTION_ROOT_PAGE_ID.
    pause
    exit /b 1
)
echo Notion sync completed.
endlocal
