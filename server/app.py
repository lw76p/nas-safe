"""
TS Safe — 后端 API 服务

依赖：仅标准库（http.server），零第三方依赖。
      这样保证在任何 NAS 上都能直接跑起来，不需要装 pip 包。

接口：
  GET  /api/system                系统能力画像
  GET  /api/volumes               存储单元列表（含快照统计）
  GET  /api/snapshots?volume=...  某单元的快照列表
  GET  /api/browse?path=...       浏览快照内的文件（只读）
  POST /api/snapshot/create       创建快照
  POST /api/snapshot/restore      从快照取回文件
  GET  /api/alerts                篡改告警列表（受保护快照消失/解锁即告警；?integrity=1 并入 v2 内容完整性校验）
  GET  /api/integrity             受保护快照内容完整性深度校验（v2）
  GET  /api/behavior?paths=...    勒索行为检测（v3）：扫描生产目录的扩展名突变/熵值骤升/批量改名
  GET  /api/notify/config         通知配置（脱敏）
  POST /api/notify/config         保存通知配置
  POST /api/notify/test           测试单个通道
  GET  /api/ai/config             AI 配置（含供应商列表与 ready 状态）
  POST /api/ai/config             保存 AI 配置
  POST /api/ai/interpret          把报告文本交给 AI 解读
  POST /api/ai/butler            AI 管家：对话式工具调用（建快照/列卷/查改动/搜文件/看健康）
  GET  /api/duplicates/status     重复文件扫描进度（v4）
  GET  /api/duplicates/report     重复文件报告（内容哈希判定，零误报）
  POST /api/duplicates/scan       启动重复文件扫描（后台线程，只读）
  POST /api/duplicates/quarantine 隔离勾选的重复文件（软删除，可恢复，每组至少留一份）
  GET  /api/duplicates/quarantine 隔离区清单
  POST /api/duplicates/restore    从隔离区恢复到原位置
  POST /api/duplicates/purge      彻底删除隔离区文件（不可恢复，仅限隔离目录内）
  POST /api/junk/scan             磁盘垃圾扫描（后台线程，只读）
  GET  /api/junk/status           垃圾扫描进度
  GET  /api/junk/report           垃圾报告（回收站/缩略图/Docker/旧日志）
  POST /api/junk/clean            按类别清理（confirm=true；逐项校验报告内路径）
  GET  /api/health                健康检查

安全约定：
  - 所有写操作需要 confirm=true 参数（服务端二次确认）
  - 路径参数统一经过 storage._validate_path 校验
  - 浏览接口强制限定在快照目录内，防止越权读取生产数据
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

# 让脚本可以独立运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage  # noqa: E402
from storage import (  # noqa: E402
    StorageError, CommandNotFound, Snapshot, Volume,
)

import integrity  # noqa: E402  v2 内容完整性校验
import behavior   # noqa: E402  v3 勒索行为检测
import notify     # noqa: E402  多渠道告警通知（微信服务号/群机器人/手机推送/邮件等）
import ai         # noqa: E402  AI 解读（多云供应商 + 本地 Ollama）
import ai_butler  # noqa: E402  AI 管家（对话式工具调用 + 免费版额度）
import autosnapshot  # noqa: E402  自动快照调度器（每小时 vital 锁快照 + 保留清理）
import metrics  # noqa: E402  系统指标采集（仪表盘：CPU/RAM/温度/网速/磁盘/卷容量）
import anomalies  # noqa: E402  异常判定 + 主动推送看门狗（小助手关闭时接管微信提醒）
import duplicates  # noqa: E402  重复文件清理（只读报告 + 隔离式软删除）
import junk  # noqa: E402  磁盘垃圾清理（回收站/缩略图/Docker缓存/旧日志，只读报告+按类清理）
import daily_report  # noqa: E402  每日健康日报（定时聚合快照/告警/空间/硬盘，复用通知链路推送）
import smartd  # noqa: E402  硬盘 SMART 健康采集（跨品牌，smartctl 多路径探测 + QTS 包兜底）
import devices  # noqa: E402
import netscan  # noqa: E402  联网设备自动扫描  跨品牌多设备总控制台（注册 + 分层聚合 + 健康汇总）
import migrate  # noqa: E402  换机迁移（配置包导出 / 导入 / 路径映射 / 能力降级）
import auth      # noqa: E402  账号/会话/权限分级
import editions  # noqa: E402  版本能力矩阵（免费版/家庭版/企业版）
import licensing  # noqa: E402  一机一码授权激活（与 editions 配套）
import knowledge  # noqa: E402  二期 RAG 知识库（家庭/专业版）
import response   # noqa: E402  三期 应急响应自动化（专业版）
import photo_search  # noqa: E402  三期 照片语义搜索（专业版，脚手架）

# 通知通道类型 <- editions.alerts 能力键的映射（alerts 键见 editions.EDITIONS）
_ALERT_CHANNEL_MAP = {
    "email": ("relay", "email"),
    "wechat": ("wechat_service_account",),
    "webhook": ("webhook",),
    "bark": ("bark",),
    "ntfy": ("ntfy",),
    "feishu": ("feishu",),
    "telegram": ("telegram",),
    "custom": ("custom",),
}


def _edition_alert_types() -> set:
    """当前版本允许的通知通道类型集合。"""
    out = set()
    for a in editions.limits().get("alerts", []):
        out.update(_ALERT_CHANNEL_MAP.get(a, (a,)))
    return out


def _edition_providers() -> list:
    """按版本裁剪 AI 供应商列表：免费版 3 种云端，家庭版/专业版全部 + 本地 Ollama。"""
    n = editions.limits().get("ai_cloud_sources", editions.UNLIMITED)
    cloud = [k for k in ai.PROVIDERS if k != "ollama"]
    if n != editions.UNLIMITED:
        cloud = cloud[:max(0, int(n))]
    if editions.limits().get("ai_local"):
        cloud.append("ollama")
    return cloud

HOST = os.environ.get("NASSAFE_BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("NASSAFE_PORT", "8848"))
WEB_DIR = os.environ.get("NASSAFE_WEB_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"
)
SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
# 打包好的 Windows 桌面小助手（PyInstaller 单文件 EXE，随镜像分发）
AGENT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"
)
AGENT_EXE = "桌面助手.exe"   # 主程序中文文件名（URL 路由仍用 ASCII）


_AGENT_README = (
    "TS Safe 桌面小助手 - 安装说明\r\n"
    "\r\n"
    "【安装只要两步】\r\n"
    "1. 解压本压缩包到任意文件夹\r\n"
    "2. 双击 桌面助手.exe：会自动弹出「安装 TS Safe 桌面助手」窗口并显示安装进度，\r\n"
    "   几秒后提示「安装完成」，不需要你选择或填写任何东西\r\n"
    "\r\n"
    "【装好后它长什么样】\r\n"
    "- 自动缩到电脑右下角的托盘图标里（蓝紫色盾牌），不占屏幕\r\n"
    "- 鼠标放上去显示：TS Safe · 快照保护中\r\n"
    "- 有异常时：图标中间亮起红色感叹号，并弹一次 Windows 通知（自动消失）\r\n"
    "- 左键点图标 = 查看未读提醒（点一条消一条）；右键 = 全部已读 / 退出\r\n"
    "- 看完的不再提醒，没看的不重复弹，只在图标上留红色标记\r\n"
    "\r\n"
    "【跟微信提醒的关系】\r\n"
    "- 小助手运行时：电脑弹窗和微信 / 邮件同时发，两边都不会漏\r\n"
    "- 主动退出小助手后：NAS 自动接管，异常继续通过微信服务号 / 邮件送达\r\n"
    "\r\n"
    "【常见问题】\r\n"
    "- 首次运行若 Windows 提示「已保护你的电脑」：点「更多信息」→「仍要运行」\r\n"
    "  （未签名软件的正常提示，代码签名证书正在办理）\r\n"
    "- 不需要安装 Python，不需要管理员权限，只写当前用户的开机自启\r\n"
    "- 卸载：右键托盘图标 → 退出\r\n"
)


def _gen_agent_zip(host: str) -> bytes:
    """动态生成小助手安装包：exe + 预置 NAS 地址（config.json）+ 说明。

    预置地址来自用户当前访问的 Host，所以从这个 NAS 页面下载的包，
    双击后无需用户再选择 NAS 地址，直接安装。
    """
    import io as _io
    import zipfile as _zip

    exe = os.path.join(AGENT_DIR, AGENT_EXE)
    if not os.path.isfile(exe):
        return b""
    base = f"http://{host}" if host else ""
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w", _zip.ZIP_STORED) as z:
        z.write(exe, AGENT_EXE)
        z.writestr("config.json", json.dumps(
            {"nas": base, "interval": 120}, ensure_ascii=False).encode("utf-8"))
        z.writestr("安装说明.txt", _AGENT_README.encode("utf-8-sig"))
    return buf.getvalue()


def _gen_setup_bat(base_url: str) -> str:
    """生成 Windows 一键安装脚本（纯 ASCII + CRLF，cmd 兼容）。

    用户双击后：下载 desktop_agent.py 到 %APPDATA%\\NASSafeAgent\\，
    寻找本机 pythonw 并以 --install 启动（探测/确认地址 + 开机自启 + 注册协议）。
    """
    bat = """@echo off
setlocal EnableExtensions
title TS Safe Agent Setup
set "DIR=%APPDATA%\\NASSafeAgent"
set "URL=__BASE__"
set "PYW="
echo [TS Safe] Downloading agent...
mkdir "%DIR%" 2>nul
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try{Invoke-WebRequest -UseBasicParsing -Uri '%URL%/agent/desktop_agent.py' -OutFile '%DIR%\\desktop_agent.py' -TimeoutSec 30}catch{exit 1}"
if errorlevel 1 (
  echo [TS Safe] Download failed. Please check the NAS address: %URL%
  pause
  exit /b 1
)
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%P"
for /f "delims=" %%P in ('where pyw.exe 2^>nul') do if not defined PYW set "PYW=%%P"
if not defined PYW if exist "%LOCALAPPDATA%\\Programs\\Python\\Python313\\pythonw.exe" set "PYW=%LOCALAPPDATA%\\Programs\\Python\\Python313\\pythonw.exe"
if not defined PYW if exist "%LOCALAPPDATA%\\Programs\\Python\\Python312\\pythonw.exe" set "PYW=%LOCALAPPDATA%\\Programs\\Python\\Python312\\pythonw.exe"
if not defined PYW (
  echo [TS Safe] Python was not found on this PC.
  echo Please install Python 3 from the page that is opening, then run this file again.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)
echo [TS Safe] Installing... A setup window will appear. Choose your NAS and click the button.
start "" "%PYW%" "%DIR%\\desktop_agent.py" --nas "%URL%" --install
exit /b 0
"""
    return bat.replace("__BASE__", base_url).replace("\n", "\r\n")

# 快照名允许的字符
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def human_size(num_bytes) -> str:
    if num_bytes is None:
        return "未知"
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "未知"


def dir_size(path: str, limit_seconds: float = 3.0) -> int:
    """估算目录占用。超时即返回已统计部分，避免大目录卡死接口。"""
    import time
    start = time.monotonic()
    total = 0
    for root, _dirnames, files in os.walk(path):
        if time.monotonic() - start > limit_seconds:
            break
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


# ---------------------------------------------------------------------------
# 配置脱敏（GET 接口返回配置时隐藏密钥）
# ---------------------------------------------------------------------------

_SECRET_FIELDS = {"appsecret", "secret", "pass", "api_key", "relay_token"}


def _mask_notify_cfg(cfg: dict) -> dict:
    out = dict(cfg)
    chs = []
    for ch in cfg.get("channels", []):
        c = dict(ch)
        for k in _SECRET_FIELDS:
            if c.get(k):
                c[k] = "***"
        chs.append(c)
    out["channels"] = chs
    return out


# ---------------------------------------------------------------------------
# 业务逻辑
# ---------------------------------------------------------------------------

def build_system_info() -> dict:
    profile = storage.probe_system()
    # 合并跨品牌能力画像（品牌标签 + 快照后端 + 白话说明），让前端统一展示
    try:
        from brands import detect_brand, detect_capabilities
        caps = detect_capabilities()
        profile.os_id = caps.get("brand") or profile.os_id
        profile.os_name = caps.get("brand_label") or profile.os_name
        for note in caps.get("notes", []):
            if note and note not in profile.warnings:
                profile.warnings.append(note)
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "system": profile.to_dict(),
        "server_time": iso_now(),
        "version": "1.0.0",
    }


def _volume_sort_key(vol: dict):
    """按 volume_id 数值升序排列，无 ID 的排最后。"""
    try:
        return (0, int(vol.get("volume_id") or 0))
    except (TypeError, ValueError):
        return (1, str(vol.get("volume_id") or ""))


def build_volume_list() -> dict:
    volumes = storage.list_all_volumes()
    result = []
    for vol in volumes:
        try:
            snaps = storage.list_all_snapshots(vol)
        except StorageError:
            snaps = []

        latest = None
        if snaps:
            dated = [s for s in snaps if s.created_at]
            if dated:
                latest = sorted(dated, key=lambda s: s.created_at)[-1].created_at
            else:
                latest = snaps[-1].name

        result.append({
            **vol.to_dict(),
            "snapshot_count": len(snaps),
            "latest_snapshot": latest,
            "protected": len(snaps) > 0,
        })

    result.sort(key=_volume_sort_key)
    return {"ok": True, "volumes": result, "count": len(result)}


def build_snapshot_list(volume_mountpoint: str) -> dict:
    volumes = storage.list_all_volumes()
    target = None
    for vol in volumes:
        if vol.mountpoint == volume_mountpoint or vol.name == volume_mountpoint:
            target = vol
            break

    if target is None:
        raise StorageError(f"未找到存储单元: {volume_mountpoint}")

    snaps = storage.list_all_snapshots(target)
    protected_keys = {
        e.get("key") for e in storage.load_protected().get("entries", [])
    }

    items = []
    for snap in snaps:
        size = None
        # 只对实体路径计算大小，避免 ZFS 快照名（dataset@snap）被当路径
        if snap.path and os.path.isabs(snap.path) and os.path.isdir(snap.path):
            size = dir_size(snap.path)
        items.append({
            **snap.to_dict(),
            "volume_id": target.volume_id,
            "protected": storage.snapshot_key(snap) in protected_keys,
            "size_bytes": size if size is not None else snap.size_bytes,
            "size_human": human_size(size if size is not None else snap.size_bytes),
        })

    return {
        "ok": True,
        "volume": target.to_dict(),
        "snapshots": items,
        "count": len(items),
    }


def _ensure_within(base: str, target: str) -> None:
    """确保 target 在 base 目录内，防止路径穿越读取生产数据。"""
    base_real = os.path.realpath(base)
    target_real = os.path.realpath(target)
    if not (target_real == base_real or target_real.startswith(base_real + os.sep)):
        raise StorageError("路径越权：只允许浏览快照目录内的内容")


def build_browse(path: str) -> dict:
    """浏览快照目录内的文件（本地文件系统路径模式）。

    安全白名单：仅允许浏览我们管理的快照目录（btrfs 的 .nassafe 目录，
    或 QNAP 的只读挂载点 /mnt/snapshot）。
    """
    if not isinstance(path, str) or not path:
        raise StorageError("缺少路径参数")

    if "\x00" in path or ".." in path.split("/") or ".." in path.split("\\"):
        raise StorageError(f"路径包含非法字符: {path}")

    # 安全白名单：仅允许浏览快照目录
    if ".nassafe" not in path and not path.startswith("/mnt/snapshot"):
        raise StorageError("出于安全考虑，仅允许浏览快照目录")

    if os.name == "posix":
        storage._validate_path(path)

    if not os.path.isdir(path):
        raise StorageError(f"目录不存在: {path}")

    parent = os.path.dirname(path.rstrip("/")) or None
    # 核心列举逻辑统一在 storage._browse_local_dir，便于审计
    return {"ok": True, "path": path, "parent": parent,
            "entries": storage._browse_local_dir(path)}


def _assert_root_in_mounts(path: str) -> None:
    """校验扫描根目录必须落在已知卷挂载点范围内（防越权扫系统目录）。

    与 /api/list_dir 同一套收集逻辑：storage 卷挂载点 + 指标采集的 df 路径。
    """
    if not path.startswith("/") or ".." in path.split("/"):
        raise StorageError("路径不合法")
    mounts = set()
    try:
        for v in storage.list_all_volumes():
            mp = str(getattr(v, "mountpoint", "") or "")
            if mp.startswith("/"):
                mounts.add(mp.rstrip("/"))
    except Exception:  # noqa: BLE001
        pass
    try:
        for v in metrics.collect().get("volumes", []):
            mounts.add(str(v.get("mount", "")).rstrip("/"))
    except Exception:  # noqa: BLE001
        pass
    mounts.discard("")
    if not mounts:
        return  # 卷信息不可得时不再拦截（SSH 模式下 metrics 一般可得）
    if not any(path == m or path.startswith(m + "/") for m in mounts):
        raise StorageError("路径必须在存储卷挂载点范围内")


def find_snapshot(volume_id: str, snapshot_id: str) -> "storage.Snapshot":
    """按 volume_id + snapshot_id 定位统一 Snapshot 对象。"""
    for vol in storage.list_all_volumes():
        if (vol.volume_id or vol.mountpoint) == volume_id:
            for snap in storage.list_all_snapshots(vol):
                if snapshot_id in (snap.snapshot_id, snap.name):
                    return snap
    raise StorageError(f"未找到快照: {volume_id}/{snapshot_id}")


def do_create_snapshot(volume_id: str, description: str = "") -> dict:
    volumes = storage.list_all_volumes()
    target = None
    for vol in volumes:
        if vol.mountpoint == volume_id or vol.name == volume_id:
            target = vol
            break

    if target is None:
        raise StorageError(f"未找到存储单元: {volume_id}")

    name = f"snap-{now_stamp()}"

    # 统一入口按 fs_type 分派（含 QNAP）。QNAP 默认 vital=1 永久锁定。
    snap = storage.create_snapshot(target, name, vital=True)

    # 后台推送"快照已创建"变动（不阻塞创建响应；未配置通道则空操作）
    try:
        import threading
        ev = [{"title": "已创建受保护快照", "detail": f"{name}（{target.name}）"}]
        t = threading.Thread(target=lambda: notify.dispatch([], ev), daemon=True)
        t.start()
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "snapshot": {**snap.to_dict(), "description": description},
        "message": f"快照创建成功：{name}",
    }


def do_restore_file(snapshot_path: str, relative_file: str, destination: str) -> dict:
    """从快照中取回单个文件到指定位置。"""
    storage._validate_path(snapshot_path)
    storage._validate_path(destination)

    if ".." in relative_file.split("/"):
        raise StorageError("相对路径非法")

    source = os.path.join(snapshot_path, relative_file.lstrip("/"))
    source_real = os.path.realpath(source)
    snap_real = os.path.realpath(snapshot_path)

    if not (source_real == snap_real or source_real.startswith(snap_real + os.sep)):
        raise StorageError("路径越权：源文件必须在快照目录内")

    if not os.path.exists(source_real):
        raise StorageError(f"快照中不存在该文件: {relative_file}")

    dest_path = os.path.join(destination, os.path.basename(source_real))

    # 绝不覆盖已存在的文件 —— 防止把用户当前数据覆盖掉
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(dest_path)
        dest_path = f"{base}.restored-{now_stamp()}{ext}"

    os.makedirs(destination, exist_ok=True)

    if os.path.isdir(source_real):
        shutil.copytree(source_real, dest_path)
    else:
        shutil.copy2(source_real, dest_path)

    return {
        "ok": True,
        "restored_to": dest_path,
        "message": f"已恢复到：{dest_path}",
    }


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TS Safe/1.0"
    # HTTP/1.1 + keep-alive：浏览器复用连接、且每条响应必须带 Content-Length，
    # 否则客户端无法界定响应体边界，表现为「200 但空体 / 页面转圈 / 接口偶发无响应」。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默，避免污染日志
        pass

    # -- Web 访问控制（登录会话 + Basic Auth 兜底）---------------------------
    WEB_USER = os.environ.get("NASSAFE_WEB_USER", "nassafe")
    WEB_PASS = os.environ.get("NASSAFE_WEB_PASS", "nassafe-dev-8848")

    def _get_cookie(self, name: str) -> str:
        for c in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = c.strip().partition("=")
            if k == name:
                return v
        return ""

    def _current_user(self) -> dict | None:
        """先读会话 cookie，失败再回退 Basic Auth（给脚本/扫描用）。"""
        sid = self._get_cookie(auth._COOKIE)
        user = auth.verify_session(sid) if sid else None
        if user:
            return user
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                decoded = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
                u, _, pw = decoded.partition(":")
                # 第一优先：登录用户表（与网页注册的账号同一套凭证）
                hit = auth.verify_credentials(u, pw)
                # 第二优先：环境变量兜底（旧部署兼容，未初始化用户表时可用）
                if not hit and u == self.WEB_USER and pw == self.WEB_PASS:
                    hit = {"username": u, "role": "admin"}
                if hit:
                    return hit
            except Exception:  # noqa: BLE001
                pass
        return None

    def _require_auth(self) -> bool:
        if self._current_user():
            return True
        self._send_401()
        return False

    def _require_admin(self) -> bool:
        user = self._current_user()
        if user and user.get("role") == "admin":
            return True
        self._send_json({"ok": False, "error": "需要管理员权限，请先登录"}, 403)
        return False

    def _set_session_cookie(self, sid: str, max_age: int) -> None:
        self.send_header("Set-Cookie", auth.cookie_header(sid, max_age))

    def _send_401(self) -> None:
        # 只有客户端主动带 Basic 凭证（脚本/代理）且失败时才 challenge；
        # 浏览器正常访问裸 401，不弹原生认证框（由前端登录弹窗接管）
        basic = (self.headers.get("Authorization") or "").startswith("Basic ")
        extra = [("WWW-Authenticate", 'Basic realm="TS Safe"')] if basic else None
        self._send_json({"ok": False, "error": "请先登录"}, 401, extra_headers=extra)

    # -- 响应helpers ------------------------------------------------------

    def _send_bytes(self, body: bytes, status: int = 200,
                   ctype: str = "application/json; charset=utf-8",
                   extra_headers=None) -> None:
        """统一发送响应：自动带 Content-Length，保证 HTTP/1.1 keep-alive 下体边界明确。"""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for (k, v) in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200, extra_headers=None) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send_bytes(body, status, extra_headers=extra_headers)

    def _send_file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        ctype = "text/html; charset=utf-8"
        if path.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif path.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif path.endswith(".svg"):
            ctype = "image/svg+xml"
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError(f"请求体不是合法 JSON: {exc}") from exc

    # -- 路由 -------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        # 鉴权模型（翻转版，参照 HMP 家庭影视库）：
        # 看数据免登录（公开）—— 所有 GET 数据接口默认公开，未登录也能看控制台/监控/快照等。
        # 改设置/动系统才要登录 —— 所有 POST 写操作由 do_POST 的 _require_admin 把关（绕开网页 curl 也改不动）。
        # 认证类接口（setup/check/logout/onelink/health）保持原生可用，供首次设账号与免输入登录。
        # /api/agent/install.sh|ps1 走一次性安装令牌，不依赖会话（在各自处理器内校验令牌）。
        # 少数会暴露文件系统浏览 / 配置导出的 GET 仍要求管理员，避免匿名遍历磁盘或导出配置。
        PROTECTED_GET = ("/api/list_dir", "/api/browse", "/api/migrate/export")

        try:
            if route == "/api/auth/setup":
                self._send_json({"ok": True, "needs_setup": auth.needs_setup()})
                return
            if route == "/api/auth/check":
                user = self._current_user()
                self._send_json({"ok": True,
                                 "needs_setup": auth.needs_setup(),
                                 "authenticated": bool(user),
                                 "user": user.get("username") if user else None,
                                 "role": user.get("role") if user else None})
                return
            if route == "/api/auth/onelink":
                # 免输入登录：一次性票据换 session（纯 GET，帮无法发出登录 POST 的环境）
                # 用后即焚 + 10 分钟有效；只种 cookie 后跳回首页
                token = (query.get("t") or [""])[0].strip()
                try:
                    sess = auth.consume_onelink_token(token)
                except ValueError:
                    self._send_bytes(b"", 302, extra_headers=[("Location", "/?onelink_err=1")])
                    return
                self._send_bytes(
                    b"", 302,
                    extra_headers=[
                        ("Location", "/"),
                        ("Set-Cookie", auth.cookie_header(sess["sid"], sess["max_age"])),
                    ])
                return
            if route == "/api/auth/logout":
                self._send_json(
                    {"ok": True}, 200,
                    extra_headers=[("Set-Cookie", auth.logout_cookie_header())])
                return

            # GET 数据接口默认公开；仅文件系统浏览与配置导出类 GET 仍需管理员
            if route in PROTECTED_GET and not self._require_auth():
                return
            if route == "/api/health":
                self._send_json({"ok": True, "time": iso_now()})
            elif route == "/api/alerts":
                include_integrity = (query.get("integrity") or ["0"])[0] == "1"
                self._send_json({
                    "ok": True,
                    "alerts": storage.scan_tamper(include_integrity=include_integrity),
                    "scanned_at": iso_now(),
                })
            elif route == "/api/integrity":
                self._send_json({
                    "ok": True,
                    "results": integrity.scan_integrity(),
                    "scanned_at": iso_now(),
                })
            elif route == "/api/behavior":
                paths = [unquote(p) for p in (query.get("paths") or []) if p]
                if not paths:
                    raise StorageError("缺少 paths 参数（可传多个 paths=...）")
                self._send_json(behavior.detect_behavior(paths))
            elif route == "/api/system/metrics":
                try:
                    force = bool(query.get("force"))
                    data = metrics.collect(force=force)
                    # 叠加 SMART 健康摘要（失败不影响指标主数据）
                    try:
                        data["smart"] = smartd.collect(force=force)
                    except Exception:  # noqa: BLE001
                        data["smart"] = {"available": False, "disks": []}
                    self._send_json({"ok": True, "metrics": data})
                except Exception as exc:  # noqa: BLE001 指标采集失败不拖垮页面
                    self._send_json({"ok": False, "error": str(exc)})

            elif route == "/api/smart":
                try:
                    force = bool(query.get("force"))
                    self._send_json({"ok": True, "smart": smartd.collect(force=force)})
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"ok": False, "error": str(exc)})

            elif route == "/api/ai/kb/list":
                # 知识库文档列表（家庭/专业版可见，免费版返回空 + 升级提示）
                if not editions.limits().get("knowledge"):
                    self._send_json({"ok": True, "docs": [], "upgrade": True,
                                     "message": "知识库为家庭版专属功能"})
                    return
                self._send_json({"ok": True, "docs": knowledge.list_docs(),
                                 "remaining": knowledge.quota_remaining()})

            elif route == "/api/ai/emergency/report":
                # 应急处置报告（专业版专属）
                if not editions.limits().get("emergency"):
                    self._send_json({"ok": False, "upgrade": True,
                                     "error": "应急响应自动化为专业版专属功能"})
                    return
                self._send_json({"ok": True, **response.report()})

            elif route == "/api/ai/photo/status":
                # 照片语义搜索状态（专业版）
                if not editions.limits().get("photo_search"):
                    self._send_json({"ok": False, "upgrade": True,
                                     "error": "照片语义搜索为专业版专属功能"})
                    return
                self._send_json(photo_search.status())

            elif route == "/api/ai/photo/thumb":
                # 照片缩略图（原图按浏览器缩放显示）：只放行索引里登记过的文件
                if not editions.limits().get("photo_search"):
                    self._send_json({"ok": False, "upgrade": True,
                                     "error": "照片语义搜索为专业版专属功能"})
                    return
                path = unquote((query.get("path") or [""])[0])
                ext = os.path.splitext(path)[1].lower()
                if ext not in photo_search._MIME:
                    self._send_json({"ok": False, "error": "不支持的图片类型"}, 400)
                    return
                if not photo_search.thumb_ok(path) or not os.path.isfile(path):
                    self._send_json({"ok": False, "error": "图片不在索引中"}, 404)
                    return
                with open(path, "rb") as f:
                    self._send_bytes(f.read(), 200, ctype=photo_search._MIME[ext])

            elif route == "/api/anomalies":
                # 统一异常列表（硬件/容量/趋势/防勒索告警），供网页端与桌面小助手共用同一套规则
                self._send_json({"ok": True, "anomalies": anomalies.collect_anomalies(),
                                 "agent": anomalies.agent_status()})
            elif route == "/api/agent/status":
                # 桌面小助手在线状态（离线时由服务端看门狗接管微信/邮件提醒）
                self._send_json({"ok": True, "status": anomalies.agent_status()})
            elif route == "/api/list_dir":
                # 只读：列出生产目录一级子目录（监控路径选择器用）。
                # 安全校验：绝对路径、禁止 ..、必须落在已知卷挂载点范围内。
                # 注意 QNAP 后端 volume.mountpoint 是卷ID，真实路径从指标采集的 df 里取。
                path = unquote((query.get("path") or [""])[0])
                if not path.startswith("/") or ".." in path.split("/"):
                    raise StorageError("路径不合法")
                mounts = set()
                try:
                    for v in storage.list_all_volumes():
                        mp = str(getattr(v, "mountpoint", "") or "")
                        if mp.startswith("/"):
                            mounts.add(mp.rstrip("/"))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    for v in metrics.collect().get("volumes", []):
                        mounts.add(str(v.get("mount", "")).rstrip("/"))
                except Exception:  # noqa: BLE001
                    pass
                mounts.discard("")
                if mounts and not any(path == m or path.startswith(m + "/") for m in mounts):
                    raise StorageError("路径必须在存储卷挂载点范围内")
                self._send_json({"ok": True, **metrics.list_dirs(path)})
            elif route == "/api/notify/config":
                self._send_json({
                    "ok": True,
                    "config": _mask_notify_cfg(notify.load_config()),
                    "relay_available": notify.relay_configured(),
                    # 当前版本允许的通知通道；前端据此把用不了的通道置灰并标注「升级后可用」
                    "allowed_types": sorted(_edition_alert_types()),
                })
            elif route == "/api/ai/config":
                cfg = ai.load_config()
                if cfg.get("api_key"):
                    cfg = dict(cfg)
                    cfg["api_key"] = "***"
                self._send_json({
                    "ok": True,
                    "config": cfg,
                    "ready": ai.is_ready(),
                    "providers": _edition_providers(),
                })
            elif route == "/api/autosnapshot":
                cfg = autosnapshot.load_config()
                self._send_json({
                    "ok": True,
                    "config": cfg,
                    "interval_seconds": max(1, int(cfg.get("interval_hours", 1))) * 3600,
                })
            elif route == "/api/duplicates/status":
                self._send_json({"ok": True, **duplicates.get_status()})
            elif route == "/api/duplicates/report":
                rpt = duplicates.load_report()
                if not rpt:
                    self._send_json({"ok": True, "empty": True, "groups": []})
                else:
                    self._send_json({"ok": True, **rpt})
            elif route == "/api/duplicates/quarantine":
                self._send_json(duplicates.quarantine_list())
            elif route == "/api/junk/status":
                self._send_json(junk.get_status())
            elif route == "/api/junk/report":
                rpt = junk.load_report()
                if not rpt:
                    self._send_json({"ok": True, "empty": True})
                else:
                    self._send_json({"ok": True, **rpt})
            elif route == "/api/daily-report":
                # 返回日报配置 + 上次报告（前端设置页用）
                self._send_json({
                    "ok": True,
                    "config": daily_report.load_config(),
                    "last": daily_report.load_last(),
                })
            elif route == "/api/license":
                # 授权信息：机器码 / 当前版本 / 是否已激活（设置页「版本与激活」用）
                # issue_demo：在线支付未接前默认 False；仅演示环境设 NASSAFE_ISSUE_DEMO=1 打开
                self._send_json({"ok": True, **licensing.info(),
                                 "issue_demo": os.environ.get("NASSAFE_ISSUE_DEMO") == "1",
                                 "pay_url": os.environ.get("NASSAFE_PAY_URL", "")})
            elif route == "/api/license/prices":
                # 升级价格：服务端实时读取（state/prices.json 可覆盖默认值，供发卡后台同步）
                self._send_json({"ok": True, "prices": licensing.get_prices()})
            elif route == "/api/system":
                _sys_info = build_system_info()
                _sys_info["edition"] = editions.summary()
                self._send_json(_sys_info)
            elif route == "/api/devices":
                # 跨品牌多设备总控制台：分层聚合所有设备健康快照
                force = bool(query.get("force"))
                self._send_json({"ok": True, **devices.collect_all(force=force)})
            elif route == "/api/devices/manage":
                # 联机设备管控（设置后台）：在线统计 + 每台设备的状态/代理/功能开关（只读查询）
                self._send_json({**devices.get_manage()})
            elif route == "/api/devices/snapshots":
                # 快照页的「联机设备快照预览」：本机走 /api/snapshots，远程由本控制台代理对端只读拉取
                dev_id = (query.get("device") or [""])[0]
                vol = (query.get("volume") or [""])[0]
                self._send_json(devices.remote_snapshots(dev_id, vol))
            elif route == "/api/agent/install.sh":
                # 轻量代理安装脚本（公开 + 一次性令牌鉴权，被控设备跑这条命令回连中控）
                token = (query.get("t") or [""])[0].strip()
                host = self.headers.get("Host") or ""
                scheme = (self.headers.get("X-Forwarded-Proto") or "http").strip()
                script = devices.agent_install_script(token, f"{scheme}://{host}")
                self._send_bytes(script.encode("utf-8"), 200, ctype="text/x-shellscript; charset=utf-8")
            elif route == "/api/agent/install.ps1":
                # Windows 版轻量代理（PowerShell 注册 + schtasks 每分钟心跳），令牌鉴权同 install.sh
                token = (query.get("t") or [""])[0].strip()
                host = self.headers.get("Host") or ""
                scheme = (self.headers.get("X-Forwarded-Proto") or "http").strip()
                script = devices.agent_install_script_ps(token, f"{scheme}://{host}")
                self._send_bytes(script.encode("utf-8"), 200, ctype="text/plain; charset=utf-8")
            elif route == "/api/migrate/export":
                # 换机迁移：导出本机可移植配置包（供前端下载）。免费版不含换机迁移
                if not editions.can("migrate"):
                    self._send_json({"ok": False, "error":
                        "换机迁移是家庭版/专业版功能，当前版本没有。升级后即可一键换机",
                        "upgrade": True})
                    return
                self._send_json({"ok": True, "bundle": migrate.build_bundle()})
            elif route == "/api/volumes":
                self._send_json(build_volume_list())
            elif route == "/api/snapshots":
                volume = (query.get("volume") or [""])[0]
                if not volume:
                    raise StorageError("缺少 volume 参数")
                self._send_json(build_snapshot_list(unquote(volume)))
            elif route == "/api/browse":
                sid = (query.get("snapshot_id") or [""])[0]
                vid = (query.get("volume_id") or [""])[0]
                subpath = (query.get("subpath") or [""])[0]
                if sid and vid:
                    # QNAP / 远程后端：按 snapshot_id 分派到统一浏览入口
                    snap = find_snapshot(vid, sid)
                    self._send_json(storage.browse_snapshot(snap, subpath))
                else:
                    path = (query.get("path") or [""])[0]
                    if not path:
                        raise StorageError("缺少 path 或 snapshot_id 参数")
                    self._send_json(build_browse(unquote(path)))
            elif route.startswith("/agent/"):
                # 桌面小助手分发：py 脚本静态下载；setup.bat 按当前 Host 动态生成
                name = route[len("/agent/"):]
                if name == "desktop_agent.py":
                    self._send_file(os.path.join(SCRIPTS_DIR, "desktop_agent.py"))
                elif name == "NASSafeAgent.zip":
                    # 首选分发方式：zip 包（exe + 预置地址 + 说明），浏览器不会拦截 zip
                    data = _gen_agent_zip(self.headers.get("Host") or "")
                    if not data:
                        self._send_json(
                            {"ok": False, "error": "安装包未随本版本分发，请改用 Python 脚本方式"}, 404)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header(
                            "Content-Disposition",
                            "attachment; filename=\"NASSafeAgent.zip\"; "
                            f"filename*=UTF-8''{quote('桌面助手.zip')}\"",
                        )
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(data)
                elif name == "NASSafeAgent.exe":
                    # 备选：直接下载 exe（无预置地址，首次运行会弹向导让选 NAS）
                    path = os.path.join(AGENT_DIR, AGENT_EXE)
                    if not os.path.isfile(path):
                        self._send_json(
                            {"ok": False, "error": "安装包未随本版本分发，请改用 Python 脚本方式"}, 404)
                    else:
                        with open(path, "rb") as fh:
                            data = fh.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header(
                            "Content-Disposition",
                            f'attachment; filename="{name}"; '
                            f"filename*=UTF-8''{quote(AGENT_EXE)}",
                        )
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(data)
                elif name == "setup.bat":
                    host = self.headers.get("Host") or ""
                    base = f"http://{host}" if host else ""
                    data = _gen_setup_bat(base).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header(
                        "Content-Disposition",
                        'attachment; filename="NAS-Safe-agent-setup.bat"',
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._send_json({"ok": False, "error": "未知文件"}, 404)
            elif route.startswith("/api/"):
                self._send_json({"ok": False, "error": f"未知接口: {route}"}, 404)
            else:
                # 静态文件
                rel = route.lstrip("/") or "index.html"
                if ".." in rel.split("/"):
                    self._send_json({"ok": False, "error": "非法路径"}, 400)
                    return
                self._send_file(os.path.join(WEB_DIR, rel))

        except (StorageError, CommandNotFound) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"ok": False, "error": f"服务内部错误: {exc}"}, 500)

    def do_POST(self):
        route = urlparse(self.path).path

        try:
            if route == "/api/auth/setup":
                payload = self._read_json()
                if not auth.needs_setup():
                    raise StorageError("已经初始化过，请直接登录")
                username = (payload.get("username") or "").strip()
                password = (payload.get("password") or "").strip()
                try:
                    auth.create_user(username, password, "admin", (payload.get("email") or "").strip())
                except ValueError as exc:
                    raise StorageError(str(exc))
                sess = auth.login(username, password)
                self._send_json(
                    {"ok": True, "user": sess["username"], "role": sess["role"]}, 200,
                    extra_headers=[("Set-Cookie", auth.cookie_header(sess["sid"], sess["max_age"]))])
                return
            if route == "/api/auth/login":
                payload = self._read_json()
                if auth.needs_setup():
                    raise StorageError("请先创建管理员账号")
                username = (payload.get("username") or "").strip()
                # 密码去首尾空白：粘贴密码时常带行尾换行/空格，直接原样比对必失败
                password = (payload.get("password") or "").strip()
                print("[login] attempt user=%r from=%s" % (username, self.client_address[0]), flush=True)
                try:
                    sess = auth.login(username, password)
                except ValueError as exc:
                    # 密码错误/限速是预期内的客户端错误，回 401 而不是 500
                    print("[login] DENIED user=%r reason=%s" % (username, exc), flush=True)
                    self._send_json({"ok": False, "error": str(exc)}, 401)
                    return
                self._send_json(
                    {"ok": True, "user": sess["username"], "role": sess["role"]}, 200,
                    extra_headers=[("Set-Cookie", auth.cookie_header(sess["sid"], sess["max_age"]))])
                return
            if route == "/api/auth/logout":
                self._send_json(
                    {"ok": True}, 200,
                    extra_headers=[("Set-Cookie", auth.logout_cookie_header())])
                return

            # 找回密码：登录页可用（未登录），靠「账号 + 注册邮箱」身份核验，验证码邮件送达后才允许改密
            if route == "/api/auth/forgot":
                payload = self._read_json()
                if auth.needs_setup():
                    raise StorageError("还没有注册账号，请先注册")
                username = (payload.get("username") or "").strip()
                email = (payload.get("email") or "").strip()
                try:
                    code = auth.make_reset_code(username, email)
                except ValueError as exc:
                    raise StorageError(str(exc))
                ok, msg = notify.send_system_mail(
                    email, "TS Safe 找回密码验证码",
                    "你的找回密码验证码是：%s\n\n10 分钟内有效，请勿透露给他人。\n如果这不是你本人的操作，请忽略这封邮件。" % code)
                if not ok:
                    raise StorageError("验证码已生成，但邮件发送失败：" + msg)
                self._send_json({"ok": True, "sent_to": email})
                return
            if route == "/api/auth/reset":
                payload = self._read_json()
                try:
                    auth.verify_reset_code(
                        (payload.get("username") or "").strip(),
                        (payload.get("code") or "").strip(),
                        payload.get("new_password") or "")
                except ValueError as exc:
                    raise StorageError(str(exc))
                self._send_json({"ok": True})
                return

            # 本人改密码（已登录，需原密码；用当前会话身份，不允许改他人）
            if route == "/api/auth/change_password":
                user = self._current_user()
                if not user:
                    self._send_json({"ok": False, "error": "请先登录"}, 401)
                    return
                payload = self._read_json()
                try:
                    auth.change_password(
                        user["username"],
                        (payload.get("old_password") or ""),
                        (payload.get("new_password") or ""),
                    )
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)})
                    return
                self._send_json({"ok": True})
                return

            # 本人改邮箱（已登录）
            if route == "/api/auth/set_email":
                user = self._current_user()
                if not user:
                    self._send_json({"ok": False, "error": "请先登录"}, 401)
                    return
                payload = self._read_json()
                try:
                    auth.set_email(user["username"], (payload.get("email") or "").strip())
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)})
                    return
                self._send_json({"ok": True})
                return

            # 轻量代理回连接口：被控设备无法登录中控，改用安装令牌鉴权（不走会话）
            if route in ("/api/agent/register", "/api/agent/heartbeat"):
                payload = self._read_json()
                if route == "/api/agent/register":
                    _aok, _amsg = editions.check_devices(len(devices.load_devices()) + 1)
                    if not _aok:
                        self._send_json({"ok": False, "error": _amsg})
                        return
                    self._send_json(devices.agent_register(payload.get("token"), {
                        "hostname": str(payload.get("hostname") or "")[:64],
                        "os": str(payload.get("os") or "")[:80],
                    }))
                else:
                    self._send_json(devices.agent_heartbeat(payload.get("token")))
                return

            # 官方验签端点（免登录）：用户机器激活时由其服务端调用，机器对机器无用户会话；
            # 仅配置了 master 密钥的官方机器开放，且只做验签不落库，无越权风险。
            if route == "/api/license/cloud-verify":
                if not os.environ.get("NASSAFE_MASTER_SECRET"):
                    self._send_json({"ok": False, "error": "验证服务未配置"}, 403)
                    return
                payload = self._read_json()
                key = str(payload.get("key") or "")
                fp_ask = str(payload.get("fp") or "").strip().upper()
                ok, msg, pl = licensing.verify_key(key)
                if not ok:
                    self._send_json({"ok": False, "error": msg}, 400)
                    return
                if fp_ask and str(pl.get("fp") or "").upper() != fp_ask:
                    self._send_json({"ok": False, "error": "机器码不符"}, 400)
                    return
                self._send_json({"ok": True, "payload": pl})
                return

            # 除认证接口外，所有 POST 都需要管理员权限（功能开关/写操作）
            if not self._require_admin():
                return
            payload = self._read_json()

            # 清除告警：把受保护基线对齐当前快照状态（确认消失/确认状态变化），横幅即消失
            if route == "/api/alerts/clear":
                self._send_json(storage.sync_protected_baseline())
                return

            if route == "/api/snapshots/vss_cleanup":
                # Windows VSS 孤儿影子副本清理：confirm=false 只预览，true 才删
                try:
                    from snapshot_vss import cleanup_orphans
                except Exception:  # noqa: BLE001
                    self._send_json({"ok": False,
                                     "error": "本机不是 Windows 或缺少 VSS 后端"}, 400)
                    return
                self._send_json(cleanup_orphans(dry_run=payload.get("confirm") is not True))

            if route == "/api/snapshot/create":
                volume = (payload.get("volume") or "").strip()
                if not volume:
                    raise StorageError("缺少 volume 参数")
                self._send_json(do_create_snapshot(volume, payload.get("description", "")))

            elif route == "/api/snapshot/restore":
                # 写操作必须显式确认
                if payload.get("confirm") is not True:
                    self._send_json({
                        "ok": False,
                        "error": "写操作需要 confirm=true 确认参数",
                    }, 400)
                    return
                sid = (payload.get("snapshot_id") or "").strip()
                vid = (payload.get("volume_id") or "").strip()
                relative_file = (payload.get("relative_file") or "").strip()
                destination = (payload.get("destination") or "").strip()
                if sid and vid:
                    # QNAP / 远程后端：按 snapshot_id 取回单文件
                    if not all([relative_file, destination]):
                        raise StorageError("缺少 relative_file / destination 参数")
                    snap = find_snapshot(vid, sid)
                    self._send_json(storage.restore_from_snapshot(snap, relative_file, destination))
                else:
                    # 本地 btrfs/zfs 模式：基于快照目录路径
                    snapshot_path = (payload.get("snapshot_path") or "").strip()
                    if not all([snapshot_path, relative_file, destination]):
                        raise StorageError(
                            "缺少参数：QNAP 模式需 snapshot_id+volume_id+relative_file+destination；"
                            "本地模式需 snapshot_path+relative_file+destination"
                        )
                    self._send_json(do_restore_file(snapshot_path, relative_file, destination))

            elif route == "/api/snapshot/revert":
                # 整卷回滚：破坏性操作。护栏 = confirm 严格 True + 仅 TS Safe 托管快照
                print(f"[revert] 收到回滚请求 confirm={payload.get('confirm')} vid={payload.get('volume_id')} sid={payload.get('snapshot_id')}", flush=True)
                if payload.get("confirm") is not True:
                    self._send_json({
                        "ok": False,
                        "error": "整卷回滚需要 confirm=true 确认参数",
                    }, 400)
                    return
                vid = (payload.get("volume_id") or "").strip()
                sid = (payload.get("snapshot_id") or "").strip()
                if not vid or not sid:
                    raise StorageError("缺少 volume_id / snapshot_id 参数")
                snap = find_snapshot(vid, sid)
                # 只允许回滚 TS Safe 自己创建的快照（与前端 canRevert 判定一致），
                # 防止误回滚 QTS 系统快照或用户手工建立的无关快照。
                if not re.match(r"^(auto-|nassafe_|snap-)", snap.name or ""):
                    print(f"[revert] 拒绝：非托管快照 name={snap.name}", flush=True)
                    raise StorageError(
                        "只允许回滚 TS Safe 创建的快照（auto-/nassafe_/snap- 前缀）"
                    )
                storage.revert_volume(snap)
                print(f"[revert] 回滚命令已提交 vid={vid} sid={sid} name={snap.name}", flush=True)
                self._send_json({
                    "ok": True, "reverted": True,
                    "volume_id": vid, "snapshot_id": sid,
                })

            elif route == "/api/agent/status":
                # 桌面小助手上报在线/离线：离线时提醒改由服务端看门狗走微信/邮件
                online = str(payload.get("online", "1")).lower() in ("1", "true", "yes", "on")
                self._send_json({"ok": True, "status": anomalies.set_agent_online(
                    online, str(payload.get("host") or ""))})
            elif route == "/api/notify/config":
                # 保存通知配置（channels 列表 + enabled）
                cfg = payload
                if not isinstance(cfg, dict):
                    raise StorageError("配置格式错误")
                cfg.setdefault("enabled", False)
                cfg.setdefault("channels", [])
                _allowed = _edition_alert_types()
                _bad = sorted({str(ch.get("type")) for ch in cfg.get("channels", [])
                               if isinstance(ch, dict) and ch.get("type")
                               and ch.get("type") not in _allowed})
                if _bad:
                    self._send_json({"ok": False, "upgrade": True,
                                     "error": "通知通道（" + "、".join(_bad) +
                                     "）是专业版功能，当前版本没有。升级后可用群机器人、飞书、手机推送、Telegram 等更多方式"})
                    return
                notify.save_config(cfg)
                self._send_json({"ok": True, "config": _mask_notify_cfg(notify.load_config())})

            elif route == "/api/notify/test":
                channel = payload.get("channel")
                if not isinstance(channel, dict) or not channel.get("type"):
                    raise StorageError("缺少 channel 配置")
                self._send_json({"ok": True, **notify.send_test(channel)})

            elif route == "/api/notify/alert":
                # 异常主动推送：按最快触达自动优选通道（微信服务号 > 手机推送 > 群机器人 > 邮件）
                title = str(payload.get("title") or "TS Safe 异常提醒")
                detail = str(payload.get("detail") or "")
                level = str(payload.get("level") or "warn")
                keys = payload.get("keys")
                if isinstance(keys, list):
                    anomalies.mark_sent([str(k) for k in keys])
                elif payload.get("key"):
                    anomalies.mark_sent([str(payload.get("key"))])
                self._send_json({"ok": True, **notify.push_alert(title, detail, level)})

            elif route == "/api/ai/config":
                cfg = payload
                if not isinstance(cfg, dict):
                    raise StorageError("配置格式错误")
                cfg.setdefault("enabled", False)
                # 前端对已存 Key 脱敏为 ***，二次保存时不传 api_key —— 此处保留旧值，避免被空值冲掉
                if not str(cfg.get("api_key") or "").strip() or cfg.get("api_key") == "***":
                    old = ai.load_config()
                    if old.get("api_key"):
                        cfg["api_key"] = old["api_key"]
                # 版本卡点：供应商必须在当前版本列表内；非专业版不能自定义接入点（地址/模型用内置默认）
                _prov = str(cfg.get("provider") or "")
                if _prov and _prov not in _edition_providers():
                    self._send_json({"ok": False, "upgrade": True,
                                     "error": "这个 AI 接口当前版本没有。升级后可用全部常见云端 AI 和本地模型"})
                    return
                if _prov and not editions.can("ai_custom_key") and _prov != "ollama":
                    _pinfo = ai.PROVIDERS.get(_prov)
                    if _pinfo:
                        cfg["base_url"] = _pinfo["base_url"]
                        cfg["model"] = _pinfo["model"]
                ai.save_config(cfg)
                self._send_json({"ok": True, "ready": ai.is_ready(), "config": ai.load_config()})

            elif route == "/api/ai/discover":
                # 自动搜索本地 AI（Ollama）：候选地址 + 局域网受限扫描 + NAS 本机通道
                self._send_json({"ok": True, **ai.discover_local()})

            elif route == "/api/ai/interpret":
                text = (payload.get("text") or "").strip()
                if not text:
                    raise StorageError("缺少 text 参数")
                result, err = ai.interpret(text)
                if err:
                    self._send_json({"ok": False, "error": err}, 400)
                elif result is None:
                    self._send_json({"ok": False, "error": "AI 未启用或未配置密钥", "ready": False}, 400)
                else:
                    self._send_json({"ok": True, "text": result})

            elif route == "/api/ai/diagnose":
                # AI 体检：聚合全机状态（硬件指标+趋势+快照保护+告警）交 AI 出报告
                if not ai.is_ready():
                    raise StorageError("AI 未配置，请先到「设置」启用 AI 解读")
                sections: list = []

                try:
                    m = metrics.collect()
                    cpu = m.get("cpu") or {}
                    mem = m.get("mem") or {}
                    up = m.get("uptime") or {}
                    cpu_t = cpu.get("temp_c")
                    parts = [f"【系统】主机 {m.get('hostname','NAS')}，已运行 "
                             f"{up.get('days',0)} 天 {up.get('hours',0)} 小时，"
                             f"CPU 占用 {cpu.get('percent','--')}%"
                             + (f"，CPU 温度 {cpu_t}°C" if cpu_t is not None else "")
                             + f"，内存占用 {mem.get('percent','--')}%"]
                    vols = m.get("volumes") or []
                    if vols:
                        vs = "；".join(f"{v['mount']} 已用 {v.get('percent',0)}%"
                                       for v in vols[:8])
                        parts.append("【存储空间】" + vs)
                    tr = m.get("trends") or []
                    for t in tr:
                        if t.get("days_to_full"):
                            parts.append(f"【趋势预警】{t['mount']} 按最近增长速度，"
                                         f"预计约 {t['days_to_full']} 天后存满（当前 {t['percent']}%）")
                        else:
                            parts.append(f"【趋势】{t['mount']} 在缓慢增长（当前 {t['percent']}%）")
                    sections.append("\n".join(parts))
                except Exception as exc:  # noqa: BLE001 单块失败不拖垮整体
                    sections.append(f"【系统】硬件指标暂时读不到（{exc}）")

                try:
                    vdata = build_volume_list().get("volumes", [])
                    snap_lines = []
                    for v in vdata[:6]:
                        cnt = v.get("snapshot_count", 0)
                        snap_lines.append(
                            f"{v.get('name','?')}：{cnt} 张快照"
                            + ("，全部受保护" if cnt else "，还没有快照，建议立即拍第一张")
                            + (f"，最新 {str(v.get('latest_snapshot'))[:16]}" if v.get("latest_snapshot") else ""))
                    if snap_lines:
                        sections.append("【快照保护】\n" + "\n".join(snap_lines))
                except Exception:  # noqa: BLE001
                    pass

                try:
                    alerts = storage.scan_tamper()
                    if alerts:
                        sections.append("【当前告警】\n" + "\n".join(
                            f"- [{a.get('level','')}] {a.get('type','')}：{a.get('detail','')}"
                            for a in alerts[:10]))
                    else:
                        sections.append("【当前告警】无，快照保护正常")
                except Exception:  # noqa: BLE001
                    pass

                report = "\n\n".join(sections)
                result, err = ai.interpret(report)
                if err:
                    self._send_json({"ok": False, "error": err}, 400)
                else:
                    self._send_json({"ok": True, "text": result, "context": report})

            elif route == "/api/ai/ask":
                # 问 AI：自然语言问 NAS 状态，自动注入当前指标/告警作为背景
                question = (payload.get("question") or "").strip()
                if not question:
                    raise StorageError("请先输入问题")
                if not ai.is_ready():
                    raise StorageError("AI 未配置，请先到「设置」启用 AI 解读")
                ok, rem = ai_butler.quota_check()
                if not ok:
                    self._send_json({"ok": False, "quota": True, "remaining": 0,
                                     "upgrade": True,
                                     "error": "本月 AI 次数已用完（免费版 20 次/月）。"
                                              "升级家庭版不限量，或下月自动恢复。"}, 400)
                    return
                ctx_parts: list = []
                try:
                    m = metrics.collect()
                    ctx_parts.append(
                        f"当前系统：CPU {((m.get('cpu') or {}).get('percent') or '--')}%，"
                        f"内存 {((m.get('mem') or {}).get('percent') or '--')}%，"
                        + "；".join(f"{v['mount']} 已用 {v.get('percent',0)}%"
                                    for v in (m.get("volumes") or [])[:6]))
                    for t in (m.get("trends") or []):
                        if t.get("days_to_full"):
                            ctx_parts.append(f"趋势：{t['mount']} 预计约 {t['days_to_full']} 天后存满")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    alerts = storage.scan_tamper()
                    ctx_parts.append("当前告警：" + ("无" if not alerts else
                        "；".join(f"[{a.get('level','')}]{a.get('type','')}" for a in alerts[:8])))
                except Exception:  # noqa: BLE001
                    pass
                if payload.get("use_kb"):
                    # 结合知识库（家庭/专业版）：召回知识库片段注入上下文；配额用完/异常则静默跳过
                    try:
                        ok_kb, _rem = knowledge.quota_check()
                        if ok_kb:
                            hits = knowledge.search(question, top_k=4)
                            if hits:
                                refs = "\n\n".join("[资料] " + h["text"] for h in hits)
                                ctx_parts.append("用户知识库中可能相关的资料（回答时可参考并说明来源）：\n" + refs)
                                knowledge.quota_incr()
                    except Exception:  # noqa: BLE001
                        pass
                if payload.get("use_kb"):
                    # 结合知识库（家庭/专业版）：召回知识库片段注入上下文；配额用完/异常则静默跳过
                    try:
                        ok_kb, _rem = knowledge.quota_check()
                        if ok_kb:
                            hits = knowledge.search(question, top_k=4)
                            if hits:
                                refs = "\n\n".join("[资料] " + h["text"] for h in hits)
                                ctx_parts.append("用户知识库中可能相关的资料（回答时可参考并说明来源）：\n" + refs)
                                knowledge.quota_incr()
                    except Exception:  # noqa: BLE001
                        pass
                history = payload.get("history") if isinstance(payload.get("history"), list) else None
                result, err = ai.answer(question, context="\n".join(ctx_parts), history=history)
                if err:
                    self._send_json({"ok": False, "error": err}, 400)
                elif result is None:
                    self._send_json({"ok": False, "error": "AI 未启用或未配置密钥", "ready": False}, 400)
                else:
                    ai_butler.quota_incr()
                    self._send_json({"ok": True, "text": result,
                                     "remaining": ai_butler.quota_remaining()})

            elif route == "/api/ai/butler":
                # AI 管家：对话式工具调用（建快照/列卷/查改动/搜文件/看健康）
                question = (payload.get("question") or "").strip()
                if not question:
                    raise StorageError("请先输入问题")
                if not ai.is_ready():
                    raise StorageError("AI 未配置，请先到「设置」启用 AI 解读")
                ok, rem = ai_butler.quota_check()
                if not ok:
                    self._send_json({"ok": False, "quota": True, "remaining": 0,
                                     "upgrade": True,
                                     "error": "本月 AI 次数已用完（免费版 20 次/月）。"
                                              "升级家庭版不限量，或下月自动恢复。"}, 400)
                    return
                history = payload.get("history") if isinstance(payload.get("history"), list) else None
                result, err = ai_butler.run(question, history=history)
                if err:
                    self._send_json({"ok": False, "error": err}, 400)
                elif result is None:
                    self._send_json({"ok": False, "error": "AI 未启用或未配置密钥", "ready": False}, 400)
                else:
                    ai_butler.quota_incr()
                    self._send_json({"ok": True, "text": result,
                                     "remaining": ai_butler.quota_remaining()})

            elif route.startswith("/api/ai/kb/"):
                # 二期：RAG 知识库（家庭/专业版专属）
                if not editions.limits().get("knowledge"):
                    self._send_json({"ok": False, "upgrade": True,
                                     "error": "知识库为家庭版专属功能，请升级家庭版。"})
                    return
                if route == "/api/ai/kb/ingest":
                    if not ai.is_ready():
                        raise StorageError("AI 未配置，请先到「设置」启用 AI 解读（需支持 embeddings 的模型）")
                    title = (payload.get("title") or "").strip()
                    text = payload.get("text")
                    data_b64 = payload.get("data_base64")
                    if data_b64:
                        import base64
                        raw = base64.b64decode(data_b64)
                        # 写临时文件再摄入，便于复用文件解析（pdf/docx 等）
                        import tempfile
                        suf = (payload.get("ext") or "txt").lower().lstrip(".")
                        tf = tempfile.NamedTemporaryFile(suffix="." + suf, delete=False)
                        tf.write(raw); tf.close()
                        try:
                            res = knowledge.ingest_file(tf.name)
                        finally:
                            try:
                                os.remove(tf.name)
                            except Exception:  # noqa: BLE001
                                pass
                        self._send_json({"ok": True, **res})
                    elif text is not None:
                        if not title:
                            title = "粘贴内容-%d" % int(time.time())
                        res = knowledge.ingest_text(title, text, kind="text")
                        self._send_json({"ok": True, **res})
                    else:
                        raise StorageError("请提供 text 或 data_base64")
                elif route == "/api/ai/kb/delete":
                    doc_id = (payload.get("id") or "").strip()
                    if not doc_id:
                        raise StorageError("缺少 id")
                    knowledge.delete_doc(doc_id)
                    self._send_json({"ok": True})
                elif route == "/api/ai/kb/query":
                    question = (payload.get("question") or "").strip()
                    if not question:
                        raise StorageError("请先输入问题")
                    if not ai.is_ready():
                        raise StorageError("AI 未配置，请先到「设置」启用 AI 解读")
                    ok, rem = knowledge.quota_check()
                    if not ok:
                        self._send_json({"ok": False, "quota": True, "remaining": 0,
                                         "upgrade": True,
                                         "error": "本月知识库调用已用完（家庭版 500 次/月），下月自动恢复或升级专业版不限量。"})
                        return
                    ans, err, sources = knowledge.query(question)
                    if err:
                        self._send_json({"ok": False, "error": err}, 400)
                    elif ans is None:
                        self._send_json({"ok": True, "text": err or "未命中知识库",
                                         "sources": [], "remaining": knowledge.quota_remaining()})
                    else:
                        knowledge.quota_incr()
                        titles = knowledge.doc_titles(sources)
                        self._send_json({"ok": True, "text": ans,
                                         "sources": [{"id": s, "title": titles.get(s, s)} for s in sources],
                                         "remaining": knowledge.quota_remaining()})
                else:
                    raise StorageError("未知的知识库接口")

            elif route.startswith("/api/ai/emergency/"):
                # 三期：应急响应自动化（专业版专属）
                if not editions.limits().get("emergency"):
                    self._send_json({"ok": False, "upgrade": True,
                                     "error": "应急响应自动化为专业版专属功能，请升级专业版。"})
                    return
                if route == "/api/ai/emergency/snapshot":
                    vid = (payload.get("volume") or "").strip()
                    if not vid:
                        raise StorageError("请提供 volume（卷 id 或挂载点）")
                    res = response.snapshot_now(vid, payload.get("label") or "")
                    self._send_json({"ok": True, **res})
                elif route == "/api/ai/emergency/isolate":
                    path = (payload.get("path") or "").strip()
                    if not path:
                        raise StorageError("请提供 path")
                    res = response.isolate(path)
                    self._send_json({"ok": True, **res})
                elif route == "/api/ai/emergency/clean":
                    vid = (payload.get("volume") or "").strip()
                    keep = int(payload.get("keep") or 10)
                    if not vid:
                        raise StorageError("请提供 volume")
                    res = response.clean_old(vid, keep)
                    self._send_json({"ok": True, **res})
                else:
                    raise StorageError("未知的应急接口")

            elif route.startswith("/api/ai/photo/"):
                # 三期：照片语义搜索（专业版专属）
                if not editions.limits().get("photo_search"):
                    self._send_json({"ok": False, "upgrade": True,
                                     "error": "照片语义搜索为专业版专属功能，请升级专业版。"})
                    return
                if route == "/api/ai/photo/index":
                    root = (payload.get("root") or "").strip()
                    batch = int(payload.get("batch") or 10)
                    res = photo_search.index_gallery(root, batch=batch)
                    self._send_json(res)
                elif route == "/api/ai/photo/search":
                    q = (payload.get("query") or "").strip()
                    top_k = int(payload.get("top_k") or 12)
                    res = photo_search.semantic_search(q, top_k=top_k)
                    self._send_json(res)
                else:
                    raise StorageError("未知的照片检索接口")

            elif route == "/api/ai/local":
                # 本地 AI 中转：浏览器直连用户电脑 Ollama 被 CORS 拦时的兜底通道。
                # 后端直接调用户电脑的 Ollama（须监听 0.0.0.0，仅限 11434 端口防滥用）。
                question = (payload.get("question") or "").strip()
                if not question:
                    raise StorageError("请先输入问题")
                base = (payload.get("base_url") or "").strip().rstrip("/")
                model = (payload.get("model") or "").strip()
                if not base:
                    raise StorageError("请先到「设置 → AI」填写本地 AI 服务地址（如 http://192.168.8.242:11434/v1）")
                if ":11434" not in base:
                    raise StorageError("本地 AI 中转仅支持 Ollama 服务地址（须含 :11434 端口）")
                ctx = (payload.get("context") or "").strip()
                history = payload.get("history") if isinstance(payload.get("history"), list) else None
                messages = []
                if ctx:
                    messages.append({"role": "system", "content": "你是 NAS 数据安全助手，用通俗中文回答。背景：" + ctx})
                for m in (history or [])[-20:]:
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant") \
                            and isinstance(m.get("content"), str) and m["content"].strip():
                        messages.append({"role": m["role"], "content": m["content"][:4000]})
                messages.append({"role": "user", "content": question})
                local_cfg = {"provider": "ollama", "base_url": base, "model": model or "qwen2.5:7b"}
                text, err = ai._chat(messages, local_cfg, timeout=120)
                if err:
                    self._send_json({"ok": False, "error": "本地 AI（NAS 中转）：" + err}, 400)
                else:
                    self._send_json({"ok": True, "text": text, "via": "nas-relay"})

            elif route == "/api/autosnapshot":
                if not isinstance(payload, dict):
                    raise StorageError("配置格式错误")
                cfg = autosnapshot.save_config(payload)
                self._send_json({"ok": True, "config": cfg})

            elif route == "/api/autosnapshot/run":
                # 立即执行一轮自动快照（供测试 / 手动触发）
                self._send_json({"ok": True, **autosnapshot.run_once()})

            elif route == "/api/duplicates/scan":
                # 重复文件扫描：只读，产出报告。后台线程执行，进度走 status 接口。
                root = (payload.get("root") or "").strip()
                if not root:
                    raise StorageError("缺少 root 参数（要扫描的目录）")
                _assert_root_in_mounts(root)
                min_mb = payload.get("min_mb", 1)
                min_age = payload.get("min_age_days", 7)
                try:
                    min_mb = max(0, int(min_mb))
                    min_age = max(0, int(min_age))
                except (TypeError, ValueError):
                    raise StorageError("min_mb / min_age_days 必须是整数")
                self._send_json(duplicates.start_scan(root, min_mb=min_mb, min_age_days=min_age))

            elif route == "/api/duplicates/quarantine":
                # 隔离（软删除）：confirm 严格 True + 只允许报告内路径 + 每组至少留一份
                self._send_json(duplicates.quarantine_files(
                    payload.get("files"), payload.get("confirm") is True))

            elif route == "/api/duplicates/restore":
                self._send_json(duplicates.restore_files(
                    payload.get("ids"), payload.get("confirm") is True))

            elif route == "/api/duplicates/purge":
                # 彻底删除隔离区文件（不可恢复）：confirm + 隔离日志白名单 + 路径格式三重护栏
                self._send_json(duplicates.purge_quarantine(
                    payload.get("ids"), payload.get("confirm") is True))

            elif route == "/api/junk/scan":
                self._send_json(junk.start_scan())
            elif route == "/api/junk/clean":
                # 按类别清理（后台任务，立即返回）：confirm 严格 True + 逐项校验
                # 属于该类别报告 + 黑名单路径拒绝；进度经 /api/junk/status 的 clean 字段
                # 支持多类别（categories=[...]，一键全清，按顺序逐类执行）
                self._send_json(junk.start_clean(
                    payload.get("category"), payload.get("confirm") is True,
                    payload.get("paths"), payload.get("categories")))

            elif route == "/api/daily-report/run":
                # 立即生成并推送一次每日状态日报（手动测试 / 即时发送）
                self._send_json({"ok": True, "last": daily_report.send_daily()})

            elif route == "/api/daily-report/config":
                # 保存日报开关与推送时间（hour/minute）
                cfg = daily_report.load_config()
                if "enabled" in payload:
                    cfg["enabled"] = bool(payload.get("enabled"))
                h = payload.get("hour")
                m = payload.get("minute")
                if isinstance(h, int) and 0 <= h <= 23:
                    cfg["hour"] = h
                if isinstance(m, int) and 0 <= m <= 59:
                    cfg["minute"] = m
                daily_report.save_config(cfg)
                self._send_json({"ok": True, "config": cfg})

            # ---------- 跨品牌多设备总控制台 ----------
            elif route == "/api/devices/add":
                _dok, _dmsg = editions.check_devices(len(devices.load_devices()) + 1)
                if not _dok:
                    self._send_json({"ok": False, "error": _dmsg, "upgrade": True})
                    return
                self._send_json({"ok": True, **devices.add_device(payload)})
            elif route == "/api/devices/remove":
                dev_id = (payload.get("id") or "").strip()
                if not dev_id:
                    raise StorageError("缺少 id 参数")
                self._send_json({"ok": True, **devices.remove_device(dev_id)})
            elif route == "/api/devices/scan":
                # 自动扫描：局域网 + 异地组网（Tailscale/WireGuard/VPN 等）里的 TS Safe
                self._send_json({"ok": True, **netscan.scan(payload)})
            elif route == "/api/devices/rename":
                dev_id = (payload.get("id") or "").strip()
                name = (payload.get("name") or "").strip()
                if not dev_id:
                    raise StorageError("缺少 id 参数")
                self._send_json({"ok": True, **devices.rename_device(dev_id, name)})
            elif route == "/api/devices/agent":
                # 手动补装轻量代理：标记待装并生成专属安装令牌
                dev_id = (payload.get("id") or "").strip()
                if not dev_id:
                    raise StorageError("缺少 id 参数")
                self._send_json(devices.set_agent_request(dev_id))
            elif route == "/api/devices/refresh":
                # 强制刷新聚合（忽略远程缓存，立即重拉）
                self._send_json({"ok": True, **devices.collect_all(force=True)})
            elif route == "/api/devices/manage":
                # 联机设备管控（设置后台）：改单台设备的功能开关 / 补装代理
                # 注意：do_POST 里没有 method 变量（只有 do_GET/do_POST 两套独立路由链），
                # 这里走到就一定是 POST，别再判断 method，否则 NameError 直接 500。
                self._send_json({**devices.set_manage(payload)})

            # ---------- 换机迁移 ----------
            elif route == "/api/migrate/preview":
                # 导入预览（dry_run）：计算路径映射 + 能力降级结果，不落地
                if not editions.can("migrate"):
                    self._send_json({"ok": False, "error":
                        "换机迁移是家庭版/专业版功能，当前版本没有。升级后即可一键换机",
                        "upgrade": True})
                    return
                bundle = payload.get("bundle")
                ok, msg = migrate.validate_bundle(bundle)
                if not ok:
                    raise StorageError(msg)
                report = migrate.apply_bundle(
                    bundle,
                    path_map=payload.get("path_map") or {},
                    target_brand=payload.get("target_brand") or None,
                    dry_run=True,
                )
                self._send_json({"ok": True, "report": report})
            elif route == "/api/migrate/import":
                # 正式导入：需 confirm=True；写入本机 state_dir
                if not editions.can("migrate"):
                    self._send_json({"ok": False, "error":
                        "换机迁移是家庭版/专业版功能，当前版本没有。升级后即可一键换机",
                        "upgrade": True})
                    return
                if payload.get("confirm") is not True:
                    self._send_json({
                        "ok": False,
                        "error": "导入会覆盖本机配置，需要 confirm=true 二次确认",
                    }, 400)
                    return
                bundle = payload.get("bundle")
                ok, msg = migrate.validate_bundle(bundle)
                if not ok:
                    raise StorageError(msg)
                report = migrate.apply_bundle(
                    bundle,
                    path_map=payload.get("path_map") or {},
                    target_brand=payload.get("target_brand") or None,
                    dry_run=False,
                )
                self._send_json({"ok": True, "report": report})

            # ---------- 版本与激活 ----------
            elif route == "/api/license/issue":
                # 模拟「支付成功」后由发卡端下发激活码：本机按机器码实时签一张码。
                # 安全收口：默认关闭（否则任何人 POST 一下就能免费拿真码）。
                # 仅演示环境设环境变量 NASSAFE_ISSUE_DEMO=1 才放行。
                if os.environ.get("NASSAFE_ISSUE_DEMO") != "1":
                    self._send_json({"ok": False, "pay_closed": True,
                                     "error": "在线支付尚未开通。请把本机机器码发给卖家，支付后获取激活码"}, 403)
                    return
                ed = (payload.get("edition") or "").strip()
                if ed not in ("home", "business"):
                    self._send_json({"ok": False, "error": "无效版本"}, 400)
                    return
                try:
                    licensing.ensure_secret()
                    fp = licensing.machine_fingerprint()
                    code = licensing.issue_key(ed, fp, 0)
                    self._send_json({"ok": True, "edition": ed, "code": code})
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"ok": False, "error": f"发码失败：{exc}"}, 500)
            elif route == "/api/license/activate":
                key = str(payload.get("key") or "")
                ok, msg = licensing.activate(key)
                self._send_json({"ok": ok, "message": msg, **licensing.info()},
                                200 if ok else 400)
            elif route == "/api/license/deactivate":
                ok, msg = licensing.deactivate()
                self._send_json({"ok": ok, "message": msg, **licensing.info()},
                                200 if ok else 500)

            else:
                self._send_json({"ok": False, "error": f"未知接口: {route}"}, 404)

        except (StorageError, CommandNotFound) as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"ok": False, "error": f"服务内部错误: {exc}"}, 500)


def main() -> None:
    profile = storage.probe_system()
    print("=" * 58)
    print("  TS Safe — 防勒索快照管理")
    print("=" * 58)
    print(f"  系统      : {profile.os_name} ({profile.os_id})")
    print(f"  内核      : {profile.kernel}")
    print(f"  容器内    : {'是' if profile.is_container else '否'}")
    print(f"  可用 FS   : {', '.join(profile.fs_available) or '未检测到'}")
    print(f"  httm      : {'已安装' if profile.has_httm else '未安装（将用内置方案）'}")
    print(f"  界面目录  : {WEB_DIR}")
    print(f"  监听      : http://{HOST}:{PORT}")
    for warn in profile.warnings:
        print(f"  [提示] {warn}")
    print("=" * 58)

    # 若还没账号且环境变量给了初始密码，自动创建管理员（首次部署/演示环境）
    if auth.needs_setup():
        init_user = os.environ.get("NASSAFE_WEB_USER", "nassafe")
        init_pass = os.environ.get("NASSAFE_WEB_PASS", "")
        if init_pass:
            try:
                auth.create_user(init_user, init_pass, "admin")
                print(f"  初始账号  : {init_user}（已自动创建）")
            except Exception as exc:
                print(f"  [提示] 自动创建初始账号失败: {exc}")
        else:
            print("  [提示] 尚未创建管理员账号，首次打开页面会引导初始化")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    # 后台自动推送线程：定期扫描新告警/变动并分发到已配置通道
    try:
        notify.start_notifier(int(os.environ.get("NASSAFE_NOTIFY_INTERVAL", "60")))
    except Exception:  # noqa: BLE001
        pass
    try:
        autosnapshot.start_scheduler()  # 自动快照守护线程（每小时 vital 锁快照）
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        # 异常看门狗：小助手被关闭/网页没开时，接管微信服务号等远端提醒
        anomalies.start_watchdog(int(os.environ.get("NASSAFE_WATCHDOG_INTERVAL", "120")))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        # 每日健康日报：每天定时聚合快照/告警/空间/硬盘，经通知链路推送到用户通道
        daily_report.start_scheduler()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
