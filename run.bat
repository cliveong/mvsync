@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

rem Pick a base interpreter for the bootstrap: prefer the py launcher at 3.12
rem (winsdk wheel compatibility), then any 3.x, then plain python.
set "BOOTPY="
py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 set "BOOTPY=py -3.12"
if not defined BOOTPY (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "BOOTPY=py -3"
)
if not defined BOOTPY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "BOOTPY=python"
)
if not defined BOOTPY (
  echo [run] No Python interpreter found. Install Python 3.12 or the py launcher.
  pause
  exit /b 1
)

rem Bootstrap-only flags: never forwarded to the mvsync CLI.
if "%~1"=="--check" goto :bootstrap_only
if "%~1"=="--force-deps" goto :bootstrap_only
if "%~1"=="--skip-tools" goto :bootstrap_only

%BOOTPY% "%~dp0bootstrap.py"
if errorlevel 1 (
  echo [run] Environment bootstrap failed. See messages above.
  pause
  exit /b 1
)

set "PYTHONPATH=%~dp0src"
"%~dp0.venv\Scripts\python.exe" -m mvsync %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%

:bootstrap_only
%BOOTPY% "%~dp0bootstrap.py" %*
exit /b %ERRORLEVEL%
