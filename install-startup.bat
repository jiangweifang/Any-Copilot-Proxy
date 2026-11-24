@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-startup.ps1"
set "installExitCode=%ERRORLEVEL%"
echo.
pause
exit /b %installExitCode%
