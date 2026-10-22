@echo off
title NeuroRadVault
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/
  echo Tick "Add python.exe to PATH" during setup, then double-click this file again.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% -m vault %*
if errorlevel 1 pause
