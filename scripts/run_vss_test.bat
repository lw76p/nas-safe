@echo off
rem NAS Safe VSS backend self-test (auto-elevate)
REM > must stay pure ASCII path. Runs the full chain: create/list/browse/restore/delete.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
set PY=C:\Users\aa\.workbuddy\binaries\python\versions\3.13.12\python.exe
set OUT=%TEMP%\vss_test_out.txt
"%PY%" "C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nas-safe-clone\scripts\test_vss_elevated.py" "%OUT%"
echo.
echo ================ RESULT ================
type "%OUT%"
echo.
pause
