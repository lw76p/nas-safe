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

## 四、适配方案（B 类·QNAP）—— 已实证 ✅

> 2026-09-29 通过 **SSH（admin 账号）+ 官方 `qcli_volumesnapshot` CLI** 在真机完整验证，
> 适配层已落地为 `server/qnap.py`（43 项测试，含真机集成测试全绿）。

QNAP 没有走 btrfs，也不是 REST API，而是**官方 CLI `qcli_volumesnapshot`**（底层 LVM 瘦快照）。
nas-safe 直接调用该 CLI，无需 btrfs/zfs 命令。

### 已验证的官方快照 CLI（TS-873A / QTS 5.2.9 实测）

| 操作 | 命令 | 说明 |
|---|---|---|
| 登录（存档会话） | `qcli -l user=<u> pw='<p>' saveauthsid=yes` | 后续命令需先登录；sid 存档于 NAS |
| 卷列表 | `qcli_volume -l` | 返回 `volumeID / Alias`，如 `1=系统盘`、`2=我的文件` |
| 列快照 | `qcli_volumesnapshot -l volumeID=<id>` | 按卷 ID 列快照 |
| **创建并锁定** | `qcli_volumesnapshot -t volumeID=<id> snapshot_name=<名> vital=1` | **`vital=1` = 永久保留 = 锁快照（防勒索核心）** |
| 删除 | `qcli_volumesnapshot -d snapshotID=<id>` | ⚠️ **只需 snapshotID，不需要 volumeID** |
| 保留策略 | `qcli_volumesnapshot -T volumeID=<id> retention_type=...` | 设保留期/数量 |
| **回滚（禁用）** | `qcli_volumesnapshot -r snapshotID=<id>` | ❌ **数据破坏性操作，适配层永不调用** |

卷 ID 映射（本机）：`volumeID=1` = 系统盘（≈1TB）；`volumeID=2` = 我的文件（≈20.8TB）；`volumeID=288` = 另一存储池。

### 关键实测结论

1. **创建 + 锁定可用**：`create snapshot 10001 ok!` 后列表显示 `vital=1`、`status=Ready`。
2. **删除是异步后台回收**：执行 `-d` 返回 `delete snapshot 10001 ok!` 后，快照会短暂处于
   `Removing...` 状态，约 20~60 秒后才彻底消失 —— 调用方需轮询确认（适配层已在测试中处理）。
3. **锁快照（vital=1）可被显式删除**：`-d` 仍能移除 vital 快照，但**保留策略/勒索软件无法催删**，正是卖点。
4. **底层是 LVM 瘦卷**：瘦池 `tp1`（≈25.9TB），`lv1`=系统盘、`lv2`=数据盘，确认块级快照本质。

### 安全红线（写进代码）

- 适配层**绝不调用 `-r` 回滚**（把整卷回退到快照点的破坏性操作）。
- 创建快照默认 `vital=1`（永久锁定）。
- 命令以列表参数 + `shell=False` 调用（本地模式）；SSH 模式对参数做 `shlex` 转义，密码安全传递。
- paramiko 仅在 SSH 模式懒加载，保持核心零强制第三方依赖。

## 五、文件取回 / 浏览（已真机验证 ✅）

QNAP 块级快照创建后，**系统会自动以只读方式挂载在宿主机的 `/mnt/snapshot/<卷ID>/<快照ID>/`**，
无需任何 `-m` 挂载命令。nas-safe 直接遍历该挂载点即可浏览目录树、读取、取回单文件，
与 btrfs/zfs 完全复用同一套 `build_browse` / `do_restore_file` 路径白名单逻辑。

2026-09-29 真机验证通过（`server/qnap.py` 的 `list_dir` / `read_file` / `restore_file`，
B 段集成测试：创建→浏览→读取→取回→删除轮询，全绿）：

1. **只读挂载点自动就绪**：`/mnt/snapshot/2/<SID>/` 列出顶层 16 个真实目录（工作/影视/软件/…）。
2. **浏览路径构造必须用 `posixpath`，不能用 `os.path`**（关键坑）：
   开发机若在 Windows，`os.path.join/normpath` 会把 Linux 远程路径的 `/` 翻成 `\`
   （变成 `\mnt\snapshot\...`），导致 SSH 上的 `ls` 找不到目录、静默返回空、浏览全失败。
   **所有"远程挂载路径"一律用 `posixpath` 拼接**（跨平台恒为 `/`），本地 `dest` 仍用 `os.path`。
3. **过滤规则**：跳过 `.` 开头的系统/隐藏虚拟目录（如 `.@wfm`、`.@__lock__工作`、`.@__thumb`、
   `.streams`、`.DS_Store`）与符号链接（跟随会乱码报错，真实目标会单独列出）。
4. **取回绝不覆盖**：`restore_file` 若目标已存在，自动加 `.restored-<时间戳>` 后缀。
5. **删除是异步回收**：取回验证后删除快照，仍需轮询 ~20-60s 确认 `Removing...` 消失。

> 注：早期设想的 `qcli_volumesnapshot -m`（mountsnapshotfolder / SMB 共享）**不需要**——
> QTS 已自动只读挂载，直接遍历即可，少一条依赖、少一个权限面。

## 五之二、Web UI 接入与后端修复（2026-09-29 续）

完成第五段的 CLI 能力后，进一步把浏览/取回接到 Web UI（前端 `web/app.js`），并在真机
端到端验证中暴露并修复了若干后端缺陷：

### 前端（Web UI）接入
- `openSnapshotDetail` / `openBrowser` / `restoreFile` 增加 QNAP 分支判定（`snap.backend==="qnap"`）：
  - 浏览用 `?snapshot_id=&volume_id=&subpath=` 而非本地 `?path=`
  - 条目 `path` 为相对路径，面包屑与"返回上级"按相对路径累加
  - 取回用 `snapshot_id+volume_id+relative_file+destination`，destination 首次弹窗确认并记忆到 localStorage
- 修复"QNAP 快照 `path` 为空导致浏览按钮被 disabled"的判定（`canBrowse` 同时接受 `backend==="qnap"`）。

### 后端修复（均经真机 HTTP 端到端验证）
1. **`list_all_volumes` 漏列远程 QNAP 卷**：原逻辑只在本地有 `qcli` 命令时列 QNAP 卷，但
   "远程管理 NAS"模式下 server 不在 QTS 宿主、`qcli` 不存在 → 卷列表为空。改为：当
   `default_client().host` 非空（指向远程 QNAP）时也纳入 QNAP 卷。
2. **`Snapshot` 无 `volume_id` 属性导致 500**：`storage.browse_snapshot` /
   `restore_from_snapshot` 误用 `snapshot.volume_id`，而该字段只存在于 `Volume`。
   改为 `getattr(snapshot, "volume_id", None) or snapshot.volume`。
3. **`restore_file` 目的地处理三处错误（真·安全 bug）**：
   - 目录预建原本在 SSH 模式跑到**远程 NAS** 去 `mkdir -p`，但文件实际写到**本地管理机**
     → 本地父目录不存在而失败。修正：destination 始终是"运行 server 的这台机器"的本地路径，目录统一在本地预建。
   - 已存在判断原本在 SSH 模式到**远程** `test -e`，远程没有该路径恒判不存在 → **静默覆盖**同名文件，
     违背"绝不覆盖"承诺。修正：统一用本地 `os.path.exists` 判断。
   - `os.path.isdir(dest)` 在首次恢复（目录尚不存在）时把 destination 误当文件 → 文件写成无扩展名的
     `nassafe_restored`。修正：destination 一律视为"恢复目录"，文件落到 `dest/<原文件名>`。

### 部署注意：监听地址与 NAS 地址分离
`app.py` 监听地址曾复用 `NASSAFE_HOST`，设成 NAS IP 后 server 尝试绑定到不属于本机的地址而启动失败。
新增专用变量 `NASSAFE_BIND_HOST`（默认 `0.0.0.0`）控制监听；`NASSAFE_QNAP_HOST/USER/PASS`
（或兼容旧名 `NASSAFE_HOST/USER/PASS`）专供 `default_client` 指向 NAS。详见 README「远程管理 NAS」一节。

## 六、同步给 GitHub 的提醒

文档与代码改完后，在 E 盘仓库（`E:/我的AI软件/NAS快照AI工具`）执行
`git add -A && git commit -m "..." && bash push.sh` 推送即可（SSH 免 token，已配置）。

## 七、Docker 实机部署验证（2026-09-29 续三）

> 目标：在**真实 QNAP TS-873A（QTS 5.2.9）**上用 Container Station 的 Docker 跑起 NAS Safe，
> 走"远程管理模式"（server 在容器里，经 SSH 回连 NAS 调 `qcli`），端到端验证快照闭环。
> 结论：**PASS**。镜像已构建并保留，验证容器已清理。

### 为什么走 Docker 远程模式，而不是裸机装 Python

- QNAP 默认没给普通用户装好 Python3 运行环境，裸装依赖麻烦；而 Container Station 是官方套件。
- 远程模式**不设 privileged、不挂宿主根目录**——容器不碰宿主任何数据，只经 SSH 回连 NAS 调官方
  `qcli_volumesnapshot`，安全面最小，正好契合"防勒索工具自身不应有过大权限"的原则。
- 镜像同时兼容"btrfs 宿主本地模式"（带 `btrfs-progs`）与"QNAP 远程模式"（带 `paramiko`），一份镜像两用。

### 镜像构建：四个真机坑（已逐一解决）

QNAP 上的 Docker 二进制是 `/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker`
（**不在 PATH**），且是 QNAP 包装的 sh 脚本，会**强制把 `HOME` 设成 `$QPKG_DIR/homes/$(id -un)`**，
2376 端口还要证书——**只能走 SSH 命令行，不能用 docker context / TCP 直连**。构建过程踩了四个坑：

| # | 现象 | 根因 | 解决 |
|---|---|---|---|
| 1 | `curl` 拉 `github.com/.../archive/...tar.gz` 返回 `http=000` | GitHub 重定向到 `objects.githubusercontent.com`，QNAP 上该域名不通 | 改用 paramiko **SFTP 从本机直传**全部源码（36 文件）到 `/share/Container/nassafe/src`，再本地 build |
| 2 | 后台 `docker build` 静默失败 | QNAP 没有 `nohup` 可用，后台进程拿不到稳定 stdio | 改**前台长超时** build（500s 一镜到底） |
| 3 | `RUN mkdir .../homes/lw76p: Permission denied` | 包装脚本强设 `HOME=homes/lw76p`，但该目录属 `admin/750`，`lw76p` 建不了 | `sudo -S` 预建 `homes/lw76p` 并 `chown lw76p:everyone`（lw76p 可 sudo，密码同 SSH） |
| 4 | `E: Package 'zfsutils-linux' has no installation candidate` | Debian trixie 软件源无该包；且 ZFS 需宿主内核模块，容器内本就不可用 | 从 `Dockerfile` **移除 zfsutils-linux**，ZFS 用户改裸机部署（已在文件头注释标明） |

> 镜像源可用性实测：Debian trixie 默认源 `apt-get` 可达；PyPI 可达；daocloud 镜像可达（302）；
> **Docker Hub 直连不通**（`http=000`）——好在构建不依赖拉 base 以外的镜像；GitHub tarball 不通（见坑 1）。
> 最终镜像 `nassafe/nassafe:1.0.0` ≈ **159MB**（含 paramiko + btrfs-progs）。

### 起容器（远程管理模式）

`docker-compose.qnap.yml` 已写好：不设 privileged、不挂 `/host`，经 `NASSAFE_QNAP_HOST` SSH 回连。
核心环境变量：

```bash
export NASSAFE_QNAP_PASS='你的威联通SSH密码'   # 密码注入环境变量，不落盘
docker compose -f docker-compose.qnap.yml up -d
```

> 关键：容器内 `NASSAFE_QNAP_HOST` 必须写**真实 NAS IP**（如 `192.168.8.62`），
> 绝不能写 `127.0.0.1/localhost`——否则 `default_client()` 会误判为"本地模式"去容器里找 `qcli` 而失败。
> 监听地址由独立的 `NASSAFE_BIND_HOST`（默认 `0.0.0.0`）控制，与 NAS 地址分离。

### 端到端验证闭环：PASS

从 **Windows 电脑**直接访问 `http://192.168.8.62:8848`（QNAP 防火墙未挡 8848 端口），
经容器 API 走完完整流程：

1. **创建锁定快照**：`POST /api/snapshot/create` → 在卷 2「我的文件」上创建 `vital=true` 快照，**sid=20002**（永久锁，防勒索核心）。
2. **列快照确认锁定**：`GET /api/snapshots` 返回该快照 `vital=1`、`status=Ready`。
3. **浏览真实文件**：`GET /api/browse` 遍历自动只读挂载点 `/mnt/snapshot/2/20002/`，列出顶层 **16 个真实目录**（Mac mini下载文件夹 / Multimediea / Time Machine / hmp-tools / …），证明远程模式浏览链路通。
4. **清理**：HTTP API **无 delete 路由**（删除走 `qcli`），改用 SSH `qcli_volumesnapshot -d snapshotID=20002` 删除并轮询到 `Removing...` 彻底消失（约 20~60s 异步回收）。

✅ **结论**：威联通 QTS 的 Docker 远程管理模式快照闭环（创建锁定→列→浏览真实文件→删除轮询消失）
在真机完整跑通，Web UI 与 REST API 均可用。

### 收尾状态

- 验证容器已移除：`docker rm -f nassafe`（验证用，不留垃圾）。
- 镜像保留：`nassafe/nassafe:1.0.0` 仍在 NAS 上，随时 `docker compose -f docker-compose.qnap.yml up -d` 重起。
- 构建目录保留：`/share/Container/nassafe/src`（源码已同步到最新 commit `5e2bd3b`，含 paramiko + zfs 去除）。

### 给用户的"重起 / 部署"命令

在你运行 Docker 的这台机器（或 NAS 的 Container Station 终端）上：

```bash
cd <nassafe仓库>
export NASSAFE_QNAP_PASS='你的威联通SSH密码'
docker compose -f docker-compose.qnap.yml up -d
# 浏览器打开：http://运行Docker的机器:8848
```

取回的文件默认落到容器内 `/app/_restored`；如需落到宿主机，给 compose 加
`-v /你本地路径:/app/_restored` 挂载即可。

