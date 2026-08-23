@echo off
rem ============================================================
rem  Laxinwen - 安装/更新自动抓取定时任务
rem  双击本文件即可创建（或更新）Windows 计划任务。
rem
rem  原理：
rem    1. 切换到项目目录
rem    2. 检查 Python 环境（uv / .venv）
rem    3. 读取 data/scheduler.json 中保存的抓取参数
rem    4. 调用 news scheduler install 创建/更新任务
rem    5. 显示任务名称与结果
rem
rem  说明：
rem    - 定时抓取由 Windows 计划任务在后台执行，无需一直打开 GUI
rem    - 若尚未在 GUI 里设置过参数，则使用默认值（RFI / 每日 08:00 / 50 篇）
rem ============================================================
setlocal
cd /d "%~dp0..\.."

echo.
echo ============================================
echo  Laxinwen 自动抓取 - 安装/更新定时任务
echo ============================================

rem ---- 检查 Python 环境 ----
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到项目 Python 环境 .venv\Scripts\python.exe
    echo 请先运行 NewsReader.bat 完成环境初始化，或运行:
    echo     uv sync --extra dev
    pause
    exit /b 1
)

rem ---- 确保配置存在（无则用默认值）----
if not exist "data\scheduler.json" (
    echo [首次] 未找到 data\scheduler.json，使用默认参数（RFI / 每日 08:00 / 50 篇）...
    if not exist "data" mkdir data
    echo { > data\scheduler.json
    echo   "enabled": true, >> data\scheduler.json
    echo   "source": "rfi", >> data\scheduler.json
    echo   "frequency": "daily", >> data\scheduler.json
    echo   "time": "08:00", >> data\scheduler.json
    echo   "interval_hours": 1, >> data\scheduler.json
    echo   "limit": 50, >> data\scheduler.json
    echo   "auto_export": true, >> data\scheduler.json
    echo   "export_type": "portable" >> data\scheduler.json
    echo } >> data\scheduler.json
)

echo.
echo 正在安装/更新定时任务...
".venv\Scripts\python.exe" -m news scheduler install
if errorlevel 1 (
    echo.
    echo [错误] 安装失败，请查看上方日志。
    pause
    exit /b 1
)

echo.
echo 任务已安装/更新。如需确认，可在 PowerShell 执行:
echo   schtasks /Query /TN "Laxinwen-RFI-AutoFetch" /V /FO LIST
echo.
pause
endlocal
