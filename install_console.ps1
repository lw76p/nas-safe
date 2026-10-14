#Requires -Version 5.1
<#
  TS Safe 控制台（Windows 版）一键安装 / 卸载
  ============================================================
  推荐用法：右键 install_console.bat → 「以管理员身份运行」
    install_console.ps1           安装并启动（默认端口 8848）
    install_console.ps1 -Port 9000  指定端口
    install_console.ps1 -Uninstall  卸载
  特点：
    - 复用或自动创建 Python venv（核心零第三方依赖；可选依赖按需安装）
    - 注册计划任务 TSafeServer：系统开机即运行、SYSTEM 身份、崩溃自动重启
    - 装完立即启动并做健康检查
  本脚本不依赖任何外部安装包，也不弹任何中间配置窗口。
#>

[CmdletBinding()]
param(
    [int]    $Port      = 8848,
    [string] $BindHost  = '0.0.0.0',
    [switch] $Uninstall
)

$ErrorActionPreference = 'Continue'
$Root      = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ServerDir = Join-Path $Root 'server'
$AppPy     = Join-Path $ServerDir 'app.py'
$VenvDir   = Join-Path $Root 'venv'
$VenvPy    = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPip   = Join-Path $VenvDir 'Scripts\pip.exe'
$TaskName  = 'TSafeServer'

function Write-Step($m) { Write-Host "`n[步骤] $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "  [警告] $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "  [错误] $m" -ForegroundColor Red }

# ---- 管理员校验 ----
function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Err '需要管理员权限。请右键 install_console.bat，选择「以管理员身份运行」，并在 UAC 窗口点「是」。'
        exit 2
    }
}

# ---- 卸载 ----
function Uninstall {
    Write-Step '卸载 TS Safe 控制台服务'
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Ok '已移除计划任务 TSafeServer'
    } catch { }
    try {
        [Environment]::SetEnvironmentVariable('NASSAFE_PORT', $null, 'Machine')
        Write-Ok '已清除端口环境变量'
    } catch { }
    Write-Host "`n卸载完成。程序文件仍保留在以下文件夹，如需彻底删除直接删掉即可：" -ForegroundColor White
    Write-Host "  $Root" -ForegroundColor White
}

# ---- 端口占用清理（仅结束我们自己的 app.py 进程）----
function Clear-Port {
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
        if (-not $conns) { return }
        $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
        $filter = "ProcessId IN ($($pids -join ','))"
        $procs = Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue
        foreach ($pr in $procs) {
            $isOurs = $false
            if ($pr.CommandLine -and $pr.CommandLine -like '*app.py*') {
                $isOurs = $true
            } elseif (-not $pr.CommandLine -and $pr.Name -and $pr.Name -like 'python*') {
                # 命令行读不到（多为 SYSTEM 进程在非管理员下），但该端口被 python 占用，
                # 本产品专用端口上几乎一定是我们的服务，直接结束以免端口冲突。
                $isOurs = $true
            }
            if ($isOurs) {
                Write-Warn "端口 $Port 被旧进程 PID $($pr.ProcessId) 占用，正在结束..."
                Stop-Process -Id $pr.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 1
    } catch {
        Write-Warn "端口清理跳过（不影响安装）：$($_.Exception.Message)"
    }
}

# ---- 健康检查 ----
function Wait-Health([int]$secs = 90) {
    $n = [int]($secs / 2)
    for ($i = 0; $i -lt $n; $i++) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 4
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
    }
    return $false
}

# ---- 虚拟环境 ----
function Ensure-Venv {
    if (Test-Path $VenvPy) {
        Write-Ok '复用已有 Python 虚拟环境（venv）'
        return
    }
    Write-Step '创建 Python 虚拟环境（首次约 10-30 秒）'
    $cands = @()
    try { $c = Get-Command python -ErrorAction SilentlyContinue; if ($c) { $cands += $c.Source } } catch { }
    try { $c = Get-Command py -ErrorAction SilentlyContinue; if ($c) { $cands += $c.Source } } catch { }
    $cands += @(
        'C:\Python313\python.exe', 'C:\Python312\python.exe', 'C:\Python311\python.exe',
        "$env:LocalAppData\Programs\Python\Python313\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe"
    )
    $py = $null
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { $py = $c; break } }
    if (-not $py) {
        Write-Err '未找到 Python。请先到 https://www.python.org 安装 Python 3.11+（安装时务必勾选 Add python.exe to PATH），再运行本安装。'
        exit 3
    }
    & $py -m venv $VenvDir
    if (-not (Test-Path $VenvPy)) { Write-Err 'venv 创建失败，请检查 Python 安装是否完整。'; exit 3 }
    Write-Ok "venv 已创建（使用 $py）"
    $req = Join-Path $Root 'requirements.txt'
    if (Test-Path $req) {
        Write-Step '安装可选依赖（失败不影响核心功能启动）'
        & $VenvPip install -r $req 2>&1 | Out-Null
    }
}

# ---- 注册计划任务 ----
function Register-Task {
    $action    = New-ScheduledTaskAction -Execute $VenvPy -Argument 'app.py' -WorkingDirectory $ServerDir
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                           -Principal $principal -Settings $settings -Force | Out-Null
}

# ===================== 主流程 =====================
Assert-Admin

if ($Uninstall) { Uninstall; exit 0 }

if (-not (Test-Path $AppPy)) {
    Write-Err "未找到 $AppPy 。请确认整个程序文件夹已完整解压，并从文件夹内部运行本脚本。"
    exit 1
}

Write-Step '开始安装 TS Safe 控制台（Windows）'
Ensure-Venv
[Environment]::SetEnvironmentVariable('NASSAFE_PORT', "$Port", 'Machine')
Write-Ok "端口已写入系统环境变量：NASSAFE_PORT = $Port"

Write-Step '清理可能残留的旧实例'
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch { }
Clear-Port

Write-Step '注册开机自启服务（计划任务 TSafeServer）'
Register-Task
Write-Ok '计划任务已注册（开机自启 / SYSTEM / 崩溃自动重启）'

Write-Step '启动服务'
try { Start-ScheduledTask -TaskName $TaskName } catch {
    Write-Warn "Start-ScheduledTask 失败：$($_.Exception.Message)"
}
Start-Sleep -Seconds 3

Write-Step "健康检查 http://127.0.0.1:$Port/api/health"
if (Wait-Health) {
    Write-Host "`n============================================" -ForegroundColor Green
    Write-Host " [成功] TS Safe 控制台已安装并启动！" -ForegroundColor Green
    Write-Host " 访问地址： http://localhost:$Port/" -ForegroundColor White
    Write-Host " 开机自启： 是（无需登录也会运行）" -ForegroundColor White
    Write-Host " 重新安装： 重新运行 install_console.bat" -ForegroundColor White
    Write-Host " 卸载服务： install_console.bat uninstall" -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Green
} else {
    Write-Warn "服务已启动但健康检查暂未通过（可能仍在初始化，或端口被其它程序占用）。"
    Write-Warn "请稍候在浏览器打开 http://localhost:$Port/ 确认；若打不开，查看 $ServerDir 目录日志或重跑安装。"
}
