@echo off
rem ============================================================
rem  ECO News Reader - Windows 启动器
rem  双击本文件即可直接启动 GUI（无需先打开 PowerShell）
rem
rem  原理：
rem    1. 自动切换到项目目录（脚本所在目录）
rem    2. 检查 uv 是否存在，并优先使用项目虚拟环境 .venv
rem    3. 执行 uv run news gui
rem    4. 出错时暂停，方便查看错误信息（不瞬间关闭窗口）
rem
rem  安全说明：
rem    - 本文件不包含任何 API Key / 敏感信息
rem    - 不修改任何环境变量中的敏感信息
rem    - AI 相关配置请放在项目根目录的 .env（已被 .gitignore 排除）
rem ============================================================
setlocal

rem ---- 切换到项目目录 ----
cd /d "%~dp0"

rem ---- 检查 uv ----
where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] 未找到 uv。请先安装 uv：
    echo   打开 PowerShell 执行:
    echo     curl -LsSf https://astral.sh/uv/install.sh | sh
    echo   或参考 https://docs.astral.sh/uv/
    echo.
    echo 安装完成后重新双击本文件即可。
    pause
    exit /b 1
)

rem ---- 首次运行自动同步依赖（含 GUI 所需标准库，无需额外安装）----
if not exist ".venv\Scripts\python.exe" (
    echo [首次运行] 正在准备 Python 环境，请稍候……
    uv sync --extra dev
    if errorlevel 1 (
        echo.
        echo [错误] uv sync 失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

echo 正在启动 ECO News Reader ...
uv run news gui

if errorlevel 1 (
    echo.
    echo [错误] ECO News Reader 异常退出（错误码 %errorlevel%）。
    echo 请查看上方日志；如有需要可运行 NewsReader-Console.bat 查看完整命令行输出。
    pause
)

endlocal
