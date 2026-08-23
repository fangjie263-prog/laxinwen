@echo off
rem ============================================================
rem  Laxinwen - 删除自动抓取定时任务
rem  双击本文件即可删除 Windows 计划任务，停止自动抓取。
rem ============================================================
setlocal
cd /d "%~dp0..\.."

echo.
echo ============================================
echo  Laxinwen 自动抓取 - 删除定时任务
echo ============================================

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到项目 Python 环境 .venv\Scripts\python.exe
    pause
    exit /b 1
)

echo.
echo 正在删除定时任务...
".venv\Scripts\python.exe" -m news scheduler delete
if errorlevel 1 (
    echo.
    echo [错误] 删除失败，请检查任务是否存在。
    pause
    exit /b 1
)

echo.
echo 定时任务已删除。
echo 可在 PowerShell 执行以下命令确认任务不存在:
echo   schtasks /Query /TN "Laxinwen-RFI-AutoFetch"
echo.
pause
endlocal
