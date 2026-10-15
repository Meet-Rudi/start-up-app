@echo off
rem MEET_RUDI Training Arena - one-time setup of the local Generator on this computer.
rem Double-click this file once. It registers the console's "Start the generator" button for your
rem Windows user (no administrator rights needed) and installs PyYAML if it is missing.
rem Uninstall-Generator.cmd removes it again.
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo Python 3 was not found. Install it from python.org - tick "Add python.exe to PATH" -
  echo then double-click this file again.
  pause
  exit /b 1
)
%PY% generator.py --install
echo.
pause
