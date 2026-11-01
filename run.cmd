@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYEXE="
if not "%PYTHON%"=="" set "PYEXE=%PYTHON%"

if not defined PYEXE (
  if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" (
    set "PYEXE=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
  )
)
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  echo [x] Python not found. Install Python 3.10+ or:  set PYTHON=C:\path\to\python.exe
  exit /b 1
)

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONPATH=%~dp0src

"!PYEXE!" -m cryptoagent %*
exit /b %ERRORLEVEL%
