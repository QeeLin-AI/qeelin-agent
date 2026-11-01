@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo.
echo  QeeLin-Agent - one click demo
echo  (factor research - backtest - multi-agent decision)
echo.
call "%~dp0run.cmd" demo %*
set "RC=%ERRORLEVEL%"
echo.
echo  Done. exit code = %RC%
if exist "%~dp0runs\demo_report.md" echo  Report  : %~dp0runs\demo_report.md
if exist "%~dp0runs\decisions\latest.md" echo  Decision: %~dp0runs\decisions\latest.md
echo.
if not defined NO_PAUSE pause
