@echo off
rem ============================================================
rem  Laxinwen - 立即运行一次自动抓取（多任务）
rem  用法:
rem    run-scheduled-fetch-now.bat [job_id]
rem
rem  例如:
rem    run-scheduled-fetch-now.bat rfi-hourly
rem    run-scheduled-fetch-now.bat eco-morning
rem
rem  不带参数时，运行 data/scheduler.json 中第一个任务。
rem  立即运行会调用 headless 后台入口（news scheduled-fetch --job-id），
rem  复用现有 pipeline，不弹出 GUI。
rem  日志写入 data\logs\scheduled-fetch.log
rem ============================================================
setlocal
cd /d "%~dp0..\.."

set "JOB_ID=%~1"

echo.
echo ============================================
echo  Laxinwen - Run scheduled fetch now
echo ============================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found: .venv\Scripts\python.exe
    pause
    exit /b 1
)

rem ---- 有 job_id 参数则校验存在，无则用第一个 ----
if defined JOB_ID (
    ".venv\Scripts\python.exe" -m news scheduler status %JOB_ID% >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [ERROR] Job id "%JOB_ID%" does not exist in data\scheduler.json.
        echo Please check the task list in the GUI first.
        pause
        exit /b 1
    )
)

echo.
echo Running scheduled fetch (headless)...
if defined JOB_ID (
    ".venv\Scripts\python.exe" -m news scheduled-fetch --job-id %JOB_ID%
) else (
    ".venv\Scripts\python.exe" -m news scheduled-fetch
)
if errorlevel 1 (
    echo.
    echo [ERROR] Fetch failed. See data\logs\scheduled-fetch.log
    pause
    exit /b 1
)

echo.
echo Done. Log: data\logs\scheduled-fetch.log
echo Data: data\news.db
if exist "data\export\portable" (
    echo Export dir: data\export\portable\
)
echo.
pause
endlocal
