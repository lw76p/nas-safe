"""
NAS Safe — 后端 API 服务

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
  GET  /api/health                健康检查

安全约定：
  - 所有写操作需要 confirm=true 参数（服务端二次确认）
  - 路径参数统一经过 storage._validate_path 校验
  - 浏览接口强制限定在快照目录内，防止越权读取生产数据
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# 让脚本可以独立运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage  # noqa: E402
from storage import (  # noqa: E402
    StorageError, CommandNotFound, Snapshot, Volume,
)

import integrity  # noqa: E402  v2 内容完整性校验
import behavior   # noqa: E402  v3 勒索行为检测
import notify     # noqa: E402  多渠道告警通知（微信服务号/Webhook/Bark/ntfy/邮件）
import ai         # noqa: E402  AI 解读（多云供应商 + 本地 Ollama）

HOST = os.environ.get("NASSAFE_BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("NASSAFE_PORT", "8848"))
WEB_DIR = os.environ.get("NASSAFE_WEB_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"
)

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

_SECRET_FIELDS = {"appsecret", "secret", "pass", "api_key"}


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
    return {
        "ok": True,
        "system": profile.to_dict(),
        "server_time": iso_now(),
        "version": "1.0.0",
    }


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


def find_snapshot(volume_id: str, snapshot_id: str) -> "storage.Snapshot":
    """按 volume_id + snapshot_id 定位统一 Snapshot 对象。"""
    for vol in storage.list_all_volumes():
        if (vol.volume_id or vol.mountpoint) == volume_id:
            for snap in storage.list_all_snapshots(vol):
                if snap.snapshot_id == snapshot_id:
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
    server_version = "NAS Safe/1.0"

    def log_message(self, fmt, *args):  # 静默，避免污染日志
        pass

    # -- 响应helpers ------------------------------------------------------

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

        try:
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
            elif route == "/api/notify/config":
                self._send_json({
                    "ok": True,
                    "config": _mask_notify_cfg(notify.load_config()),
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
                    "providers": list(ai.PROVIDERS.keys()),
                })
            elif route == "/api/system":
                self._send_json(build_system_info())
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
            payload = self._read_json()

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

            elif route == "/api/notify/config":
                # 保存通知配置（channels 列表 + enabled）
                cfg = payload
                if not isinstance(cfg, dict):
                    raise StorageError("配置格式错误")
                cfg.setdefault("enabled", False)
                cfg.setdefault("channels", [])
                notify.save_config(cfg)
                self._send_json({"ok": True, "config": _mask_notify_cfg(notify.load_config())})

            elif route == "/api/notify/test":
                channel = payload.get("channel")
                if not isinstance(channel, dict) or not channel.get("type"):
                    raise StorageError("缺少 channel 配置")
                self._send_json({"ok": True, **notify.send_test(channel)})

            elif route == "/api/ai/config":
                cfg = payload
                if not isinstance(cfg, dict):
                    raise StorageError("配置格式错误")
                cfg.setdefault("enabled", False)
                ai.save_config(cfg)
                self._send_json({"ok": True, "ready": ai.is_ready(), "config": ai.load_config()})

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
    print("  NAS Safe — 防勒索快照管理")
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

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    # 后台自动推送线程：定期扫描新告警/变动并分发到已配置通道
    try:
        notify.start_notifier(int(os.environ.get("NASSAFE_NOTIFY_INTERVAL", "60")))
    except Exception:  # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
