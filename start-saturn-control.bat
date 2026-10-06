@echo off
setlocal
cd /d "%~dp0"
title Saturn Control

rem ---- find Python (py launcher first, then python) ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python 3.9+ was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)

rem ---- first run: create venv and install requirements (re-installs if requirements.txt changes) ----
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time...
  %PY% -m venv .venv || (echo Could not create virtual environment & pause & exit /b 1)
  del /q ".venv\req.stamp" 2>nul
)
fc /b requirements.txt ".venv\req.stamp" >nul 2>nul
if errorlevel 1 (
  echo Installing requirements...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (echo Install failed - check your internet connection & pause & exit /b 1)
  copy /y requirements.txt ".venv\req.stamp" >nul
)

rem ---- run (the app opens your browser itself) ----
".venv\Scripts\python.exe" -m saturn_control
echo.
echo Server stopped.
pause
