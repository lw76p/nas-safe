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
$DiagFile  = Join-Path $Root 'install_diag.txt'
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

$Diag = New-Object System.Collections.ArrayList
function Add-Diag { param($t) [void]$Diag.Add([string]$t) }

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

function Save-Diag {
    try {
        $utf8Bom = New-Object System.Text.UTF8Encoding($true)
        [System.IO.File]::WriteAllText($DiagFile, (($Diag -join "`r`n") + "`r`n"), $utf8Bom)
    } catch { }
}

function Pause-End {
    Save-Diag
    Write-Host ''
    Read-Host '按回车键关闭本窗口' | Out-Null
}

function Get-RichDiag {
    $sb = New-Object System.Text.StringBuilder
    $null = $sb.AppendLine('【服务状态】')
    try {
        $gs = Get-Service -Name $SvcName -ErrorAction Stop
        $null = $sb.AppendLine('  Get-Service : ' + $gs.Status + '   (StartType=' + $gs.StartType + ')')
    } catch {
        $null = $sb.AppendLine('  Get-Service : 服务未创建或无法查询')
    }
    try {
        $q = & sc.exe query $SvcName 2>&1
        $null = $sb.AppendLine('  sc query    : ' + ((($q | ForEach-Object { $_.ToString() }) -join ' ').Trim()))
    } catch {
        $null = $sb.AppendLine('  sc query    : (本机不可用)')
    }
    $null = $sb.AppendLine('')
    foreach ($n in @('service.log', 'startup_error.log', 'app.log')) {
        $lp = Join-Path $LogDir $n
        $null = $sb.AppendLine("【$n】")
        if (Test-Path $lp) {
            $tt = (Get-Content $lp -Tail 20 -ErrorAction SilentlyContinue) -join "`n"
            if ([string]::IsNullOrWhiteSpace($tt)) { $null = $sb.AppendLine('  (文件存在但为空)') }
            else { $null = $sb.AppendLine($tt) }
        } else {
            $null = $sb.AppendLine('  (文件不存在 —— 服务进程很可能根本没启动)')
        }
        $null = $sb.AppendLine('')
    }
    $null = $sb.AppendLine('【系统事件日志：最近与本服务相关的记录】')
    try {
        $ev = @(Get-EventLog -LogName System -SourceName 'Service Control Manager' -Newest 20 -ErrorAction Stop |
                Where-Object { "$($_.Message)" -like "*$SvcName*" })
        if ($ev.Count -gt 0) {
            foreach ($e in $ev) {
                $msg = "$($e.Message)" -replace "`r`n", ' ' -replace "`n", ' '
                $null = $sb.AppendLine('  ' + $e.TimeGenerated + '  ' + $e.EntryType + ' : ' + $msg)
            }
        } else {
            $null = $sb.AppendLine('  (无相关事件 —— 服务可能从未被系统尝试启动)')
        }
    } catch {
        $null = $sb.AppendLine('  (读取事件日志失败: ' + $_.Exception.Message + ')')
    }
    return $sb.ToString()
}

function Wait-Console {
    param([int]$Seconds = 90, [int]$PortNum = 8848)
    $steps = [int]($Seconds / 2)
    for ($i = 0; $i -lt $steps; $i++) {
        Start-Sleep -Seconds 2
        try {
            $rr = Invoke-WebRequest -Uri "http://127.0.0.1:$PortNum/" -UseBasicParsing -TimeoutSec 4
            if ($rr.StatusCode -eq 200) { return $true }
        } catch { }
    }
    return $false
}

function Register-FallbackTask {
    $taskName = 'TSafeServer'
    try {
        $action = New-ScheduledTaskAction -Execute $VenvPy -Argument 'app.py' -WorkingDirectory $ServerDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
                               -Principal $principal -Settings $settings -Force | Out-Null
        Add-Diag "已注册降级计划任务：$taskName"
        return $true
    } catch {
        Add-Diag "Register-ScheduledTask 失败: $($_.Exception.Message)"
        return $false
    }
}

# ---------------------------------------------------------------- 管理员校验
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Err '没有管理员权限，无法安装 Windows 服务。'
    Add-Diag '没有管理员权限'
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

    # 老系统回退：解析 netstat（在 PowerShell 里解析，不存在 cmd 转义问题）
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
            Add-Diag "端口 $TargetPort 被 $($o.Name) PID $($o.Id) 占用（非本程序进程，中止）"
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
        Add-Diag "端口 $TargetPort 释放失败"
        Show-Popup -Title 'TS Safe 端口仍被占用' -Text $msg -IsError
        return $false
    }
    return $true
}

# ---------------------------------------------------------------- Python 探测（重写版）
# 关键点：
#  1) 调用外部程序必须用 splatting（& $Exe @pre）传参数数组；写成 & $Exe $a 会把
#     整个数组当成一个参数，导致版本检测命令本身就是坏的（曾导致误报"没找到 Python"）。
#  2) 版本检测只用 -V（输出 "Python 3.13.7"），不拼接 -c 代码，避开引号转义地狱。
#  3) 候选来源要全：PATH、py 启动器的各版本选择器、注册表 InstallPath、常见安装目录。

function Test-PythonExe {
    param([string]$ExePath, [string[]]$PreArgs = @())
    if (-not (Test-Path -LiteralPath $ExePath -ErrorAction SilentlyContinue)) { return $null }
    try {
        $res = & $ExePath @PreArgs '-V' 2>&1
        $line = $res | Select-Object -Last 1
        if ($null -eq $line) { return $null }
        $txt = $line.ToString()
        if ($txt -match 'Python\s+(\d+)\.(\d+)\.?(\d+)?') {
            $maj = [int]$Matches[1]
            $min = [int]$Matches[2]
            $pat = [int]$Matches[3]
            if ($maj -ge 3 -and $min -ge 9) {
                return [pscustomobject]@{
                    Exe = $ExePath
                    Pre = $PreArgs
                    Ver = "$maj.$min.$pat"
                }
            }
        }
    } catch { }
    return $null
}

function Find-SystemPython {
    $cands = New-Object System.Collections.ArrayList

    # 1) py 启动器：先 -3（最新版），再逐个具体版本
    foreach ($sel in @('-3', '-3.14', '-3.13', '-3.12', '-3.11', '-3.10', '-3.9')) {
        [void]$cands.Add(@{ Cmd = 'py'; Pre = @($sel) })
    }
    # 2) PATH 上的 python / python3
    foreach ($n in @('python', 'python3')) {
        [void]$cands.Add(@{ Cmd = $n; Pre = @() })
    }
    # 3) 注册表里登记的安装位置（HKLM / HKCU）
    foreach ($rk in @('HKLM:\SOFTWARE\Python\PythonCore', 'HKCU:\SOFTWARE\Python\PythonCore')) {
        try {
            $subs = Get-ChildItem $rk -ErrorAction SilentlyContinue
            foreach ($s in $subs) {
                try {
                    $ip = (Get-Item $s.PSPath -ErrorAction SilentlyContinue).GetValue('')
                } catch { $ip = $null }
                if ($ip) {
                    [void]$cands.Add(@{ Cmd = (Join-Path $ip 'python.exe'); Pre = @() })
                }
            }
        } catch { }
    }
    # 4) 常见安装目录
    foreach ($pat in @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                       "C:\Python3*\python.exe",
                       "C:\Program Files\Python3*\python.exe",
                       "C:\Program Files (x86)\Python3*\python.exe")) {
        try {
            $found = Get-ChildItem -Path $pat -ErrorAction SilentlyContinue
            foreach ($f in $found) {
                [void]$cands.Add(@{ Cmd = $f.FullName; Pre = @() })
            }
        } catch { }
    }

    foreach ($c in $cands) {
        $full = $c.Cmd
        if (-not (Test-Path -LiteralPath $full -ErrorAction SilentlyContinue)) {
            $g = Get-Command $c.Cmd -ErrorAction SilentlyContinue
            if (-not $g) {
                Add-Diag ("候选不可用: " + $c.Cmd + " " + ($c.Pre -join ' '))
                continue
            }
            $full = $g.Source
        }
        $r = Test-PythonExe -ExePath $full -PreArgs $c.Pre
        if ($r) {
            Add-Diag ("找到 Python: " + $r.Exe + " " + ($r.Pre -join ' ') + " -> " + $r.Ver)
            return $r
        }
        Add-Diag ("候选版本不合格或无响应: " + $full + " " + ($c.Pre -join ' '))
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
    if (Test-Path -LiteralPath $VenvPy) {
        $pExe = $VenvPy; $pPre = @()
    } else {
        $found = Find-SystemPython
        if ($found) { $pExe = $found.Exe; $pPre = @($found.Pre) } else { $pExe = $null; $pPre = @() }
    }
    if ($pExe) {
        & $pExe @pPre (Join-Path $ServerDir 'win_service.py') remove 2>$null | Out-Null
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
    Add-Diag '安装包不完整：缺少 server\app.py'
    Show-Popup -Title 'TS Safe 安装失败' -Text "没找到 server\app.py。`n请不要直接运行压缩包里的文件：先把整个 zip 解压到一个不含中文、不含空格的文件夹（例如 D:\TSafe），再从该文件夹运行 install_windows_service.bat。" -IsError
    Pause-End
    exit 1
}
if (-not (Test-Path (Join-Path $ServerDir 'win_service.py'))) {
    Write-Err "没找到 server\win_service.py，安装包不完整，请重新下载。"
    Add-Diag '安装包不完整：缺少 server\win_service.py'
    Show-Popup -Title 'TS Safe 安装失败' -Text '安装包不完整（缺少 server\win_service.py），请重新下载 NAS-Safe-Full.zip 并完整解压。' -IsError
    Pause-End
    exit 1
}

# 1. 准备 Python 运行环境
#    优先复用本目录里已存在的 venv（重装/续装场景根本不需要再找系统 Python）
Write-Step '检查 Python 运行环境...'
$PyExe = $null
$PyPre = @()

if (Test-Path -LiteralPath $VenvPy) {
    $ok = Test-PythonExe -ExePath $VenvPy -PreArgs @()
    if ($ok) {
        $PyExe = $VenvPy
        $PyPre = @()
        Write-Ok "复用本目录已存在的运行环境（Python $($ok.Ver)）"
        Add-Diag "复用已有 venv: $($ok.Ver)"
    } else {
        Write-Warn '本目录的运行环境已损坏，将重建...'
        Add-Diag '已有 venv 损坏，准备重建'
        try { Remove-Item -Recurse -Force $VenvDir -ErrorAction Stop } catch { }
    }
}

if (-not $PyExe) {
    $found = Find-SystemPython
    if (-not $found) {
        Write-Err '没找到可用的 Python 3.9 或更高版本。'
        Add-Diag '未找到可用的 Python 3.9+（详见本目录 install_diag.txt）'
        Show-Popup -Title 'TS Safe 缺少 Python' -Text "TS Safe 需要 Python 3.9 或更高版本。`n本机没找到可用的 Python。`n`n将要打开 Python 官方下载页：安装时务必勾选「Add python.exe to PATH」，`n装完后再重新运行本安装程序。`n`n（排查明细见安装目录下的 install_diag.txt）" -IsError
        Start-Process 'https://www.python.org/downloads/'
        Pause-End
        exit 1
    }
    $PyExe = $found.Exe
    $PyPre = @($found.Pre)
    Write-Ok "使用系统 Python $($found.Ver)：$PyExe $($PyPre -join ' ')"

    Write-Step '创建独立运行环境（venv），约 10-30 秒...'
    & $PyExe @PyPre -m venv $VenvDir
    $created = Test-PythonExe -ExePath $VenvPy -PreArgs @()
    if (-not $created) {
        Write-Err '虚拟环境创建失败。'
        Add-Diag "venv 创建失败：$PyExe $($PyPre -join ' ')"
        Show-Popup -Title 'TS Safe 安装失败' -Text '创建 Python 虚拟环境失败。请确认 Python 安装完整（含 pip / venv），然后重新运行安装。' -IsError
        Pause-End
        exit 1
    }
    $PyExe = $VenvPy
    $PyPre = @()
    Write-Ok '运行环境就绪。'
}

# 2. 装依赖
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
$depOk = Test-PythonExe -ExePath $VenvPy -PreArgs @()
if (-not $depOk) {
    Write-Err '依赖安装失败，运行环境不可用。'
    Add-Diag '依赖安装失败，venv python 不可用'
    Show-Popup -Title 'TS Safe 安装失败' -Text '依赖安装失败，常见原因是网络不通。请连网后重新运行安装。' -IsError
    Pause-End
    exit 1
}
Write-Ok '依赖安装完成。'

# 3. 注册 pywin32 服务宿主
Write-Step '注册 pywin32 服务宿主，约 30 秒-1 分钟...'
Write-Warn '这一步也会较慢，请勿关闭窗口。'
$post = Join-Path $VenvDir 'Scripts\pywin32_postinstall.py'
if (-not (Test-Path $post)) { $post = Join-Path $VenvDir 'Lib\site-packages\pywin32_system32\pywin32_postinstall.py' }
if (Test-Path $post) {
    & $VenvPy $post -install 2>$null | Out-Null
}
Write-Ok 'pywin32 注册完成。'

# 3.5 引擎自检：先绕开 Windows 服务，直接跑一次 app.py。
#     这样一旦引擎本身有问题，能立刻拿到真实的 Python 报错，
#     而不是只看到一句「服务启动了但 8848 没响应」无从下手。
Write-Step '引擎自检（直接启动 app.py 验证，约 10-60 秒）...'
$selfOut = Join-Path $Root 'engine_selftest.log'
$selfErr = Join-Path $Root 'engine_selftest.err'
foreach ($f in @($selfOut, $selfErr)) {
    if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}
$selfProc = $null
try {
    $selfProc = Start-Process -FilePath $VenvPy -ArgumentList 'app.py' -WorkingDirectory $ServerDir `
                -RedirectStandardOutput $selfOut -RedirectStandardError $selfErr -PassThru -WindowStyle Hidden
} catch {
    Write-Err "自检进程启动失败：$($_.Exception.Message)"
    Add-Diag "自检启动失败: $($_.Exception.Message)"
}
$selfOk = $false
if ($selfProc) {
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 2
        if ($selfProc.HasExited) { break }
        try {
            $rr = Invoke-WebRequest -Uri 'http://127.0.0.1:8848/api/system' -UseBasicParsing -TimeoutSec 4
            if ($rr.StatusCode -eq 200) { $selfOk = $true; break }
        } catch { }
    }
    if (-not $selfOk -and $selfProc.HasExited) { Add-Diag ('自检进程提前退出 rc=' + $selfProc.ExitCode) }
    if (-not $selfProc.HasExited) {
        try { Stop-Process -Id $selfProc.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
    Start-Sleep -Seconds 2
}
if ($selfOk) {
    Write-Ok '引擎自检通过（app.py 能正常提供服务）。'
    Add-Diag '引擎自检通过'
} else {
    $so = ''
    $se = ''
    if (Test-Path $selfOut) { $so = (Get-Content $selfOut -Tail 25 -ErrorAction SilentlyContinue) -join "`n" }
    if (Test-Path $selfErr) { $se = (Get-Content $selfErr -Tail 25 -ErrorAction SilentlyContinue) -join "`n" }
    Write-Err '引擎自检失败：app.py 本身跑不起来，所以服务也起不来。'
    Add-Diag "引擎自检失败。`r`n--- stdout ---`r`n$so`r`n--- stderr ---`r`n$se"
    Save-Diag
    Show-Popup -Title 'TS Safe 引擎自检失败' -Text "app.py 本身启动失败，因此 Windows 服务也起不来。`n`n--- 错误输出(stderr) ---`n$se`n--- 标准输出(stdout) ---`n$so`n`n完整诊断已写入：`n$DiagFile`n$selfErr" -IsError
    Pause-End
    exit 1
}

# 4. 清理可能残留的旧服务
Write-Step '清理可能残留的旧 TSafeServer 服务...'
& net.exe stop $SvcName 2>$null | Out-Null
Start-Sleep -Seconds 2
& $VenvPy (Join-Path $ServerDir 'win_service.py') remove 2>$null | Out-Null
& sc.exe delete $SvcName 2>$null | Out-Null
Start-Sleep -Seconds 1

# 5. 端口占用安全处理
Write-Step "检查并安全释放 $Port 端口..."
if (-not (Ensure-PortFree -TargetPort $Port)) {
    Pause-End
    exit 1
}
Write-Ok "$Port 端口可用。"

# 6. 准备状态目录 + 环境变量
Write-Step '准备数据目录与环境变量...'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir  | Out-Null
[Environment]::SetEnvironmentVariable('NASSAFE_STATE_DIR', $StateDir, 'Machine')
[Environment]::SetEnvironmentVariable('NASSAFE_WEB_DIR',    $WebDir,   'Machine')
[Environment]::SetEnvironmentVariable('NASSAFE_PORT',       "$Port",   'Machine')
[Environment]::SetEnvironmentVariable('NASSAFE_BIND_HOST',  '0.0.0.0', 'Machine')
Write-Ok "数据目录：$StateDir"

# 7. 注册服务
Write-Step '注册 TSafeServer 服务（开机自启）...'
$env:NASSAFE_STATE_DIR = $StateDir
$env:NASSAFE_WEB_DIR   = $WebDir
$env:NASSAFE_PORT      = "$Port"
$env:NASSAFE_BIND_HOST = '0.0.0.0'
& $VenvPy (Join-Path $ServerDir 'win_service.py') install
if ($LASTEXITCODE -ne 0) {
    Write-Err '服务注册失败。'
    Add-Diag 'win_service.py install 返回非 0'
    Show-Popup -Title 'TS Safe 服务注册失败' -Text "服务注册失败，请查看上面窗口的红色报错。`n日志目录：$LogDir`n排查明细：$DiagFile" -IsError
    Pause-End
    exit 1
}
& sc.exe config $SvcName start= auto 2>$null | Out-Null
& sc.exe failure $SvcName reset= 86400 actions= restart/5000/restart/5000/restart/5000 2>$null | Out-Null
Write-Ok '服务已注册（开机自启）。'

# 8. 防火墙
Write-Step "放行防火墙 TCP $Port ..."
& netsh.exe advfirewall firewall delete rule name="$FwRule" 2>$null | Out-Null
& netsh.exe advfirewall firewall add rule name="$FwRule" dir=in action=allow protocol=TCP localport=$Port 2>$null | Out-Null
Write-Ok '防火墙已放行。'

# 9. 启动服务
Write-Step '启动 TSafeServer 服务...'
$netOut = & net.exe start $SvcName 2>&1
$netRc = $LASTEXITCODE
$netTxt = (($netOut | ForEach-Object { $_.ToString() }) -join ' ').Trim()
Write-Host ('    net start 返回：' + $netRc + '  ' + $netTxt) -ForegroundColor DarkGray
Add-Diag "net start rc=$netRc out=$netTxt"
Start-Sleep -Seconds 3
$svcState = '未创建'
try { $svcState = (Get-Service -Name $SvcName -ErrorAction Stop).Status } catch { }
Write-Host ('    服务当前状态：' + $svcState) -ForegroundColor DarkGray
Add-Diag "服务状态：$svcState"

# 10. 健康检查：先用「Windows 服务」方式，不行就自动降级为「登录自启计划任务」
$runMode = 'Windows 服务（开机自启、后台运行）'
Write-Step '等待控制台就绪（服务方式，最多 90 秒）...'
$ok = Wait-Console -Seconds 90
$richDiag = ''

if (-not $ok) {
    Write-Warn '服务方式未就绪，正在收集诊断信息...'
    $richDiag = Get-RichDiag
    Add-Diag "服务方式控制台未就绪。`r`n$richDiag"
    Save-Diag
    Write-Host $richDiag -ForegroundColor DarkYellow

    Write-Step '自动改用「登录自启计划任务」方式（不依赖 Windows 服务封装）...'
    if (Register-FallbackTask) {
        try { Start-ScheduledTask -TaskName 'TSafeServer' } catch { Add-Diag "Start-ScheduledTask 失败: $($_.Exception.Message)" }
        Write-Warn '已创建计划任务 TSafeServer（SYSTEM 身份、登录自动启动、崩溃自动重启），等待引擎就绪...'
        if (Wait-Console -Seconds 90) {
            $ok = $true
            $runMode = '登录自启计划任务（Windows 服务方式在本机不可用，已自动切换；功能完全一样）'
            Write-Ok "控制台已就绪（$runMode）。"
        }
    } else {
        Write-Warn '计划任务方式注册失败（可能是系统版本不支持）。'
    }
}

if (-not $ok) {
    Write-Err '两种方式都没能把控制台跑起来。'
    $manual = "& '$VenvPy' '$ServerDir\app.py'"
    Show-Popup -Title 'TS Safe 启动失败' -Text @"
服务方式和计划任务方式都没能让控制台响应。

$richDiag

【你可以这样处理】
1. 先重启一次电脑（服务/计划任务都会随开机自动启动）；
2. 重启后仍不行，用管理员 PowerShell 手动前台跑一次引擎看真实报错：
   $manual
3. 把本弹窗内容、或安装目录下的 install_diag.txt 发给我。
"@ -IsError
    Pause-End
    exit 1
}

# 11. 写《首次使用指南》
Write-Step '生成《首次使用指南.txt》...'
$guidePath = Join-Path $Root '首次使用指南.txt'
$guide = @"
TS Safe 完整版 — 首次使用指南
========================================

安装位置：$Root
运行方式：$runMode
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

# 12. 打开控制台 + 弹窗
Write-Step '打开控制台页面...'
try { Start-Process "http://localhost:$Port" } catch { }

Write-Host ''
Write-Host '============================================' -ForegroundColor Green
Write-Host ' 安装完成！' -ForegroundColor Green
Write-Host '============================================' -ForegroundColor Green

Add-Diag '安装完成'
Show-Popup -Title 'TS Safe 安装完成' -Text @"
TS Safe 安装完成。
当前运行方式：$runMode

接下来请做两件事：
1. 浏览器已打开 http://localhost:$Port —— 注册一个管理员账号
2. 如果你是想把这台电脑交回原来的「总控台」统一管理：
   回到原总控台 → 联机设备 → 添加设备/扫描 → 选中这台电脑接管，
   之后迁移/快照/清理/日报等功能就都能用了。

本目录下的《首次使用指南.txt》随时可以双击查看。
"@

Pause-End
exit 0
