@echo off
setlocal
echo SelfMedia Content Collector - continuous memory recording
echo Keep this window open. You may minimize it.
echo When the error appears, return here and press ENTER to build the ZIP.
echo No automatic upload, app restart, database access or startup task.
echo.
if not exist "%~dp0collect_runtime_diagnostics.ps1" (
    echo Missing collect_runtime_diagnostics.ps1. Extract all ZIP files first.
    pause
    exit /b 1
)
if "%~1"=="" (
    powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0collect_runtime_diagnostics.ps1" -Monitor -OpenFolder
) else (
    powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0collect_runtime_diagnostics.ps1" -Monitor -InstallDir "%~1" -OpenFolder
)
set "runtimeMemoryExit=%errorlevel%"
if not "%runtimeMemoryExit%"=="0" echo Recording failed. Keep the evidence folder and send the error above.
pause
exit /b %runtimeMemoryExit%
