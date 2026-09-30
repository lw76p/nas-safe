# 一键清理 NAS Safe 桌面助手旧版进程与安装目录
# 右键「使用 PowerShell 运行」或在管理员/普通 PowerShell 中执行均可
$ErrorActionPreference = "SilentlyContinue"

$names = @("桌面助手.exe", "NASSafeAgent.exe", "desktop_agent.py")
foreach ($n in $names) {
    Get-Process | Where-Object { $_.ProcessName -like "*$n*" -or $_.Path -like "*NAS Safe*" } | Stop-Process -Force
}

# 再强制 taskkill 一轮（处理上面漏掉的）
foreach ($n in $names) {
    Start-Process -FilePath "taskkill.exe" -ArgumentList "/F","/IM","$n" -WindowStyle Hidden -Wait
}

# 删除安装目录
$dir = Join-Path $env:APPDATA "NAS Safe 桌面助手"
if (Test-Path $dir) {
    Remove-Item -Path $dir -Recurse -Force
    Write-Host "已删除安装目录：$dir" -ForegroundColor Green
} else {
    Write-Host "安装目录已不存在：$dir" -ForegroundColor Yellow
}

# 删除开机自启项（当前用户）
$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Remove-ItemProperty -Path $regPath -Name "NASSafeAgent" -Force
Remove-ItemProperty -Path $regPath -Name "NAS Safe 桌面助手" -Force
Remove-ItemProperty -Path $regPath -Name "nassafe-agent" -Force

# 删除旧版英文自启项
Remove-ItemProperty -Path $regPath -Name "NASSafeAgent" -Force

Write-Host "清理完成。现在可以刷新 NAS Safe 网页，下载最新安装包重新安装。" -ForegroundColor Green
pause
