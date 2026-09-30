# 一键彻底清理 NAS Safe 桌面助手（新版 + 旧版 NASSafeAgent）
# 右键「使用 PowerShell 运行」或在 PowerShell 中执行均可
$ErrorActionPreference = "SilentlyContinue"

function Stop-AgentProcess {
    param([string]$name)
    # 先按进程名结束（不带 .exe）
    $base = $name -replace '\.exe$',''
    Get-Process | Where-Object { $_.ProcessName -like "*$base*" } | Stop-Process -Force
    # 再按 taskkill /F /IM 兜底
    Start-Process -FilePath "taskkill.exe" -ArgumentList "/F","/IM","$name" -WindowStyle Hidden -Wait
}

# 1) 结束所有相关进程（新版 + 旧版 + Python 脚本方式）
$names = @("桌面助手.exe", "NASSafeAgent.exe", "pythonw.exe", "python.exe")
foreach ($n in $names) {
    Stop-AgentProcess $n
}
# 额外按路径兜底：任何路径里带 NASSafeAgent / NAS Safe 的进程
Get-Process | Where-Object {
    $_.Path -like "*NASSafeAgent*" -or $_.Path -like "*NAS Safe*"
} | Stop-Process -Force

# 等它们真正退出
Start-Sleep -Seconds 2

# 2) 删除安装目录
$dirs = @(
    (Join-Path $env:APPDATA "NAS Safe 桌面助手"),
    (Join-Path $env:APPDATA "NASSafeAgent")
)
foreach ($dir in $dirs) {
    if (Test-Path $dir) {
        Remove-Item -Path $dir -Recurse -Force
        Write-Host "已删除安装目录：$dir" -ForegroundColor Green
    } else {
        Write-Host "安装目录已不存在：$dir" -ForegroundColor Yellow
    }
}

# 3) 删除开机自启项（当前用户）
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$keys = @("NAS Safe 桌面助手", "NASSafeAgent", "nassafe-agent")
foreach ($k in $keys) {
    try {
        Remove-ItemProperty -Path $regPath -Name $k -Force
        Write-Host "已删除开机自启项：$k" -ForegroundColor Green
    } catch {}
}

# 4) 删除 nassafe-agent 自定义协议（当前用户）
$protocolKey = "HKCU:\Software\Classes\nassafe-agent"
if (Test-Path $protocolKey) {
    Remove-Item -Path $protocolKey -Recurse -Force
    Write-Host "已删除自定义协议：nassafe-agent" -ForegroundColor Green
}

# 5) 确认 18765 端口已释放
$tcp = Get-NetTCPConnection -LocalPort 18765 -ErrorAction SilentlyContinue
if ($tcp) {
    $tcp | ForEach-Object {
        $proc = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        Write-Host "端口 18765 仍被占用：PID=$($_.OwningProcess) $($proc.ProcessName)" -ForegroundColor Red
        try { Stop-Process -Id $_.OwningProcess -Force } catch {}
    }
    Start-Sleep -Seconds 1
} else {
    Write-Host "端口 18765 已释放" -ForegroundColor Green
}

Write-Host "`n清理完成。请刷新 NAS Safe 网页，点击「安装 / 重装小助手」重新下载安装。" -ForegroundColor Green
pause
