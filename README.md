# NAS Safe

**给你的 NAS 上一把锁和一个傻瓜时间轴。**

装完默认开启，出事一键回滚。

---

## 为什么需要它

你的 NAS 有快照，对吧？那你可能不知道这两件事：

### 一、你的快照，勒索软件能删

现代勒索攻击的标准流程是这样的：

```
入侵 → 横向移动 → 找到备份凭据 → 攻陷备份系统 → 删掉所有快照 → 最后才加密你的数据
```

从入侵到加密，往往间隔**几周**。攻击者有充足的时间清空所有能碰到的快照。

而且——**各家 NAS 的"锁定快照"，管理员照样能删。** 这不是猜测，是厂商文档里写着的：

> "Locked snapshots also remain manageable by an administrator,
> so they are not immutable recovery copies."
> —— 绿联官方文档

ZFS 的 `zfs hold` 也不是真保护：有 root 权限就能 `zfs release` 再 `zfs destroy`。

**你的快照，本质上是一把挂在门上的钥匙。**

### 二、快照回不去，因为你没法只拿一个文件

系统自带的快照，基本只能"整卷回滚"——把整个存储空间退回某个时间点。

但真实场景往往是：**你只想要回一个文件。**

- 昨天误改了一份合同
- 上周删错了一个文件夹
- 某个文档被覆盖了

为了这一个文件，你要把整个卷回滚？**那这期间的所有新数据怎么办？**

大多数人的选择是：算了，手动重做吧。**快照白拍了。**

---

## NAS Safe 做什么

**它的定位不是"快照工具"，而是"快照之上的防勒索层"。**

| 功能 | 说明 |
|---|---|
| **时间轴界面** | 横向时间轴，每个点是一次快照，点击查看详情 |
| **单文件取回** | 浏览快照目录，勾选文件取回，**不覆盖你当前的数据** |
| **一键拍快照** | 不用进系统面板翻菜单 |
| **风险提示** | 自动检测哪些存储单元没有保护，红黄绿一眼看懂 |
| **篡改告警** | 你锁定的快照一旦被删/被解锁，顶栏立刻红/黄告警（每 30s 巡检） |
| **跨品牌统一** | 飞牛、绿联、TrueNAS、Unraid、OMV、群晖/威联通（开 SSH）—— 同一个界面 |

---

## 三条设计原则

### 1. 核心功能完全离线

**拔网线也能救数据。** 没有激活服务器、没有联网校验、没有云端依赖。

因为如果一个"防勒索工具"本身依赖某个服务器活着——那它保护了什么？

### 2. 绝不动你的生产数据

- 只读浏览快照
- 取回文件时**绝不覆盖**同名文件（自动加 `.restored-时间戳` 后缀）
- 所有写操作需要显式确认

### 3. 开源

**因为你要把系统最高权限交给它。**

不开源，你没理由信它。源码摆在这里，任何人都可以审计：

- 所有外部命令通过列表参数调用，`shell=False`，杜绝命令注入
- 路径参数统一白名单校验
- 浏览接口强制限定在快照目录内，防止越权读取生产数据

---

## 快速开始

### 方式一：Docker（推荐）

```bash
git clone <仓库地址> nassafe
cd nassafe
docker compose up -d
```

然后浏览器打开：`http://你的NAS地址:8848`

### 方式二：直接跑（需要系统 Python 3.9+）

```bash
cd nassafe
NASSAFE_WEB_DIR=./web python3 server/app.py
```

### 先检测你的 NAS 支不支持

跑一下探测脚本（**纯只读，不修改任何东西**）：

```bash
sh scripts/probe.sh
```

它会告诉你：

- 系统是什么（飞牛/绿联/TrueNAS/…）
- 有没有 btrfs 或 ZFS（**这是前提**）
- 快照命令能不能用
- 权限够不够

**如果只有 ext4 —— 快照功能用不了**，需要重建存储池为 btrfs。脚本会告诉你。

---

## 支持的系统

| 系统 | 状态 | 说明 |
|---|---|---|
| 飞牛 fnOS | ✅ 首发支持 | Debian + btrfs，SSH 开放 |
| 裸 Linux | ✅ 支持 | 直接调 btrfs/zfs |
| TrueNAS | ✅ 支持 | ZFS 原生 |
| Unraid | ✅ 支持 | btrfs / ZFS |
| OMV | ✅ 支持 | Debian 底子 |
| **绿联 UGOS Pro** | ⚠️ 实测中 | btrfs 池可用；ext4 池无法快照 |
| 群晖 DSM（开 SSH） | ⚠️ 实测中 | 需 SSH + btrfs 存储池 |
| **威联通 QTS（开 SSH）** | ✅ **已适配** | ext4 + 块级快照，走官方 `qcli_volumesnapshot` CLI；创建/锁定/删除 + 浏览/取回全闭环真机验证（含 Web UI）。**Docker 远程模式已真机验证**（见下） |
| 极空间等封闭系统 | ❌ 不支持 | 无 SSH、无 btrfs、无开放接口 |

### 关于绿联

绿联建存储池时可以选 **ext4 或 Btrfs**。

- 选 **Btrfs** → 你的快照功能可用（但注意：绿联的快照是**文件夹级**，不是整卷级）
- 选 **ext4** → **无法使用快照**，需要重建存储池

**NAS Safe 会主动检测并提示这一点** —— 绿联自己的界面不会告诉你"你选错了文件系统"。

---

### 远程管理 NAS（高级）

默认情况下 NAS Safe 装在 NAS 本机运行，直接读取宿主机的快照。
如果你的 NAS 不方便装服务（比如威联通 QTS 只想用 SSH 管理），也可以把服务装在**另一台电脑/服务器**上，远程管理 NAS：

```bash
# 监听地址（默认 0.0.0.0；只想本机访问可设 127.0.0.1）
export NASSAFE_BIND_HOST=0.0.0.0
# 指向你的 NAS（SSH 凭据）
export NASSAFE_QNAP_HOST=192.168.8.62
export NASSAFE_QNAP_USER=admin
export NASSAFE_QNAP_PASS='你的密码'
python3 server/app.py
```

- 浏览器打开 `http://运行服务的那台机器:8848` 即可像在 NAS 本机一样浏览、取回文件
- 取回的文件会保存到**运行服务的这台机器**上（不是 NAS 上），路径在取回时弹窗确认
- Windows 开发机照样能跑：远程路径一律按 Linux 处理，不受本机系统影响

#### 用 Docker 部署（威联通 QTS 推荐）

如果你不想在 NAS 上裸装 Python，可以用官方提供的 `docker-compose.qnap.yml`：
容器**不设 privileged、不挂宿主根目录**，只经 SSH 回连 NAS 调 `qcli`，安全面最小。

```bash
# 在你运行 Docker 的机器上（或 NAS 的 Container Station 终端）
cd nassafe
export NASSAFE_QNAP_PASS='你的威联通SSH密码'   # 密码走环境变量，不写进文件
docker compose -f docker-compose.qnap.yml up -d
# 浏览器打开：http://运行Docker的机器:8848
```

注意：容器内 `NASSAFE_QNAP_HOST` 必须填**真实 NAS IP**（如 `192.168.8.62`），
不能填 `127.0.0.1/localhost`，否则会误判成"本地模式"去容器里找 `qcli` 而失败。
取回的文件默认落到容器内 `/app/_restored`；想落到宿主机就给 compose 加
`-v /本地路径:/app/_restored` 挂载。

> 该 `docker-compose.qnap.yml` 已在真实 QNAP TS-873A（QTS 5.2.9）上构建镜像并端到端验证：
> 创建锁定快照 → 列快照确认锁定 → 浏览到真实文件 → SSH 删除轮询消失，全闭环 PASS。
> 详见 [docs/QNAP-REALITY.md](docs/QNAP-REALITY.md) 第七章。

---

## 使用流程

1. **打开界面** → 自动扫描所有存储单元
2. **看到红色横幅** → 说明有单元没保护，点进去
3. **点"立即拍一张快照"** → 创建第一张只读快照
4. **需要恢复时** → 点时间轴上的点 → "浏览并取回文件"
5. **选中要恢复的文件** → 取回到 `_restored` 目录，**原文件不动**

---

## 技术细节

### 快照是怎么创建的

**btrfs：**
```bash
btrfs subvolume snapshot -r <源> <快照目录>/snap-20260929-163000
#                      ↑ -r 创建只读快照，无法被意外修改
```

**ZFS：**
```bash
zfs snapshot pool/data@snap-20260929-163000
# ZFS 快照天然只读
```

快照存放位置：`<存储单元>/.nassafe/snapshots/`

> **威联通 QTS（ext4）特例**：不走 btrfs/zfs，而是官方 `qcli_volumesnapshot` CLI（底层 LVM 瘦快照）。
> 快照创建后系统会**自动只读挂载**在宿主机的 `/mnt/snapshot/<卷ID>/<快照ID>/`，nas-safe 直接遍历该挂载点
> 完成浏览/取回，与 btrfs/zfs 复用同一套逻辑。详见 [docs/QNAP-REALITY.md](docs/QNAP-REALITY.md)。

### 架构

```
浏览器 → Web UI（时间轴）
           ↓ HTTP
       后端服务（容器内，标准库，零依赖）
           ↓ subprocess（列表参数，shell=False）
       btrfs / zfs 命令
           ↓
       底层存储
```

### 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system` | 系统能力画像 |
| GET | `/api/volumes` | 存储单元列表（含 QNAP 远程卷） |
| GET | `/api/snapshots?volume=<路径>` | 快照列表 |
| GET | `/api/browse?path=<路径>` 或 `?snapshot_id=<ID>&volume_id=<ID>&subpath=<路径>` | 浏览快照内文件（btrfs/zfs 用 `path`；威联通 QNAP 用后者） |
| POST | `/api/snapshot/create` | 创建快照（自动登记为受保护） |
| GET | `/api/alerts` | 篡改告警列表（受保护快照消失/解锁即告警） |
| POST | `/api/snapshot/restore` | 取回文件（需 `confirm: true`；btrfs/zfs 用 `snapshot_path`，QNAP 用 `snapshot_id+volume_id+relative_file+destination`） |

### 运行测试

```bash
python3 server/test_e2e.py      # 通用：路径注入防护、越权防护、取回逻辑、格式化、品牌识别
python3 server/test_qnap.py     # 威联通：含真机集成测试（需设 NASSAFE_QNAP_HOST/NASSAFE_QNAP_USER/NASSAFE_QNAP_PASS 指向真机）
```

覆盖：路径注入防护、越权防护、取回逻辑、格式化、品牌识别，以及威联通 QTS 的
创建/锁定/删除 + 浏览/读取/取回全链路（真机集成测试会自我清理，创建后删除并轮询确认）。

---

## 路线图

**v1.0（当前）** —— 时间轴、单文件取回、跨系统适配、快照锁定 + 篡改告警（受保护快照消失/解锁即顶栏告警）

**v1.x** —— 自动快照策略、重复文件扫描

**v2.0** —— 异地不可变副本、多设备统一看板、AI 解读体检报告

---

## 授权

**核心功能永久免费，且完全开源。**

进阶功能（自动策略、告警、异地副本）采用买断制，License 为**离线签名的文件** —— 即使我们的服务器关闭，你已购买的功能依然可用。

---

## 一条不承诺的话

**这不是数据恢复服务。** 它是"防患于未然"，不是"事后抢救"。

如果你现在就已经丢数据了，找专业的数据恢复公司。这个工具的价值在于：**让你永远不需要去找他们。**

---

*Snapshots are your last line of defense. Make sure they can't be deleted.*

---

## 许可证

核心引擎与时间轴 UI 以 **MIT 协议**开源，详见 [LICENSE](LICENSE)。

进阶功能（自动策略、告警、异地不可变副本、AI 解读）将以闭源 License 形式提供，
不在本仓库内。开源范围与闭源范围以本仓库内容与后续发布说明为准。
