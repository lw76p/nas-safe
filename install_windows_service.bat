@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions

REM ============================================================
REM  TS Safe 完整版 - Windows 一键安装 / 卸载（需管理员）
REM
REM  把完整 TS Safe 引擎注册为「开机自启」的 Windows 服务，
REM  后台运行并提供 Web 控制台（默认 http://localhost:8848）。
REM  所有功能（快照 / 重复文件 / 磁盘清理 / 迁移 / 日报 / 告警）
REM  都在这台 Windows 上原生可用，无需再依赖别的设备来「代管」。
REM
REM  重要：① 请先把整个 NAS-Safe-Full.zip 解压到一个【不含中文、不含空格】
REM          的文件夹（例如 D:\TSafe），不要直接双击压缩包里的本文件；
REM        ② 右键本文件 -> 以管理员身份运行。
REM  卸载：以管理员身份运行  install_windows_service.bat uninstall
REM ============================================================

REM --- 用 8.3 短路径，彻底规避中文/空格目录导致的提权与安装失败 ---
set "DP=%~sdp0"
if not defined DP set "DP=%~dp0"
set "BAT=%~sdp0%~nx0"
if not defined BAT set "BAT=%~f0"

REM --- 0. 自提权到管理员（若尚未提权）---
fltmc >nul 2>&1
if errorlevel 1 (
    echo.
    echo [需要管理员权限] 即将弹出 Windows 用户账户控制（UAC），请点击「是」。
    echo   若不想自动提权，可关闭此窗口，改为右键本文件 - 以管理员身份运行。
    echo.
    timeout /t 2 >nul
    powershell -NoProfile -Command "Start-Process -FilePath '%BAT%' -ArgumentList 'ELEV' -Verb RunAs" >nul 2>&1
    if errorlevel 1 (
        powershell -NoProfile -Command "[System.Windows.Forms.MessageBox]::Show('无法自动获取管理员权限。请右键本文件，选择「以管理员身份运行」，再重试。', 'TS Safe 安装')" >nul 2>&1
        echo [错误] 自动提权失败，请手动以管理员身份运行本文件。
        pause
    )
    exit /b
)

REM --- 卸载分支 ---
if /i "%~1"=="uninstall" goto UNINSTALL

REM --- 1. 定位安装目录（本脚本应位于仓库根目录，含 server\ 与 web\）---
set "INSTALL_ROOT=%DP%"
set "SERVER_DIR=%INSTALL_ROOT%server"
if not exist "%SERVER_DIR%\app.py" (
    echo [错误] 未找到 %SERVER_DIR%\app.py
    echo         请把本脚本放在仓库根目录（与 server\、web\ 同级）后再运行。
    echo         也请确认你是先解压了整个 zip，而不是直接双击压缩包里的文件。
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

REM --- 2.5 预先写出《首次使用指南.txt》（任何结果下都可查看，窗口关了也能看）---
(
echo ============================================================
echo        TS Safe 完整版 - 首次使用指南
echo ============================================================
echo.
echo  ★ 第一步：打开控制台，设置管理员账号
echo      本机访问：  http://localhost:8848
echo      局域网：    http://你的局域网IP:8848
echo      （首次打开会让你创建管理员账号，请牢记）
echo.
echo  ★ 第二步（可选）：回原总控台接管这台设备
echo      本机现在已是独立的 TS Safe 主机，所有功能原生可用。
echo      想在原总控台也直接管理它：到总控台「＋添加设备 / 扫描」，
echo      它会以“TS Safe 服务端”身份出现（不再是只能监控的端点），
echo      迁移 / 快照 / 重复文件 / 磁盘清理 / 日报 等都可用。
echo.
echo  状态目录：   C:\ProgramData\NAS Safe\state
echo  日志：       C:\ProgramData\NAS Safe\state\logs\service.log
echo               C:\ProgramData\NAS Safe\state\logs\app.log
echo  卸载：       以管理员运行  install_windows_service.bat uninstall
echo ============================================================
) > "%INSTALL_ROOT%首次使用指南.txt"
echo [信息] 已生成《首次使用指南.txt》（可随时双击查看）。

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
    powershell -NoProfile -Command "[System.Windows.Forms.MessageBox]::Show('服务未能自动启动。请到 Windows「服务」手动启动 TSafeServer，再打开 http://localhost:8848 设置管理员；日志见 C:\ProgramData\NAS Safe\state\logs\', 'TS Safe 安装提示')" >nul 2>&1
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  TS Safe 已安装并启动（开机自动运行）！
echo.
echo  ★ 第一步：打开控制台设管理员账号
echo      本机：    http://localhost:8848
echo      局域网：  http://本机局域网IP:8848
echo.
echo  ★ 第二步（可选）：回到「总控台」接管这台设备
echo      在这台电脑的控制台里，它本身已是独立主机，所有功能原生可用。
echo      若想在原来的总控台里也直接管理它：到总控台「＋ 添加设备 / 扫描」，
echo      会把它识别为 TS Safe 服务端（不再是只能监控的端点），迁移/快照等都可用。
echo.
echo  状态目录：   C:\ProgramData\NAS Safe\state
echo  日志：       C:\ProgramData\NAS Safe\state\logs\service.log
echo               C:\ProgramData\NAS Safe\state\logs\app.log
echo  卸载：       以管理员运行  install_windows_service.bat uninstall
echo ============================================================
echo.
echo [信息] 即将为你打开控制台页面（首次请设置管理员账号）...
timeout /t 3 >nul
start "" "http://localhost:8848"
powershell -NoProfile -Command "[System.Windows.Forms.MessageBox]::Show('TS Safe 已安装完成！请打开 http://localhost:8848 设置管理员账号。详细步骤见同目录《首次使用指南.txt》。', 'TS Safe 安装完成')" >nul 2>&1
pause
goto :EOF

REM ============================================================
REM  卸载：停止并移除服务、删除防火墙规则（保留 venv 与 state 以便重装）
REM ============================================================
:UNINSTALL
set "INSTALL_ROOT=%DP%"
set "SERVER_DIR=%INSTALL_ROOT%server"
echo [步骤] 停止并移除 TSafeServer 服务...
net stop TSafeServer >nul 2>&1
if exist "%SERVER_DIR%\win_service.py" (
    "%INSTALL_ROOT%venv\Scripts\python.exe" "%SERVER_DIR%\win_service.py" remove >nul 2>&1
)
echo [步骤] 删除防火墙规则 TS Safe Console ...
netsh advfirewall firewall delete rule name="TS Safe Console" >nul 2>&1
echo.
echo ============================================================
echo  已卸载 TSafeServer 服务（防火墙规则已删）。
echo  说明：venv 与 C:\ProgramData\NAS Safe\state 已保留，方便重新安装；
echo        如需彻底清理，手动删除上述目录即可。
echo ============================================================
pause
endlocal
