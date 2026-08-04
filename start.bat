@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==========================================
echo  QuantDesk V2 - MySQL
echo ==========================================

set "PYTHON_BIN=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_BIN=.venv\Scripts\python.exe"
set "PYTHONPATH=%CD%\src"

set "MARIADB_HOME=%CD%\.local\mariadb-12.3.2-winx64"
set "MARIADB_DATA=%CD%\.local\mariadb-data"
if exist "%MARIADB_HOME%\bin\mariadb-admin.exe" (
  "%MARIADB_HOME%\bin\mariadb-admin.exe" --host=127.0.0.1 --port=3306 --user=quantdesk --password=4eb43b3914e7419b9f3015e6e18b75c7 --disable-ssl ping >nul 2>&1
  if errorlevel 1 (
    echo Starting local MariaDB...
    start "" /b "%MARIADB_HOME%\bin\mariadbd.exe" --defaults-file="%MARIADB_DATA%\my.ini" --console
    timeout /t 5 /nobreak >nul
  )
)

:restart
echo Starting QuantDesk V2 on port 8200...
"%PYTHON_BIN%" -m quantdesk_v2.cli serve
echo.
echo [Warning] Service exited unexpectedly. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto restart
