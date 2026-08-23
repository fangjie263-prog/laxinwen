@echo off
rem ============================================================
rem  Laxinwen - 立即运行一次自动抓取
rem  双击本文件即触发一次后台抓取（与 Windows 定时任务同一入口）。
rem
rem  说明：
rem    - 立即运行会调用 headless 后台入口（news scheduled-fetch），
rem      复用现有 pipeline，不弹出 GUI。
rem    - 日志写入 data\logs\scheduled-fetch.log
rem ============================================================
setlocal
cd /d "%~dp0..\.."

echo.
echo ============================================
echo  Laxinwen 自动抓取 - 立即运行一次
echo ============================================

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到项目 Python 环境 .venv\Scripts\python.exe
    pause
    exit /b 1
)

echo.
echo 正在触发后台抓取（headless）...
".venv\Scripts\python.exe" -m news scheduled-fetch
if errorlevel 1 (
    echo.
    echo [错误] 抓取失败，请查看 data\logs\scheduled-fetch.log
    pause
    exit /b 1
)

echo.
echo 抓取完成。日志位置: data\logs\scheduled-fetch.log
echo 数据位置: data\news.db
if exist "data\export\portable" (
    echo 自动导出目录: data\export\portable\
)
echo.
pause
endlocal
