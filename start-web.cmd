@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem 端口：命令行里给了 --port 就用给的，否则 8000
set "PORT=8000"
set "ARGS=%*"
echo %ARGS% | findstr /c:"--port" >nul
if not errorlevel 1 (
  for %%a in (%*) do (
    if defined NEXT set "PORT=%%a" & set "NEXT="
    if /i "%%a"=="--port" set "NEXT=1"
  )
)

echo.
echo  QeeLin-Agent web console
echo  打开 http://127.0.0.1:!PORT!   （Ctrl+C 停止）
echo.

rem 已经带 --port 就直接透传，避免重复
echo %ARGS% | findstr /c:"--port" >nul
if not errorlevel 1 (
  call "%~dp0run.cmd" web %*
) else (
  call "%~dp0run.cmd" web --port !PORT! %*
)
echo.
if not defined NO_PAUSE pause
