@echo off
setlocal
echo Collecting read-only diagnostics. No settings will be changed.
echo Please wait for the report window to open.
if "%~1"=="" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect_install_diagnostics.ps1" -OpenReport
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect_install_diagnostics.ps1" -InstallDir "%~1" -OpenReport
)
set "collectorDiagnosticsExit=%errorlevel%"
if not "%collectorDiagnosticsExit%"=="0" echo Collection failed. Please send the error shown above.
pause
exit /b %collectorDiagnosticsExit%
