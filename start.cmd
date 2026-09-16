@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0app.py" %*
) else (
  python "%~dp0app.py" %*
)
if errorlevel 1 (
  echo 启动失败。请安装 Python 3.10 或更高版本，并确保 py 或 python 可用。
  pause
)
