@echo off
REM ============================================================
REM  TS Safe — Windows 服务一键安装（需管理员）
REM
REM  把完整 TS Safe 引擎注册为「开机自启」的 Windows 服务，
REM  后台运行并提供 Web 控制台（默认 http://localhost:8848）。
REM  所有功能（快照 / 重复文件 / 磁盘清理 / 迁移 / 日报 / 告警）
REM  都在这台 Windows 上原生可用，无需再依赖 NAS 上的「联机设备」。
REM
REM  用法：右键本文件 -> 以管理员身份运行
REM ============================================================
setlocal EnableExtensions

REM --- 0. 自提权到管理员（若尚未提权） ---
fltmc >nul 2>&1 || (
    echo [需要管理员权限] 正在请求 UAC 提权...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
    exit /b
)

REM --- 1. 定位安装目录（本脚本应位于仓库根目录，含 server\ 与 web\）---
set "INSTALL_ROOT=%~dp0"
set "SERVER_DIR=%INSTALL_ROOT%server"
if not exist "%SERVER_DIR%\app.py" (
    echo [错误] 未找到 %SERVER_DIR%\app.py
    echo         请把本脚本放在仓库根目录（与 server\、web\ 同级）后再运行。
    pause
    exit /b 1
)

REM --- 2. 选择 Python（优先 py 启动器，其次 python）---
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [错误] 未检测到 Python。请先安装 Python 3.10+（安装时勾选 "Add python.exe to PATH"）。
    echo         下载：https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [信息] 使用 Python：%PY%

REM --- 3. 创建 venv 并安装依赖 ---
if not exist "%INSTALL_ROOT%venv\Scripts\python.exe" (
    echo [步骤] 创建虚拟环境 venv ...
    %PY% -m venv "%INSTALL_ROOT%venv"
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败（可能是 Windows 缺少 VC 运行库或权限不足）。
        pause
        exit /b 1
    )
)
echo [步骤] 安装依赖（requirements.txt + pywin32）...
"%INSTALL_ROOT%venv\Scripts\pip.exe" install -r "%INSTALL_ROOT%requirements.txt" pywin32
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)
REM pywin32 在 venv 中需执行 post-install：把 pythonservice.exe 复制到 Scripts
REM 并注册为 Python 服务宿主（该步骤需要管理员权限，bat 已自提权）。
echo [步骤] 注册 pywin32 服务宿主（pywin32_postinstall）...
"%INSTALL_ROOT%venv\Scripts\python.exe" "%INSTALL_ROOT%venv\Scripts\pywin32_postinstall.py" -install

REM --- 4. 注册 Windows 服务（自动启动）---
echo [步骤] 注册 TSafeServer 服务...
"%INSTALL_ROOT%venv\Scripts\python.exe" "%SERVER_DIR%\win_service.py" install
if errorlevel 1 (
    echo [错误] 服务注册失败。
    pause
    exit /b 1
)
sc config TSafeServer start= auto >nul 2>&1

REM --- 5. 放行防火墙 TCP 8848 ---
echo [步骤] 放行防火墙 TCP 8848 ...
netsh advfirewall firewall add rule name="TS Safe Console" dir=in action=allow protocol=TCP localport=8848 >nul 2>&1

REM --- 6. 启动服务 ---
echo [步骤] 启动 TSafeServer 服务...
net start TSafeServer
if errorlevel 1 (
    echo [警告] 服务启动失败，可在「服务」中手动启动 TSafeServer，或查看日志排查。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  TS Safe 已安装并启动（开机自动运行）！
echo.
echo  本机控制台： http://localhost:8848
echo  局域网访问： http://本机局域网IP:8848
echo  状态目录：   C:\ProgramData\NAS Safe\state
echo  日志：       C:\ProgramData\NAS Safe\state\logs\service.log
echo               C:\ProgramData\NAS Safe\state\logs\app.log
echo.
echo  首次打开页面请设置管理员账号。
echo  使用 VSS 快照（Windows 卷影）需要以管理员权限运行本服务
echo  （默认服务以 LocalSystem 运行，已具备足够权限）。
echo ============================================================
pause
endlocal
