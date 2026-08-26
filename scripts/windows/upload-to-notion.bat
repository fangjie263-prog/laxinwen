@echo off
setlocal
cd /d "%~dp0..\.."

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] uv was not found on PATH.
    echo Please install uv or run this command from a terminal where uv is available.
    pause
    exit /b 1
)

echo Uploading pending artifacts to Notion ...
call uv run news notion-sync
if errorlevel 1 (
    echo.
    echo [ERROR] Notion sync failed. Check .env, NOTION_TOKEN, and NOTION_ROOT_PAGE_ID.
    pause
    exit /b 1
)

echo.
echo Notion upload completed.
pause
endlocal
