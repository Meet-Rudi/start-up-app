@echo off
rem MEET_RUDI Training Arena - removes the Generator's start link for your Windows user.
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo Python 3 was not found.
  pause
  exit /b 1
)
%PY% generator.py --uninstall
echo.
pause
