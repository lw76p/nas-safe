# QNAP 威联通真机实测记录（192.168.8.62 / NAS873A）

> 记录时间：2026-09-29
> 数据来源：NAS MCP 官方 API 探测（非 SSH）
> 意义：修正 PRODUCT.md 中"威联通 QTS 走 btrfs"的判断，明确 B 类适配档位的真实路径

## 一、真机规格

| 项 | 数据 |
|---|---|
| 型号 | QNAP TS-873A（内部型号 TS-X73A，平台 TS-NASX86） |
| 系统 | QTS 5.2.9（build 20260514，patch 0）—— **非 QuTS Hero** |
| CPU | AMD Ryzen Embedded V1500B 四核 / 8 线程 |
| 内存 | 32GB（2×16GB Juhor） |
| 主机名 | NAS873A |
| 网络 | 三网口：eth1=192.168.8.62 / eth2=192.168.8.90 / eth0=192.168.8.91 |
| 磁盘 | 8× WD 4TB HDD（RAID5）+ 2× WD SN750 SE 250GB NVMe（RAID1） |
| 温度/健康 | CPU 58℃ / 系统 38℃；全部磁盘 health=OK |

## 二、存储结构（来自 list_storages）

- **1 个存储池**（pool_id=1），总容量 ≈25.6TB，已分配 12.74TB，空闲 12.87TB
- 池内含两个 RAID 组：
  - raid_id=1：**RAID1**，2× M.2 NVMe SSD（系统/缓存用）
  - raid_id=3：**RAID5**，8× 3.5" SATA HDD
- 卷（Volume）：
  - vol_no=1「系统盘」≈1.09TB，已用 35%
  - vol_no=2「我的文件」≈22.9TB，已用 48%（thin volume）

## 三、关键结论：文件系统是 ext4，不是 btrfs

### 为什么重要

PRODUCT.md 原写"威联通 QTS / QuTS Hero：✅（btrfs 型号 / ZFS）"，表述不够准确。实测澄清：

- **QTS（标准版，本机）**：底层是 **ext4** + 威联通自己的**块级存储池快照**（Storage Pool Snapshot），**不是 btrfs subvolume 快照**
- **QuTS Hero（仅部分型号可切换）**：才是 ZFS
- 本机 TS-873A 出厂即 QTS 5.2.9，跑的是 ext4

### 对产品适配的影响

| 原假设 | 修正后 |
|---|---|
| 威联通走 btrfs 命令 | ❌ 不成立，本机是 ext4 |
| 适配方式 | ✅ 必须走 **QNAP 官方 Snapshot API**（SYNO 类不存在，QNAP 有 `Storage/PoolSnapshot` 相关 API + Snapshot Replica 套件） |
| 适配档位 | 归入 PRODUCT.md 的 **B 类·Web API** 档（和群晖 DSM 同档） |
| btrfs 适配层 | 在本机**不适用**，只在飞牛/裸 Linux/TrueNAS(btrfs)/OMV/Unraid 生效 |

## 四、适配方案（B 类·QNAP）

本机是验证 B 类适配的**完美真机**——快照管理、路径、API 全现成。需要做：

1. **探测 QNAP 快照能力**：调用 QNAP API 列出存储池快照、确认快照挂载路径（QNAP 快照挂载在 `/.share/snapshot/...` 或类似隐藏路径，需实测确认）
2. **适配存储池快照的"取回单文件"**：块级快照挂载后同样是目录，可只读浏览 + 复制单文件
3. **适配"锁快照 + 篡改检测"**：QNAP 快照本身可被管理员删除，需要在产品层做独立权限 + 完整性校验告警（补绿联/威联通都承认的洞）

### 待实测确认（需要 QNAP 官方 API 权限 / 或 SSH）

- 存储池快照的**挂载路径**（决定 build_browse 的白名单规则）
- QNAP Snapshot API 的具体端点与鉴权（需 Container Station 或 qcli 调用）
- 容器在 QTS 上以 privileged 跑时，能否访问到宿主机的 `/.share/...` 快照目录

## 五、同步给 GitHub 的提醒

本文件写好后，需要重新生成 GitHub token 并通过 `bash push.sh` 推送（旧 token 已删）。
