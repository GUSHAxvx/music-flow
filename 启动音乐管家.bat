@echo off
title Music Flow - 音乐管家
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [X] Python not found. Please install Python 3.10+ and add it to PATH.
  echo    没有找到 Python，请先安装 Python 3.10+ 并加入 PATH
  echo.
  pause
  exit /b 1
)

echo 正在启动 Music Flow ...
echo 浏览器会自动打开；这个窗口保持开着，关掉窗口即退出程序。
echo.
python music_flow.py
if errorlevel 1 (
  echo.
  echo [!] 启动失败，请看上面的错误信息。
  echo.
  pause
)
