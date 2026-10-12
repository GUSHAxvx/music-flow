@echo off
title Music Flow - Music Manager
cd /d "%~dp0"
python music_flow.py
if errorlevel 1 (
  echo.
  echo [!] Start failed. Check that Python is installed and on PATH.
  pause
)
