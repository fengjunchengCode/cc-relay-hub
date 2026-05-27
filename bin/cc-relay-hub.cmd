@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "HUB=%SCRIPT_DIR%..\hub.py"

where python3 >nul 2>nul
if not errorlevel 1 (
  python3 -c "import sys; assert sys.version_info >= (3,9); import tomllib" >nul 2>nul
  if not errorlevel 1 (
    python3 "%HUB%" %*
    exit /b !ERRORLEVEL!
  )
  python3 -c "import sys; assert sys.version_info >= (3,9); import tomli" >nul 2>nul
  if not errorlevel 1 (
    python3 "%HUB%" %*
    exit /b !ERRORLEVEL!
  )
)

where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; assert sys.version_info >= (3,9); import tomllib" >nul 2>nul
  if not errorlevel 1 (
    python "%HUB%" %*
    exit /b !ERRORLEVEL!
  )
  python -c "import sys; assert sys.version_info >= (3,9); import tomli" >nul 2>nul
  if not errorlevel 1 (
    python "%HUB%" %*
    exit /b !ERRORLEVEL!
  )
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; assert sys.version_info >= (3,9); import tomllib" >nul 2>nul
  if not errorlevel 1 (
    py -3 "%HUB%" %*
    exit /b !ERRORLEVEL!
  )
  py -3 -c "import sys; assert sys.version_info >= (3,9); import tomli" >nul 2>nul
  if not errorlevel 1 (
    py -3 "%HUB%" %*
    exit /b !ERRORLEVEL!
  )
)

echo Error: Python 3.9+ with tomllib or tomli is required but not found. 1>&2
exit /b 1
