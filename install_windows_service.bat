@echo off
setlocal EnableExtensions
title TS Safe Installer

REM ============================================================
REM  TS Safe Windows installer launcher (ASCII only, cmd-safe).
REM  All real logic + all Chinese text lives in install_windows.ps1
REM  (UTF-8 with BOM) so cmd never has to parse non-ASCII bytes.
REM
REM  Usage:
REM    install_windows_service.bat            -> install
REM    install_windows_service.bat uninstall  -> uninstall
REM ============================================================

set "DIR=%~dp0"
set "SDIR=%~sdp0"
if not defined SDIR set "SDIR=%~dp0"

set "PS1=%SDIR%install_windows.ps1"
if not exist "%PS1%" set "PS1=%DIR%install_windows.ps1"

if not exist "%PS1%" (
  echo.
  echo [TS Safe] ERROR: install_windows.ps1 was not found next to this file.
  echo Please extract the whole NAS-Safe-Full.zip to a folder first,
  echo then run this file from inside that folder.
  echo Expected: "%DIR%install_windows.ps1"
  echo.
  pause
  exit /b 1
)

set "ARG="
if /i "%~1"=="uninstall" set "ARG=-Uninstall"
if /i "%~1"=="remove" set "ARG=-Uninstall"

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
