# JT-PC 接入 NAS 转控 —— 一键开启 WinRM 步骤

> 目标：让 NAS 上的 TS Safe 能远程管控你那台 Windows 电脑（JT-PC）。
> 原理：用 Windows **自带的** WinRM 远程管理能力，**电脑端零安装**任何 TS Safe 软件。
> 只需在电脑上右键运行一次脚本，之后开机自动在线。

---

## 第一步：下载脚本（在 JT-PC 上做）

在 **JT-PC 这台 Windows 电脑**上，打开任意浏览器，访问：

```
http://192.168.8.62:8848/api/downloads/enable-winrm.ps1
```

浏览器会下载一个名为 **`enable-winrm.ps1`** 的文件（默认在"下载"文件夹）。

> 如果这个网址打不开，也说明 JT-PC 和 NAS 不在同一局域网，先确认网段连通。
> 备用方案见文末"手动新建脚本"。

---

## 第二步：右键运行脚本（在 JT-PC 上做）

1. 打开"下载"文件夹，找到 `enable-winrm.ps1`。
2. **右键**它 → 选 **"使用 PowerShell 运行"**。
   - 会弹 **UAC 管理员确认**，点 **"是"**。
3. 如果右键菜单里**没有**"使用 PowerShell 运行"这个选项：
   - 右键"开始"菜单 → **"Windows PowerShell(管理员)"**（或"终端(管理员)"）。
   - 先允许运行脚本，复制执行：
     ```powershell
     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
     ```
   - 再运行脚本（把路径换成你实际下载的位置）：
     ```powershell
     & "$env:USERPROFILE\Downloads\enable-winrm.ps1"
     ```

脚本会自动完成五件事：开启 WinRM 服务并设为开机自启、放行防火墙（仅本地子网 + NAS 地址）、允许局域网 Basic 认证、解除本地管理员远程登录的 UAC 令牌降权、把当前账户加进「Remote Management Users」组，最后**打印出这台电脑的 IP 地址**。

看到绿色大字 **"开启完成！"** 和下面的 IP，就成功了。

> ⚠️ 前置条件：你的 **Windows 管理员账户必须有开机密码**。没密码的话远程登录会被拒绝——
> 先到 Windows 设置里给管理员账户加个密码，再跑脚本。

---

## 第三步：在 NAS 控制台添加这台电脑

1. 打开 NAS 上的 TS Safe 网页控制台（就是刚才下载脚本那个 `192.168.8.62:8848`）。
2. 进入 **设备管理 → 添加电脑**。
3. 填写：
   - **IP 地址**：第二步脚本打印出来的 JT-PC 的 IP（类似 `192.168.8.xx`）。
   - **账号**：JT-PC 的 Windows 管理员账户名（脚本里"当前账户"那行显示的就是）。
   - **密码**：该管理员账户的登录密码。
4. 点保存。

---

## 第四步：验证转控

添加后设备状态应显示 **"在线"**。然后逐项试一下转控能力：

- **状态查询**：看 CPU / 内存 / 磁盘。
- **磁盘扫描**：列文件、算大小、可选做哈希。
- **远程重启 / 关机**：确认能下发。
- **执行命令**：发一条命令看返回。

全部能通，说明 NAS↔电脑 转控链路打通。

---

## 之后还要做吗？

不用。WinRM 已设为开机自启，JT-PC 每次开机自动在线，NAS 控制台里它一直在列表里。
除非你重装了系统或关掉了 WinRM，否则不用再跑脚本。

---

## 附：手动新建脚本（下载不顺时）

如果浏览器下载失败，可在 JT-PC 上新建一个文本文件，命名为 `enable-winrm.ps1`，
把下面内容整段贴进去保存，再按第二步运行即可（内容由 NAS 自动生成，此处为同款）：

```powershell
# ============================================================
# TS Safe 电脑端一键开启（只需要运行一次）
# 作用：打开 Windows 自带的远程管理(WinRM)，让 NAS 上的 TS Safe 可以远程管控这台电脑。
# 说明：用的是 Windows 官方能力，不需要安装任何 TS Safe 软件。
# 用法：右键本文件 → 使用 PowerShell 运行（会弹管理员确认，点是）
# ============================================================
#requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "==> 1/5 启动 WinRM 服务并设为开机自动..." -ForegroundColor Cyan
Enable-PSRemoting -Force -SkipNetworkProfileCheck | Out-Null
Set-Service WinRM -StartMode Automatic
Start-Service WinRM

Write-Host "==> 2/5 放行防火墙（仅本子网和总控台地址）..." -ForegroundColor Cyan
# 注意：Get-NetFirewallRule 的 -Direction 与 -Name 分属不同参数集，不能同时写，
# 否则脚本在这一步直接崩。先按名字取，再在管道里过滤方向。
Get-NetFirewallRule -Name "WINRM*" -ErrorAction SilentlyContinue |
    Where-Object { $_.Direction -eq "Inbound" } |
    ForEach-Object { Set-NetFirewallRule -Name $_.Name -RemoteAddress "LocalSubnet,192.168.8.62" }

Write-Host "==> 3/5 允许 Basic 认证（局域网一次性方案）..." -ForegroundColor Cyan
winrm set winrm/config/service/auth '@{Basic="true"}' | Out-Null
winrm set winrm/config/service '@{AllowUnencrypted="true"}' | Out-Null

Write-Host "==> 4/5 放行本地管理员的远程登录令牌（UAC 不再降权）..." -ForegroundColor Cyan
# 工作组 / Azure AD 加入的机器，本地管理员远程登录会被 UAC 令牌过滤降权，
# 必须设 LocalAccountTokenFilterPolicy=1，否则账号密码对了也连不上。
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" ^
    /v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f | Out-Null

Write-Host "==> 5/5 把当前账户加进远程管理组并检查账户..." -ForegroundColor Cyan
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Host "    当前账户：$me"
net localgroup "Remote Management Users" /add "$me" 2>$null | Out-Null

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " 开启完成！这台电脑的 IP 地址是：" -ForegroundColor Green
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" } |
    ForEach-Object { Write-Host "   $($_.IPAddress)" -ForegroundColor Yellow }
Write-Host ""
Write-Host " 下一步：打开 NAS 的 TS Safe 网页 → 设备管理 → 添加电脑，把上面的 IP 填进去，" -ForegroundColor Green
Write-Host " 账号密码用管理员账户的登录密码。" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
```

> 注意：上面手动版里防火墙范围写死成了 `192.168.8.62`（NAS 地址）。若 NAS 地址变了，
> 把那行 `LocalSubnet,192.168.8.62` 里的 IP 改成实际 NAS IP 即可；自动下载版会根据你访问的网址自动填。

---

## 多台电脑 / 多台 NAS 怎么办？

**Windows 电脑（几台就要跑几次）**
每台 Windows 电脑都要**各跑一次**这个脚本。原因：WinRM 是 Windows **自带**的组件，
脚本不是"装软件"，只是把开关打开（起服务 + 开防火墙 + 开 Basic + 授权）。
一次生效，之后开机自动在线，不用重复跑。

**NAS（可装可不装，取决于想不想让它被纳管）**
- 每台 NAS 各装一份 = 各自一个独立控制台，各管各的账号/设备/快照，互不干扰。
- 也可以只在**一台主 NAS** 上装，然后在控制台用「自动扫描」发现局域网里其他
  跑着 TS Safe 的 NAS，把它们作为**联机设备**纳管进来（填 IP + 端口 + 访问令牌），
  实现一个控制台看全部。
- 注意：被纳管的 NAS 自己得跑着 TS Safe，否则只有"在线/离线"，拿不到指标和快照。

**Mac 电脑（当前版本管不了）**
TS Safe 管 Windows 走的是 **WinRM**，这是 Windows 专属协议，Mac 上没有。
所以 Mac 现阶段**不能被这样远程转控**（远程执行 / 重启 / 关机都不行）。
想在 Mac 上用 TS Safe，只能在 Mac 本机装一份（走 `install_service.sh`），
让它成为一台独立的控制台、或被主 NAS 纳管为一个联机设备。

---

## Linux / Mac 电脑怎么接入？（不是装那个脚本）

先说清楚：**`enable-winrm.ps1` 没有 Linux/Mac 版本，也不需要**——它是 Windows
专属的"开开关"脚本。Linux 和 Mac 的接入方式不同：**直接装 TS Safe 完整版**。
装完之后这台机器就自带完整引擎，主动向 NAS 控制台报到（每 60 秒心跳），
在总控台里能看到它的完整指标（CPU / 内存 / 磁盘 / 快照 / 防勒索）。

### 安装步骤（Linux 和 Mac 通用，共 4 步）

1. **下载安装包**：打开 NAS 控制台 → 点对应设备卡片 → 「设备管控」→
   点 **「⬇ 下载完整版安装包（含安装引导）」**，得到 `NAS-Safe-Full.zip`。
   （包里自动带了一张"报到票据"，装完会自动回到这台 NAS 的控制台登记在线；
   也可以直接在目标电脑的浏览器里打开 `http://192.168.8.62:8848` 下载。）
2. **拷到目标电脑并解压**：U 盘 / 空投 / scp 都行。
3. **装 Python（≥3.10，如果没有）**：
   - macOS：终端跑 `python3 --version`，低于 3.10 或没有就
     `brew install python`（或去 python.org 下载安装包）。
   - Linux（Ubuntu/Debian）：`sudo apt install python3 python3-venv`。
4. **运行安装脚本**（在解压出来的目录里）：
   ```bash
   # Linux（会用 systemd 注册开机自启，需要 root）：
   sudo bash install_service.sh

   # macOS（用用户级 launchd 注册开机自启，不需要 sudo）：
   bash install_service.sh
   ```
   卸载：同命令末尾加 `uninstall`。

装完它会：创建独立 venv → 注册开机自启服务 → 控制台开在本机
`http://localhost:8848` → 自动向来源总控台（NAS）报到并保持心跳。
回到 NAS 控制台，这台设备就会从「离线/仅监控」变成**在线 + 完整指标**。

### 和 Windows 接入的区别（一句话）

| | Windows | Linux / Mac |
|---|---|---|
| 装什么 | **什么都不装**，跑一次 enable-winrm.ps1 | 装 TS Safe 完整版 |
| 在线方式 | NAS 主动连它（WinRM） | 它主动向 NAS 报到（心跳） |
| 能看到 | 状态 + 远程执行/重启/关机 | 完整指标 + 它自身的快照/清理等 API |
| 前提 | 管理员账户 + 同网段 | Python ≥3.10 + 网络可达 NAS |

---

## 实测记录（2026-10-14，JT-PC 真机）

| 项目 | 结果 |
|---|---|
| 本机 | JT-PC，Windows 11 IoT 企业版 LTSC，IP 192.168.8.31 |
| 账户 | 因本机是 **Azure AD 加入**，WinRM Basic 只认本地 SAM 账户，故新建专用本地管理员 `tsafe` |
| 连通测试 | 通过（PowerShell 5） |
| 状态采集 | CPU / 内存 / 4 块盘 / 防火墙三档 / Defender 全部取到 |
| 远程执行 | `1+1` 返回 `2`，耗时约 1 秒 |
| 远程扫描 | 4 个文件中正确识别出 3 个重复文件 |

**两个高频坑（脚本已自动处理，手动配制时留意）**
1. **防火墙参数**：`Get-NetFirewallRule -Direction Inbound -Name "WINRM*"` 会报
   "无法解析参数集"直接崩在第 2 步 —— 必须拆成 `-Name` 取值 + `Where-Object` 过滤方向。
2. **UAC 令牌降权**：非域机器上本地管理员远程登录会被降权，账号密码正确也连不上。
   设 `HKLM\...\Policies\System\LocalAccountTokenFilterPolicy = 1` 解决。
