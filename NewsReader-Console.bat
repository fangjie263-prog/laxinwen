@echo off
rem ============================================================
rem  Laxinwen News Reader - 控制台启动器
rem  与 NewsReader.bat 相同，但保留命令行窗口，
rem  便于查看抓取/AI 的完整命令行日志。
rem ============================================================
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 uv。请先安装 uv 后重试。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [首次运行] 正在准备 Python 环境，请稍候……
    uv sync --extra dev
    if errorlevel 1 (
        echo [错误] uv sync 失败。
        pause
        exit /b 1
    )
)

echo 正在启动 Laxinwen News Reader（控制台模式）...
uv run news gui -v

echo.
echo Laxinwen News Reader 已退出（错误码 %errorlevel%）。
pause
endlocal
