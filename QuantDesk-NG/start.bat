@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-local.ps1"
set "START_EXIT=%ERRORLEVEL%"

if not "%START_EXIT%"=="0" (
  echo.
  echo Startup failed with exit code %START_EXIT%.
  echo Check the log files in "%~dp0logs".
  pause
)

exit /b %START_EXIT%
