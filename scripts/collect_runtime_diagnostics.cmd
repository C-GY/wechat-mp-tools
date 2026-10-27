@echo off
setlocal
echo SelfMedia Content Collector - Out of Memory diagnostics
echo Keep the error window open. This tool reads local evidence only.
echo No Python, admin rights or Internet connection required.
echo.
if not exist "%~dp0collect_runtime_diagnostics.ps1" (
    echo Missing collect_runtime_diagnostics.ps1. Extract all ZIP files first.
    pause
    exit /b 1
)
if "%~1"=="" (
    powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0collect_runtime_diagnostics.ps1" -OpenFolder
) else (
    powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0collect_runtime_diagnostics.ps1" -InstallDir "%~1" -OpenFolder
)
set "runtimeDiagnosticsExit=%errorlevel%"
if not "%runtimeDiagnosticsExit%"=="0" echo Collection failed. Please send the error displayed above.
pause
exit /b %runtimeDiagnosticsExit%
