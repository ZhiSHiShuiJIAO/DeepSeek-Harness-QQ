@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=C:\Users\%USERNAME%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

echo 正在启动整合包...
"%PY%" "%~dp0app.py"
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo 启动失败，错误码 %CODE%。把上面的报错发给我。
  pause
)
