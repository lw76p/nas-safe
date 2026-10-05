"""告警历史记录 —— 把本机每次出现的告警落盘，形成「历史记录」。

设计要点（与 TS Safe 部署形态一致）：
- 「告警信息」页面要展示本机 + 每台联机设备的「当前告警 + 历史记录」。
- 每台设备各自记录自己的历史（对端也跑同一套 TS Safe，各自落盘）。
- 本模块只负责「本机」历史；联机设备由控制台经 HTTP 拉取对端的 /api/alerts/summary。
- 同一告警（device + key）再次出现时只更新 last_seen / 计数，不重复建记录；
  某告警在最近一次扫描中消失，则标记为 resolved（保留记录供回溯）。
- 历史文件：<state_dir>/alerts_history.json（与 devices.json / 快照状态同级持久化）。

告警记录字段：
- id, device, device_name, type(metric/tamper/integrity/behavior)
- level(critical/warn), key, title, detail
- first_seen, last_seen, status(active/resolved), resolved_at, count
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

FILE = "alerts_history.json"
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def _path() -> str:
    import storage  # 延迟导入，避免循环依赖
    return os.path.join(storage.state_dir(), FILE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _load() -> list:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(records: list) -> None:
    try:
        d = os.path.dirname(_path())
        os.makedirs(d, exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 归一化：把 anomalies.collect_anomalies 的结果转成统一告警项
# ---------------------------------------------------------------------------

def normalize(items: list) -> list:
    out = []
    for a in (items or []):
        sev = a.get("sev", 1)
        view = a.get("view") or ""
        # 与前端一致：view=monitor 的是快照/勒索保护类，其余是硬件/容量类
        atype = "tamper" if view == "monitor" else "metric"
        out.append({
            "type": atype,
            "level": "critical" if sev >= 2 else "warn",
            "key": a.get("key") or a.get("title") or "unknown",
            "title": a.get("title") or "告警",
            "detail": a.get("detail") or "",
        })
    return out


# ---------------------------------------------------------------------------
# 用当前告警集合刷新历史：新增/更新活跃记录，消失的标记 resolved
# ---------------------------------------------------------------------------

def reconcile(device_id: str, device_name: str, items: list) -> list:
    norm = normalize(items)
    now = _now_iso()
    with _lock:
        records = _load()
        seen = set()
        for it in norm:
            rk = (device_id, it["key"])
            seen.add(rk)
            existing = next(
                (r for r in records
                 if r.get("device") == device_id and r.get("key") == it["key"]),
                None,
            )
            if existing is None:
                records.append({
                    "id": f"{device_id}:{it['key']}",
                    "device": device_id,
                    "device_name": device_name,
                    "type": it["type"],
                    "level": it["level"],
                    "key": it["key"],
                    "title": it["title"],
                    "detail": it["detail"],
                    "first_seen": now,
                    "last_seen": now,
                    "status": "active",
                    "resolved_at": None,
                    "count": 1,
                })
            else:
                was_resolved = existing.get("status") != "active"
                existing["last_seen"] = now
                existing["level"] = it["level"]
                existing["title"] = it["title"]
                existing["detail"] = it["detail"]
                existing["status"] = "active"
                existing["resolved_at"] = None
                # 只在「从已恢复再次变活跃」时计一次数，避免每轮扫描都 +1
                if was_resolved:
                    existing["count"] = int(existing.get("count", 0)) + 1
        # 标记消失：本设备下仍为 active 但本轮没出现 → resolved
        for r in records:
            if (r.get("device") == device_id
                    and r.get("status") == "active"
                    and (r["device"], r["key"]) not in seen):
                r["status"] = "resolved"
                r["resolved_at"] = now
        _save(records)
        return records


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def history(device_id: str | None = None, include_resolved: bool = True,
            limit: int = 300) -> list:
    with _lock:
        records = _load()
    if device_id:
        records = [r for r in records if r.get("device") == device_id]
    if not include_resolved:
        records = [r for r in records if r.get("status") == "active"]
    records.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    return records[:limit]


def counts(device_id: str) -> dict:
    recs = history(device_id, include_resolved=True)
    active = [r for r in recs if r.get("status") == "active"]
    resolved = [r for r in recs if r.get("status") == "resolved"]
    return {
        "critical": sum(1 for r in active if r.get("level") == "critical"),
        "warn": sum(1 for r in active if r.get("level") == "warn"),
        "total": len(active),
        "resolved": len(resolved),
    }


# ---------------------------------------------------------------------------
# 清理：删除已恢复且超过 N 天的记录
# ---------------------------------------------------------------------------

def prune(days: int = 90) -> int:
    cutoff = time.time() - days * 86400
    with _lock:
        records = _load()
        kept = [
            r for r in records
            if r.get("status") == "active"
            or (r.get("resolved_at") and _ts(r.get("resolved_at")) > cutoff)
        ]
        removed = len(records) - len(kept)
        if removed:
            _save(kept)
        return removed


# ---------------------------------------------------------------------------
# 后台周期扫描：即使没人打开页面，也持续刷新本机历史
# ---------------------------------------------------------------------------

def start_scanner(interval: int = 120) -> None:
    import metrics  # 延迟导入
    import anomalies
    import devices

    def loop() -> None:
        while True:
            try:
                time.sleep(interval)
                devs = devices.load_devices()
                local = next((d for d in devs if d.get("id") == "local"
                              or d.get("type") == "local"), None)
                name = (local or {}).get("name") or "本机"
                try:
                    m = metrics.collect(force=False)
                except Exception:  # noqa: BLE001
                    m = {}
                try:
                    items = anomalies.collect_anomalies(m or {})
                except Exception:  # noqa: BLE001
                    items = []
                reconcile("local", name, items)
            except Exception:  # noqa: BLE001  单轮失败不影响线程
                pass

    t = threading.Thread(target=loop, name="alertlog-scanner", daemon=True)
    t.start()
