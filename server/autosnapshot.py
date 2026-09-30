"""自动快照调度器 —— 防勒索的最后一公里：让快照真正"自动"。

默认每小时对受保护卷创建 vital 永久锁快照，并按 keep 上限自动清理最旧的
自动快照。配置持久化在 state_dir()/autosnapshot.json。

设计要点：
- 自动快照命名统一前缀 ``auto-`` + ``YYYYMMDDHHMMSS``，便于与用户手动快照区分，
  清理时只动 ``auto-`` 前缀的，绝不碰用户手动创建的快照。
- 创建走统一入口 ``create_snapshot(..., vital=True)``，会自动 register_protected；
  清理走 ``delete_snapshot``，会自动 unregister_protected —— 不会触发篡改误报。
- 远程管理模式（QNAP SSH 回连）同样适用：list_all_volumes / create_snapshot
  都已适配，调度线程每小时 SSH 一次，开销可接受。
- 调度线程为 daemon，不阻塞主进程退出；run_once 内部全程 try，单卷失败不影响整体。
"""

import os
import json
import time
import threading
import datetime
import traceback

from storage import (
    list_all_volumes,
    list_all_snapshots,
    create_snapshot,
    delete_snapshot,
    state_dir,
)

AUTO_PREFIX = "auto-"
DEFAULT_INTERVAL_HOURS = 1
DEFAULT_KEEP = 48
_lock = threading.Lock()


def _now_stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d%H%M%S")


def _stamp_to_dt(name: str) -> "datetime.datetime":
    """从 auto- 前缀后的时间戳解析时间，失败返回极小值（排到最旧）。"""
    tail = name[len(AUTO_PREFIX):] if name.startswith(AUTO_PREFIX) else name
    try:
        return datetime.datetime.strptime(tail, "%Y%m%d%H%M%S")
    except Exception:
        return datetime.datetime.min


def config_path() -> str:
    return os.path.join(state_dir(), "autosnapshot.json")


def default_config() -> dict:
    return {
        "enabled": True,
        "interval_hours": int(os.environ.get("NASSAFE_AUTOSNAP_INTERVAL_HOURS")
                              or DEFAULT_INTERVAL_HOURS),
        "keep": int(os.environ.get("NASSAFE_AUTOSNAP_KEEP") or DEFAULT_KEEP),
        # 空列表 = 对所有卷自动快照；填入 volume_id 或卷名则只保护这些卷
        "volumes": [],
    }


def load_config() -> dict:
    p = config_path()
    if not os.path.exists(p):
        return default_config()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default_config()
    if not isinstance(data, dict):
        return default_config()
    base = default_config()
    base.update({k: v for k, v in data.items() if k in base})
    return base


def save_config(cfg: dict) -> dict:
    base = default_config()
    base.update({k: v for k, v in (cfg or {}).items() if k in base})
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=2)
    except OSError:
        # 状态目录不可写时静默跳过，不影响核心快照功能
        pass
    return base


def run_once() -> dict:
    """执行一轮自动快照。返回结构化结果；异常不抛出（供调度线程调用）。"""
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": True, "skipped": "disabled"}

    keep = max(1, int(cfg.get("keep", DEFAULT_KEEP)))
    whitelist = set(str(v) for v in (cfg.get("volumes") or []))

    created, cleaned, errors = [], [], []
    try:
        volumes = list_all_volumes()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"枚举存储卷失败: {exc}"}

    for vol in volumes:
        vkey = str(vol.volume_id or vol.mountpoint or vol.name)
        if whitelist and vkey not in whitelist and vol.name not in whitelist:
            continue
        # 列出该卷现有自动快照
        try:
            snaps = list_all_snapshots(vol)
        except Exception as exc:  # noqa: BLE001
            errors.append({"volume": vkey, "error": f"列举快照失败: {exc}"})
            continue

        auto = [s for s in snaps if (s.name or "").startswith(AUTO_PREFIX)]
        auto.sort(key=lambda s: _stamp_to_dt(s.name or "") or (s.created_at or ""))

        # 超额清理：删除最旧的自动快照（delete_snapshot 会同步 unregister_protected）
        while len(auto) >= keep:
            old = auto.pop(0)
            try:
                delete_snapshot(old)
                cleaned.append(old.name)
            except Exception as exc:  # noqa: BLE001
                errors.append({"volume": vkey, "name": old.name,
                               "error": f"清理失败: {exc}"})

        # 创建新自动快照（create_snapshot 会同步 register_protected + vital 锁）
        try:
            name = AUTO_PREFIX + _now_stamp()
            snap = create_snapshot(vol, name, vital=True)
            created.append({"volume": vkey, "name": snap.name})
        except Exception as exc:  # noqa: BLE001
            errors.append({"volume": vkey, "error": f"创建失败: {exc}"})

    return {"ok": True, "created": created, "cleaned": cleaned, "errors": errors}


def _latest_auto_snapshot_time() -> "datetime.datetime | None":
    """返回所有卷中最新的 auto- 快照时间，用于首次启动时对齐周期，防止重启洪水。"""
    latest = None
    try:
        volumes = list_all_volumes()
    except Exception:  # noqa: BLE001
        return None
    for vol in volumes:
        try:
            snaps = list_all_snapshots(vol)
        except Exception:  # noqa: BLE001
            continue
        for s in snaps:
            name = s.name or ""
            if not name.startswith(AUTO_PREFIX):
                continue
            dt = _stamp_to_dt(name)
            if dt and dt != datetime.datetime.min:
                if latest is None or dt > latest:
                    latest = dt
    return latest


def start_scheduler() -> "threading.Thread":
    """后台守护线程：按 interval_hours 周期执行 run_once。

    首次启动会等 60s 后检查最新 auto- 快照：如果它还在当前周期内，
    则睡到周期满再打，避免每次容器/服务重启都额外产生一张快照。
    """
    def loop() -> None:
        first = True
        while True:
            cfg = load_config()
            interval = max(1, int(cfg.get("interval_hours", DEFAULT_INTERVAL_HOURS))) * 3600
            if first:
                # 首次启动延迟 60s，避免重启后立即打一批快照
                time.sleep(60)
                first = False
                # 防重启洪水：若最新 auto 快照仍在周期内，额外对齐到周期末尾
                try:
                    latest = _latest_auto_snapshot_time()
                    if latest is not None:
                        elapsed = (datetime.datetime.now() - latest).total_seconds()
                        if elapsed < interval:
                            wait_more = interval - elapsed
                            print(f"[autosnapshot] 首次启动对齐：最新快照 {latest.isoformat()} 在周期内，额外等待 {wait_more:.0f}s")
                            time.sleep(wait_more)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
            else:
                time.sleep(interval)
            try:
                with _lock:
                    run_once()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    t = threading.Thread(target=loop, daemon=True, name="autosnap")
    t.start()
    return t
