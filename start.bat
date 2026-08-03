@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo  QuantDesk - 币安 TradFi 量化工作台
echo ==========================================

if exist "QuantDesk.exe" (
    echo 启动 QuantDesk.exe ...
    :restart_exe
    "QuantDesk.exe"
    echo.
    echo [警告] 程序意外退出，10 秒后自动重启...
    timeout /t 10 /nobreak >nul
    goto restart_exe
)

echo 未找到 QuantDesk.exe，尝试源码运行...
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
:restart_py
"%PY%" "src\run.py"
echo.
echo [警告] 程序意外退出，10 秒后自动重启...
timeout /t 10 /nobreak >nul
goto restart_py
