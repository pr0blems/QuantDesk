@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==========================================
echo  QuantDesk V2 - MySQL
echo ==========================================

set "PYTHON_BIN=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_BIN=.venv\Scripts\python.exe"
set "PYTHONPATH=%CD%\src"

:restart
echo Starting QuantDesk V2 on port 8200...
"%PYTHON_BIN%" -m quantdesk_v2.cli serve
echo.
echo [Warning] Service exited unexpectedly. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto restart
