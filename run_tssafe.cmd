@echo off
set "NASSAFE_STATE_DIR=C:\ProgramData\NAS Safe\state"
set "NASSAFE_WEB_DIR=D:\TSafe\NAS-Safe-Windows-Full\web"
set "NASSAFE_PORT=8848"
set "NASSAFE_BIND_HOST=0.0.0.0"
cd /d "D:\TSafe\NAS-Safe-Windows-Full\server"
"D:\TSafe\NAS-Safe-Windows-Full\venv\Scripts\python.exe" app.py