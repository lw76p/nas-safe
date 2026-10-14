@echo off
setlocal EnableExtensions
title TS Safe Installer

REM ============================================================
REM  TS Safe Windows 控制台安装器启动器（仅 ASCII，cmd 安全）
REM  真正的逻辑与中文提示都在 install_console.ps1（UTF-8 BOM）
REM
REM  用法：
REM    install_console.bat            -> 安装并启动
REM    install_console.bat uninstall  -> 卸载
REM ============================================================

set "DIR=%~dp0"
set "SDIR=%~sdp0"
if not defined SDIR set "SDIR=%~dp0"

set "PS1=%SDIR%install_console.ps1"
if not exist "%PS1%" set "PS1=%DIR%install_console.ps1"

if not exist "%PS1%" (
  echo.
  echo [TS Safe] ERROR: install_console.ps1 was not found next to this file.
  echo Please extract the whole TS-Safe-Full folder first, then run this
  echo file from inside that folder.
  echo Expected: "%DIR%install_console.ps1"
  echo.
  pause
  exit /b 1
)

set "ARG="
if /i "%~1"=="uninstall" set "ARG=-Uninstall"
if /i "%~1"=="remove"    set "ARG=-Uninstall"
if /i "%~1"=="-Uninstall" set "ARG=-Uninstall"

REM 已经在管理员上下文？fltmc 无报错即可（极少），否则请求提权
fltmc >nul 2>&1
if not errorlevel 1 goto RUN_ELEVATED

echo [TS Safe] Requesting administrator permission (click Yes on the UAC window)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%PS1%\" %ARG%'"
goto END

:RUN_ELEVATED
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %ARG%

:END
echo.
echo [TS Safe] Done. Press any key to close this window.
pause >nul
endlocal
