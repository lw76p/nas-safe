# TS Safe — Windows 完整版安装 / 卸载（PowerShell 主脚本，UTF-8 with BOM）
#
# 说明：install_windows_service.bat 只是纯 ASCII 启动器，真正的安装逻辑全在本文件。
# 这样做的原因：cmd.exe 解析含中文的 .bat 极易出编码/转义问题，PowerShell（UTF-8 BOM）
# 则完全可靠。
#
# 用法（需管理员，bat 会自动提权）：
#   install_windows_service.bat            安装
#   install_windows_service.bat uninstall  卸载

param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ---------------------------------------------------------------- 基础配置
$Root      = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }
$ServerDir = Join-Path $Root 'server'
$WebDir    = Join-Path $Root 'web'
$VenvDir   = Join-Path $Root 'venv'
$VenvPy    = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPip   = Join-Path $VenvDir 'Scripts\pip.exe'
$StateDir  = 'C:\ProgramData\NAS Safe\state'
$LogDir    = Join-Path $StateDir 'logs'
$Port      = 8848
$SvcName   = 'TSafeServer'
$FwRule    = 'TS Safe 8848'

# 只允许结束这些进程名（防止误杀系统进程 —— 绝不使用无差别 taskkill）
$SafeProcessNames = @('python', 'pythonw', 'py', 'pythonservice')

# ---------------------------------------------------------------- 输出辅助
function Write-Step { param($t) Write-Host ''; Write-Host "[步骤] $t" -ForegroundColor Cyan }
function Write-Ok   { param($t) Write-Host "[完成] $t" -ForegroundColor Green }
function Write-Warn { param($t) Write-Host "[提示] $t" -ForegroundColor Yellow }
function Write-Err  { param($t) Write-Host "[错误] $t" -ForegroundColor Red }

function Show-Popup {
    param($Title, $Text, [switch]$IsError)
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $icon = 'Information'
        if ($IsError) { $icon = 'Error' }
        [System.Windows.Forms.MessageBox]::Show($Text, $Title, 'OK', $icon) | Out-Null
    } catch {
        Write-Host ''
        Write-Host '============== ' $Title ' ==============' -ForegroundColor Yellow
        Write-Host $Text
        Write-Host '==========================================' -ForegroundColor Yellow
    }
}

function Pause-End {
    Write-Host ''
    Read-Host '按回车键关闭本窗口' | Out-Null
}

# ---------------------------------------------------------------- 管理员校验
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Err '没有管理员权限，无法安装 Windows 服务。'
    Show-Popup -Title 'TS Safe 需要管理员权限' -Text '请右键 install_windows_service.bat，选择「以管理员身份运行」，并在弹出的 UAC 窗口点「是」。' -IsError
    Pause-End
    exit 1
}

# ---------------------------------------------------------------- 端口工具（安全）
function Get-PortListener {
    param([int]$TargetPort)
    $list = @()
    try {
        $conns = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction Stop
        foreach ($c in $conns) {
            $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
            $name = if ($p) { $p.ProcessName } else { '<unknown>' }
            $list += [pscustomobject]@{ Id = $c.OwningProcess; Name = $name }
        }
        return $list
    } catch { }

    # 老系统回退：解析 netstat（PowerShell 里解析，不存在 cmd 转义问题）
    try {
        $lines = & netstat.exe -ano -p TCP 2>$null
        foreach ($l in $lines) {
            if ($l -notmatch 'LISTENING') { continue }
            $f = -split ($l.ToString())
            if ($f.Count -lt 5) { continue }
            if ($f[1] -notmatch "[:.]$TargetPort$") { continue }
            $pidv = 0
            [void][int]::TryParse($f[4], [ref]$pidv)
            if ($pidv -le 0) { continue }
            $p = Get-Process -Id $pidv -ErrorAction SilentlyContinue
            $name = if ($p) { $p.ProcessName } else { '<unknown>' }
            $list += [pscustomobject]@{ Id = $pidv; Name = $name }
        }
    } catch { }
    return $list
}

function Ensure-PortFree {
    param([int]$TargetPort)
    $owners = @(Get-PortListener -TargetPort $TargetPort)
    if ($owners.Count -eq 0) { return $true }

    # 先全部判定，任何一个"非本程序进程"就整体放弃，绝不误杀
    foreach ($o in $owners) {
        $n = ($o.Name).ToLowerInvariant()
        if ($o.Id -le 4 -or $SafeProcessNames -notcontains $n) {
            $msg = "本机 {0} 端口已被占用：`n进程 {1} (PID {2})`n`n这不是 TS Safe 自己的进程，为安全起见本脚本不会结束它。`n请手动关闭该程序后重新运行安装；`n或换个端口（设置环境变量 NASSAFE_PORT 后重装）。" -f $TargetPort, $o.Name, $o.Id
            Write-Err "端口 $TargetPort 被 [$($o.Name) PID $($o.Id)] 占用，且不是 TS Safe 进程，已安全中止。"
            Show-Popup -Title 'TS Safe 端口被占用' -Text $msg -IsError
            return $false
        }
    }

    foreach ($o in $owners) {
        Write-Warn "结束上一次安装残留的本程序进程：$($o.Name) (PID $($o.Id))"
        try { Stop-Process -Id $o.Id -Force -ErrorAction Stop } catch { }
    }
    Start-Sleep -Seconds 2

    $left = @(Get-PortListener -TargetPort $TargetPort)
    if ($left.Count -gt 0) {
        $msg = "端口 {0} 仍然被占用，请重启电脑后再次运行安装。" -f $TargetPort
        Write-Err $msg
        Show-Popup -Title 'TS Safe 端口仍被占用' -Text $msg -IsError
        return $false
    }
    return $true
}

# ---------------------------------------------------------------- Python 探测
function Test-Python {
    param([string]$Exe, [string]$Pre)
    try {
        $a = @()
        if ($Pre) { $a += $Pre }
        $a += @('-c', 'import sys;print("%d.%d" % sys.version_info[:2])')
        $out = & $Exe $a 2>$null
        if (-not $out) { return $null }
        $s = ($out | Select-Object -Last 1).ToString().Trim()
        $v = [version]$s
        if ($v.Major -ge 3 -and $v.Minor -ge 10) {
            if ($Pre) { return @($Exe, $Pre) }
            return @($Exe)
        }
    } catch { }
    return $null
}

function Find-Python {
    foreach ($e in @('py', 'python', 'python3')) {
        $cmd = Get-Command $e -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        foreach ($pre in @('-3', $null)) {
            $r = Test-Python -Exe $cmd.Source -Pre $pre
            if ($r) { return $r }
        }
    }
    return $null
}

# ================================================================ 卸载分支
if ($Uninstall) {
    Write-Host '============================================' -ForegroundColor Cyan
    Write-Host ' TS Safe 卸载' -ForegroundColor Cyan
    Write-Host '============================================' -ForegroundColor Cyan

    Write-Step '停止并移除 TSafeServer 服务...'
    & net.exe stop $SvcName 2>$null | Out-Null
    Start-Sleep -Seconds 2
    $py = Find-Python
    if ($py) {
        $exe = $py[0]; $rest = @()
        if ($py.Count -gt 1) { $rest = @($py[1]) }
        & $exe @rest (Join-Path $ServerDir 'win_service.py') remove 2>$null | Out-Null
    }
    & sc.exe delete $SvcName 2>$null | Out-Null

    Write-Step '删除防火墙放行规则...'
    & netsh.exe advfirewall firewall delete rule name="$FwRule" 2>$null | Out-Null

    Write-Step '清理环境变量...'
    foreach ($k in @('NASSAFE_STATE_DIR', 'NASSAFE_WEB_DIR', 'NASSAFE_PORT', 'NASSAFE_BIND_HOST')) {
        [Environment]::SetEnvironmentVariable($k, $null, 'Machine')
    }

    Write-Ok '卸载完成。'
    Show-Popup -Title 'TS Safe 已卸载' -Text "服务、防火墙规则已移除。`n程序文件仍在：$Root`n状态数据仍在：$StateDir`n如需彻底清理，手动删除上述目录即可。"
    Pause-End
    exit 0
}

# ================================================================ 安装流程
Write-Host '============================================' -ForegroundColor Cyan
Write-Host ' TS Safe 完整版 — Windows 一键安装' -ForegroundColor Cyan
Write-Host ' 安装目录：' $Root -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor Cyan

# 0. 完整性校验
if (-not (Test-Path (Join-Path $ServerDir 'app.py'))) {
    Write-Err "没找到 server\app.py。请把整个 NAS-Safe-Full.zip 解压后再运行安装。"
    Show-Popup -Title 'TS Safe 安装失败' -Text "没找到 server\app.py。`n请不要直接运行压缩包里的文件：先把整个 zip 解压到一个不含中文、不含空格的文件夹（例如 D:\TSafe），再从该文件夹运行 install_windows_service.bat。" -IsError
    Pause-End
    exit 1
}
if (-not (Test-Path (Join-Path $ServerDir 'win_service.py'))) {
    Write-Err "没找到 server\win_service.py，安装包不完整，请重新下载。"
    Show-Popup -Title 'TS Safe 安装失败' -Text '安装包不完整（缺少 server\win_service.py），请重新下载 NAS-Safe-Full.zip 并完整解压。' -IsError
    Pause-End
    exit 1
}

# 1. 定位 Python
Write-Step '检查 Python 环境...'
$pycmd = Find-Python
if (-not $pycmd) {
    Write-Err '没找到 Python 3.10 或更高版本。'
    Show-Popup -Title 'TS Safe 缺少 Python' -Text "TS Safe 需要 Python 3.10 或更高版本。`n即将打开 Python 官方下载页，安装时请勾选「Add Python to PATH」，装完再重新运行本安装程序。" -IsError
    Start-Process 'https://www.python.org/downloads/'
    Pause-End
    exit 1
}
$PyExe  = $pycmd[0]
$PyPre  = @()
if ($pycmd.Count -gt 1) { $PyPre = @($pycmd[1]) }
Write-Ok "使用 Python：$PyExe $($PyPre -join ' ')"

# 2. 建虚拟环境
if (-not (Test-Path $VenvPy)) {
    Write-Step '创建独立运行环境（venv），约 10-30 秒...'
    & $PyExe @PyPre -m venv $VenvDir
    if (-not (Test-Path $VenvPy)) {
        Write-Err 'venv 创建失败。'
        Show-Popup -Title 'TS Safe 安装失败' -Text '创建 Python 虚拟环境失败，请确认 Python 安装完整（勾选了 pip / venv），然后重新运行安装。' -IsError
        Pause-End
        exit 1
    }
}
Write-Ok '运行环境就绪。'

# 3. 装依赖
Write-Step '安装运行依赖（含 pywin32）...'
Write-Warn 'pywin32 需要向 Windows 注册服务组件，这一步可能需要 1-3 分钟，窗口不动是正常现象，请勿关闭！'
$req = Join-Path $Root 'requirements.txt'
$pipArgs = @('install', '--timeout', '300', '--retries', '3', 'pywin32')
if (Test-Path $req) { $pipArgs += @('-r', $req) }
& $VenvPip @pipArgs
if ($LASTEXITCODE -ne 0) {
    Write-Warn '第一次安装未完全成功，重试一次...'
    & $VenvPip @pipArgs
}
if (-not (Test-Path $VenvPy)) {
    Write-Err '依赖安装失败。'
    Show-Popup -Title 'TS Safe 安装失败' -Text '依赖安装失败，常见原因是网络不通。请连网后重新运行安装。' -IsError
    Pause-End
    exit 1
}
Write-Ok '依赖安装完成。'

# 4. 注册 pywin32 服务宿主
Write-Step '注册 pywin32 服务宿主，约 30 秒-1 分钟...'
Write-Warn '这一步也会较慢，请勿关闭窗口。'
$post = Join-Path $VenvDir 'Scripts\pywin32_postinstall.py'
if (-not (Test-Path $post)) { $post = Join-Path $VenvDir 'Lib\site-packages\pywin32_system32\pywin32_postinstall.py' }
if (Test-Path $post) {
    & $VenvPy $post -install 2>$null | Out-Null
}
Write-Ok 'pywin32 注册完成。'

# 5. 清理可能残留的旧服务
Write-Step '清理可能残留的旧 TSafeServer 服务...'
& net.exe stop $SvcName 2>$null | Out-Null
Start-Sleep -Seconds 2
& $VenvPy (Join-Path $ServerDir 'win_service.py') remove 2>$null | Out-Null
& sc.exe delete $SvcName 2>$null | Out-Null
Start-Sleep -Seconds 1

# 6. 端口占用安全处理
Write-Step "检查并安全释放 $Port 端口..."
if (-not (Ensure-PortFree -TargetPort $Port)) {
    Pause-End
    exit 1
}
Write-Ok "$Port 端口可用。"

# 7. 准备状态目录 + 环境变量
Write-Step '准备数据目录与环境变量...'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir  | Out-Null
[Environment]::SetEnvironmentVariable('NASSAFE_STATE_DIR', $StateDir, 'Machine')
[Environment]::SetEnvironmentVariable('NASSAFE_WEB_DIR',    $WebDir,   'Machine')
[Environment]::SetEnvironmentVariable('NASSAFE_PORT',       "$Port",   'Machine')
[Environment]::SetEnvironmentVariable('NASSAFE_BIND_HOST',  '0.0.0.0', 'Machine')
Write-Ok "数据目录：$StateDir"

# 8. 注册服务
Write-Step '注册 TSafeServer 服务（开机自启）...'
$env:NASSAFE_STATE_DIR = $StateDir
$env:NASSAFE_WEB_DIR   = $WebDir
$env:NASSAFE_PORT      = "$Port"
$env:NASSAFE_BIND_HOST = '0.0.0.0'
& $VenvPy (Join-Path $ServerDir 'win_service.py') install
if ($LASTEXITCODE -ne 0) {
    Write-Err '服务注册失败。'
    Show-Popup -Title 'TS Safe 服务注册失败' -Text "服务注册失败，请查看上面窗口的红色报错。`n日志目录：$LogDir" -IsError
    Pause-End
    exit 1
}
& sc.exe config $SvcName start= auto 2>$null | Out-Null
& sc.exe failure $SvcName reset= 86400 actions= restart/5000/restart/5000/restart/5000 2>$null | Out-Null
Write-Ok '服务已注册（开机自启）。'

# 9. 防火墙
Write-Step "放行防火墙 TCP $Port ..."
& netsh.exe advfirewall firewall delete rule name="$FwRule" 2>$null | Out-Null
& netsh.exe advfirewall firewall add rule name="$FwRule" dir=in action=allow protocol=TCP localport=$Port 2>$null | Out-Null
Write-Ok '防火墙已放行。'

# 10. 启动服务
Write-Step '启动 TSafeServer 服务...'
& net.exe start $SvcName 2>$null | Out-Null

# 11. 健康检查
Write-Step '等待控制台就绪（最多 40 秒）...'
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if (-not $ok) {
    $tail = ''
    $appLog = Join-Path $LogDir 'app.log'
    if (Test-Path $appLog) { $tail = (Get-Content $appLog -Tail 15 -ErrorAction SilentlyContinue) -join "`n" }
    $errLog = Join-Path $LogDir 'startup_error.log'
    $extra = ''
    if (Test-Path $errLog) { $extra = (Get-Content $errLog -Tail 15 -ErrorAction SilentlyContinue) -join "`n" }
    Write-Err '服务已安装，但控制台暂时没响应。'
    Show-Popup -Title 'TS Safe 服务未就绪' -Text "服务已注册，但控制台暂时没响应。`n请尝试：重启电脑后访问 http://localhost:$Port`n`n日志目录：$LogDir`n$extra`n$tail" -IsError
    Pause-End
    exit 1
}
Write-Ok '控制台已就绪。'

# 12. 写《首次使用指南》
Write-Step '生成《首次使用指南.txt》...'
$guidePath = Join-Path $Root '首次使用指南.txt'
$guide = @"
TS Safe 完整版 — 首次使用指南
========================================

安装位置：$Root
控制台地址：http://localhost:$Port
数据目录：$StateDir

【第一步】设置管理员账号（必须做）
----------------------------------
1. 打开（或等待自动打开） http://localhost:$Port
2. 页面会弹出「注册管理员账号」，填用户名 + 密码 + 邮箱 → 注册
3. 不注册只能看页面，快照 / 防勒索 / 设备管控等全部功能都用不了

【第二步】回到原来的「总控台」接管这台设备（如果你是从别的设备上下载的这个安装包）
--------------------------------------------------------------------------------
1. 回到你原来那台总控台（比如 NAS 上的 TS Safe）
2. 进入「联机设备 / 添加设备 / 扫描」，扫描这台电脑
3. 这台电脑会以「TS Safe 服务端」的身份出现——
   此时迁移 / 快照 / 重复文件 / 磁盘清理 / 日报 / 告警 等功能
   在这台电脑上就全部可用了

【日常使用】
----------------------------------
- 服务名：TSafeServer，开机自动启动，无需手动打开任何窗口
- 想看提醒可另外安装「桌面小助手」
- 停止服务：管理员命令行运行  net stop TSafeServer
- 启动服务：管理员命令行运行  net start TSafeServer
- 卸载：管理员运行  install_windows_service.bat uninstall
- 日志目录：$LogDir
"@
$utf8Bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($guidePath, $guide, $utf8Bom)
Write-Ok "已生成：$guidePath"

# 13. 打开控制台 + 弹窗
Write-Step '打开控制台页面...'
try { Start-Process "http://localhost:$Port" } catch { }

Write-Host ''
Write-Host '============================================' -ForegroundColor Green
Write-Host ' 安装完成！' -ForegroundColor Green
Write-Host '============================================' -ForegroundColor Green

Show-Popup -Title 'TS Safe 安装完成' -Text @"
TS Safe 已安装为 Windows 服务（开机自启、后台运行）。

接下来请做两件事：
1. 浏览器已打开 http://localhost:$Port —— 注册一个管理员账号
2. 如果你是想把这台电脑交回原来的「总控台」统一管理：
   回到原总控台 → 联机设备 → 添加设备/扫描 → 选中这台电脑接管，
   之后迁移/快照/清理/日报等功能就都能用了。

本目录下的《首次使用指南.txt》随时可以双击查看。
"@

Pause-End
exit 0
