@echo off
rem ============================================================
rem  Laxinwen - 删除自动抓取定时任务（多任务）
rem  用法:
rem    delete-scheduled-fetch.bat [job_id]
rem
rem  例如:
rem    delete-scheduled-fetch.bat rfi-hourly
rem    delete-scheduled-fetch.bat eco-morning
rem
rem  不带参数时，删除 data/scheduler.json 中第一个任务对应的计划任务。
rem  删除一个任务不影响其它任务。
rem ============================================================
setlocal
cd /d "%~dp0..\.."

set "JOB_ID=%~1"

echo.
echo ============================================
echo  Laxinwen - Delete scheduled task
echo ============================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found: .venv\Scripts\python.exe
    pause
    exit /b 1
)

echo.
echo Deleting scheduled task...
if defined JOB_ID (
    ".venv\Scripts\python.exe" -m news scheduler delete %JOB_ID%
) else (
    ".venv\Scripts\python.exe" -m news scheduler delete
)
if errorlevel 1 (
    echo.
    echo [ERROR] Delete failed. Check whether the task exists.
    pause
    exit /b 1
)

echo.
echo Task deleted.
echo To confirm, run in PowerShell:
echo   schtasks /Query /TN "Laxinwen-*"
echo.
pause
endlocal
