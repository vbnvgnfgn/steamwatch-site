@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Steam Watch - Website
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py") else (set "PY=python")
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install packages. Verify Python and internet connection.
  pause
  exit /b 1
)
start "" http://127.0.0.1:5000
%PY% app.py
pause
