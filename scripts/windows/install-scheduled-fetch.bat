@echo off
rem ============================================================
rem  Laxinwen - 安装/更新自动抓取定时任务（多任务）
rem  用法:
rem    install-scheduled-fetch.bat [job_id]
rem
rem  例如:
rem    install-scheduled-fetch.bat rfi-hourly
rem    install-scheduled-fetch.bat rfi-morning
rem    install-scheduled-fetch.bat eco-morning
rem
rem  不带参数时，安装 data/scheduler.json 中的第一个任务。
rem  若指定的 job_id 不存在，会给出清晰提示（不会静默失败）。
rem
rem  原理：
rem    1. 切换到项目目录（不硬编码绝对路径）
rem    2. 检查 Python 环境（.venv\Scripts\python.exe）
rem    3. 调用 news scheduler install <job_id> 创建/更新任务
rem    4. 显示任务名称与结果
rem ============================================================
setlocal
cd /d "%~dp0..\.."

set "JOB_ID=%~1"

echo.
echo ============================================
echo  Laxinwen - Install/Update scheduled task
echo ============================================

rem ---- 检查 Python 环境 ----
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python environment not found: .venv\Scripts\python.exe
    echo Please run NewsReader.bat first, or run:  uv sync --extra dev
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
) else (
    if not exist "data\scheduler.json" (
        echo.
        echo [ERROR] data\scheduler.json not found. Please create a task in the GUI first.
        pause
        exit /b 1
    )
)

echo.
echo Installing/updating scheduled task...
if defined JOB_ID (
    ".venv\Scripts\python.exe" -m news scheduler install %JOB_ID%
) else (
    ".venv\Scripts\python.exe" -m news scheduler install
)
if errorlevel 1 (
    echo.
    echo [ERROR] Install failed. See log above.
    pause
    exit /b 1
)

echo.
echo Task installed/updated.
echo To verify, run in PowerShell:
echo   schtasks /Query /TN "Laxinwen-*" /V /FO LIST
echo.
pause
endlocal
