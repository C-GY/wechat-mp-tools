@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect_channels_diagnostics.ps1"
set "channelsDiagnosticsExit=%errorlevel%"
if not "%channelsDiagnosticsExit%"=="0" echo Diagnostic collection failed. Please send the error displayed above.
pause
exit /b %channelsDiagnosticsExit%
