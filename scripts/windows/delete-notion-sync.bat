@echo off
setlocal
echo Deleting Laxinwen-Notion-Sync ...
schtasks /Delete /TN "Laxinwen-Notion-Sync" /F
if errorlevel 1 (
    echo [ERROR] Could not delete the Windows task.
    pause
    exit /b 1
)
echo Task deleted.
endlocal
