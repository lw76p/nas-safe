# TS Safe（NAS Safe）— 介绍与 NAS 安装指南

> 全功能开源免费版 · v20261014 · 镜像已推阿里云容器镜像仓库

---

## 一、TS Safe 是什么

TS Safe 是装在 NAS 上的**数据安全总控台**，一个界面管完家里所有设备：

| 能力 | 说明 |
|---|---|
| 🛡 防勒索 | 异常加密行为检测 + 告警，篡改防护 |
| 💾 快照与取回 | 基于 btrfs / ZFS / VSS / APFS 的受保护快照，误改误删一键取回，不覆盖原文件 |
| 🖥 跨设备管控 | Windows 电脑零安装接入（远程执行 / 重启 / 关机）；Mac / Linux 装完整版主动报到；摄像头在线检测；路由器仅监控 |
| ⧉ 重复文件清理 | 跨设备按内容哈希找重复，安全清理 |
| 🧹 磁盘清理 | 垃圾文件扫描清理 |
| ⇄ 换机迁移 | 一键打包备份、换机恢复 |
| 🤖 AI 管家 / 问 AI | 接云端 AI（DeepSeek / 通义 / 智谱 / 七牛 / OpenAI）或本地 Ollama，可建知识库让它照文档回答 |
| 📚 知识库 | 上传说明书 / 笔记，RAG 问答 |
| 🔍 照片语义搜索 | 自然语言找图 |
| 📊 日报 / 告警 | 邮件 + 微信服务号推送，支持群机器人 / 飞书 / Bark / Telegram 等 9 种通道 |

**全部功能免费开放**：设备数不限、AI 调用不限量、知识库额度不限、迁移全开，没有任何升级门槛。

---

## 二、镜像地址

```text
crpi-aoopmmoq9zfqyus5.cn-hangzhou.personal.cr.aliyuncs.com/lw76p/nas-safe
```

| 标签 | 说明 |
|---|---|
| `v20261014` | 本次发布版本（电脑转控真机打通 + 全免费化 + 探活修复） |
| `latest` | 始终指向最新版 |

- 架构：linux/amd64（威联通 x86_64 实测通过）
- 大小：约 194 MB
- 内置：程序 + 前端 + 使用指南（`http://<NAS>:8848/guide.html` 可直接打开）
- 腾讯镜像仓库（ccr.ccs.tencentyun.com/lw76p/nas-safe）待推，命令相同

---

## 三、NAS 安装命令（威联通 SSH 实测）

### 1. 一键安装（推荐：拉镜像 + 自动选最大存储卷 + 启动，一条命令装完）

```bash
sudo bash install_nas_safe.sh                      # 自动选剩余空间最大的卷
sudo bash install_nas_safe.sh /share/我的目录       # 或自己指定数据目录
```

- 脚本随安装包提供（见随附「程序」文件夹里的 `install_nas_safe.sh`）；
- 控制台已装好的机器也可在线获取：
  `curl -fsSL http://<NAS的IP>:8848/api/downloads/install_nas_safe.sh -o install.sh`
- 重跑同一脚本即可升级（自动检测并替换旧容器，数据目录保留）。

### 2. 手动安装（想一步步来时用）

```bash
docker pull crpi-aoopmmoq9zfqyus5.cn-hangzhou.personal.cr.aliyuncs.com/lw76p/nas-safe:latest

docker run -d --name nassafe \
  --network host \
  -v /share/CE_CACHEDEV1_DATA/nassafe_state:/app/state \
  --restart unless-stopped \
  crpi-aoopmmoq9zfqyus5.cn-hangzhou.personal.cr.aliyuncs.com/lw76p/nas-safe:latest
```

- `--network host`：直用宿主网络，扫描 / 探活 / 被纳管都最顺（也可改 `-p 8848:8848`）
- `-v ...:/app/state`：**唯一要挂出来的目录**。账号、设备、配置、快照记录全在里面，升级换容器不丢
- QNAP 的 docker 若不在 PATH，用全路径：
  `/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker`

### 3. 初始化

浏览器打开 `http://<NAS的IP>:8848` → 首次进入注册管理员账号 → 完成。

### 4. 验证

```bash
curl http://<NAS的IP>:8848/api/health      # 200 即正常
```

### 5. 升级 / 回滚

```bash
# 升级：重跑一键脚本即可（自动检测并替换旧容器，数据目录保留）
sudo bash install_nas_safe.sh

# 或手动升级：
# docker pull crpi-aoopmmoq9zfqyus5.cn-hangzhou.personal.cr.aliyuncs.com/lw76p/nas-safe:latest
# docker rm -f nassafe
# docker run -d --name nassafe --network host \
#   -v /share/CE_CACHEDEV1_DATA/nassafe_state:/app/state \
#   --restart unless-stopped \
#   crpi-aoopmmoq9zfqyus5.cn-hangzhou.personal.cr.aliyuncs.com/lw76p/nas-safe:latest

# 回滚（指定旧版本 tag 即可）
docker run -d --name nassafe ... crpi-...aliyuncs.com/lw76p/nas-safe:v20261014
```

### 6. 忘记管理员密码（逃生门）

```bash
docker exec -it nassafe python3 server/reset_admin.py 用户名 新密码
```

---

## 四、装完第一步：接入你的设备

| 设备 | 怎么接 | 在哪操作 |
|---|---|---|
| Windows 电脑 | 电脑上跑一次「一键开启」脚本 → 添加电脑填 IP + 账号密码 | `http://<NAS>:8848/api/downloads/enable-winrm.ps1` |
| Mac / Linux | 下载完整版安装包，目标机 `bash install_service.sh` | 控制台 → 设备卡片 → ⬇ 下载完整版安装包 |
| 摄像头 / 路由器 | 仅在线监控，填 IP 即可 | 设置 → ＋添加设备 |
| 其他 TS Safe 的 NAS | 扫描自动发现，填地址 + 令牌纳管 | 设置 → ＋添加设备 → 自动扫描 |

详细图文（含常见问题）：**`http://<NAS的IP>:8848/guide.html`**（镜像内置，离线可看）。

---

## 五、本版（v20261014）主要更新

1. **电脑转控真机打通**：重写 WinRM 客户端协议层（10 处修复），NAS ↔ Windows 电脑远程执行 / 状态采集 / 文件扫描实测通过
2. **一键开启脚本修复**：防火墙参数崩溃、UAC 远程令牌降权、远程管理组，三处全修
3. **路由器 / 仅监控设备不再"永远离线"**：容器里没有 ping 命令导致探活必挂，已加 TCP 兜底
4. **收费门槛全部拆除**：设置页「版本与升级」菜单下线；设备数 / AI 额度 / 知识库 / 照片搜索 / 迁移 / 全部通知通道实测放行（含修复免费档合并算式把 AI 供应商裁成 3 个的隐藏 bug）
5. **测试**：7 套测试全绿（E2E 42 项等），镜像冒烟测试通过（健康检查 / 指南页 / 首页均 200）
