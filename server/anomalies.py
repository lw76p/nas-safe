"""异常判定与主动推送看门狗。

统一一份「什么算异常」的规则，供三端复用：
  · 网页端（/api/anomalies）
  · 桌面小助手（/api/anomalies）
  · 服务端看门狗（无人值守时自己发微信）

推送策略（用户拍板，2026-09-30）：
  · 桌面端与微信端**同步发**，不判断用户是否坐在电脑前
    （判断键鼠空闲既容易误判——看视频不动鼠标也算空闲——又要常驻检测，不划算）
  · 小助手在线：小助手弹窗 + 微信/邮件同一时刻发出（小助手带 keys 上报，看门狗去重跳过）
  · 小助手被用户关闭：看门狗接管，继续发微信/邮件，提醒不丢
  · 去重：同一异常 key 只推一次；异常消失后清除记录，复发才再提醒

助手在线状态持久化在 state_dir()/agent_status.json。
"""
from __future__ import annotations

import json
import os
import threading
import time

import metrics
import notify
import storage

# 同一异常的重推冷却（秒）：防止桌面端与看门狗各发一次变成重复打扰
COOLDOWN = 1800
# 看门狗默认检测间隔（秒）；与小助手默认 120s 一致，保证两端几乎同时收到
WATCHDOG_INTERVAL = 120
# 小助手心跳超时：超过这个时间没上报就认为已关闭（默认轮询 120s，留 4 倍余量）
OFFLINE_AFTER = 480


# ---------------------------------------------------------------------------
# 异常判定（规则与网页端、小助手保持一致）
# ---------------------------------------------------------------------------
def collect_anomalies(m: dict | None = None) -> list:
    """返回 [{key, sev, title, detail, view}]，sev: 2=严重 1=注意。"""
    if m is None:
        try:
            m = metrics.collect()
        except Exception:  # noqa: BLE001
            return []
    out: list = []
    cap = m.get("capabilities") or {}
    cpu = m.get("cpu") or {}

    if cap.get("cpu_temp") and cpu.get("temp_c") is not None:
        t = cpu["temp_c"]
        if t >= 90:
            out.append({"key": "cpu-temp", "sev": 2, "title": f"CPU 温度过高（{t}°C）",
                        "detail": "CPU 温度达到危险区间，检查机箱风道与散热。", "view": "dashboard"})
        elif t >= 80:
            out.append({"key": "cpu-temp", "sev": 1, "title": f"CPU 温度偏高（{t}°C）",
                        "detail": "持续高温会缩短硬件寿命，留意散热。", "view": "dashboard"})

    load1 = cpu.get("load1")
    if load1 is not None:
        try:
            load1 = float(load1)
        except (TypeError, ValueError):
            load1 = None
    if load1 is not None and load1 >= 8:
        out.append({"key": "cpu-load", "sev": 2 if load1 >= 16 else 1,
                    "title": f"系统负载过高（{load1}）",
                    "detail": "有进程长期占用 CPU，可能是异常程序在运行。", "view": "dashboard"})

    disks = m.get("disks") or []
    for group, cn in (
        ([d for d in disks if str(d.get("name", "")).startswith("nvme")], "固态"),
        ([d for d in disks if not str(d.get("name", "")).startswith("nvme")], "硬盘"),
    ):
        for i, d in enumerate(group):
            t = d.get("temp_c")
            if t is None:
                continue
            if t >= 60:
                out.append({"key": f"disk-{d.get('name')}", "sev": 2,
                            "title": f"{cn} {i + 1} 过热（{t}°C）",
                            "detail": "硬盘温度过高，长期会影响寿命，建议排查散热与通风。", "view": "dashboard"})
            elif t >= 50:
                out.append({"key": f"disk-{d.get('name')}", "sev": 1,
                            "title": f"{cn} {i + 1} 温度偏高（{t}°C）",
                            "detail": "温度高于舒适区间，留意后续变化。", "view": "dashboard"})

    for v in m.get("volumes") or []:
        if not v or (v.get("total_kb") or 0) <= 0:
            continue
        p = v.get("percent", 0)
        name = str(v.get("mount", "")).split("/")[-1] or v.get("mount")
        if p >= 90:
            out.append({"key": f"vol-{v.get('mount')}", "sev": 2,
                        "title": f"「{name}」空间即将用尽（已用 {p}%）",
                        "detail": "空间不足会导致写入失败，建议清理或扩容。", "view": "dashboard"})
        elif p >= 75:
            out.append({"key": f"vol-{v.get('mount')}", "sev": 1,
                        "title": f"「{name}」空间偏紧（已用 {p}%）",
                        "detail": "留意增长趋势，提前规划清理。", "view": "dashboard"})

    for t in m.get("trends") or []:
        if t.get("days_to_full"):
            name = str(t.get("mount", "")).split("/")[-1] or t.get("mount")
            out.append({"key": f"trend-{t.get('mount')}", "sev": 1,
                        "title": f"「{name}」预计 {t['days_to_full']} 天后存满",
                        "detail": "按近期增长速度推算，建议提前清理。", "view": "dashboard"})

    # 防勒索告警（快照被删/基线被改等）：优先级最高
    try:
        for a in storage.scan_tamper(include_integrity=True) or []:
            lvl = str(a.get("level") or "warn")
            title = a.get("title") or "快照保护异常"
            out.append({
                "key": f"tamper-{title}",
                "sev": 2 if lvl == "critical" else 1,
                "title": title,
                "detail": a.get("detail") or "保护状态发生变化，请立即确认。",
                "view": "monitor",
            })
    except Exception:  # noqa: BLE001
        pass

    # 同一 key 只保留最严重的一条
    best: dict = {}
    for a in out:
        cur = best.get(a["key"])
        if cur is None or a["sev"] > cur["sev"]:
            best[a["key"]] = a
    return sorted(best.values(), key=lambda x: (-x["sev"], x["key"]))


# ---------------------------------------------------------------------------
# 桌面小助手在线状态
# ---------------------------------------------------------------------------
def _agent_state_path() -> str:
    return os.path.join(storage.state_dir(), "agent_status.json")


def set_agent_online(online: bool, host: str = "") -> dict:
    rec = {"online": bool(online), "last_seen": time.time(), "host": host}
    try:
        os.makedirs(storage.state_dir(), exist_ok=True)
        with open(_agent_state_path(), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return rec


def agent_status() -> dict:
    """返回小助手在线状态（含心跳超时判定）。"""
    rec = {}
    try:
        with open(_agent_state_path(), "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    last = float(rec.get("last_seen") or 0)
    online = bool(rec.get("online")) and (time.time() - last) < OFFLINE_AFTER
    return {
        "online": online,
        "last_seen": last,
        "host": rec.get("host") or "",
        "fallback": "wechat" if not online else "",
    }


# ---------------------------------------------------------------------------
# 推送去重 + 看门狗
# ---------------------------------------------------------------------------
def _sent_path() -> str:
    return os.path.join(storage.state_dir(), "anomaly_sent.json")


def _load_sent() -> dict:
    try:
        with open(_sent_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_sent(sent: dict) -> None:
    try:
        os.makedirs(storage.state_dir(), exist_ok=True)
        with open(_sent_path(), "w", encoding="utf-8") as f:
            json.dump(sent, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def mark_sent(keys: list) -> None:
    """登记已推送的异常 key（小助手同步发送时调用，避免看门狗重复发）。"""
    if not keys:
        return
    sent = _load_sent()
    now = time.time()
    for k in keys:
        if k:
            sent[str(k)] = now
    _save_sent(sent)


def scan_and_push() -> dict:
    """检测一次异常并推送「新增」的部分；异常消失后清除记录，复发才再提醒。"""
    items = collect_anomalies()
    keys = {i["key"] for i in items}
    sent = _load_sent()
    now = time.time()
    for k in list(sent):
        if k not in keys:
            sent.pop(k)  # 异常已恢复，允许下次复发再提醒
    fresh = [i for i in items if (now - float(sent.get(i["key"]) or 0)) > COOLDOWN]
    if not fresh:
        _save_sent(sent)
        return {"ok": True, "pushed": 0, "items": items}
    for i in fresh:
        sent[i["key"]] = now
    _save_sent(sent)
    level = "critical" if any(i["sev"] >= 2 for i in fresh) else "warn"
    title = "NAS Safe 异常提醒"
    detail = "；".join(i["title"] for i in fresh)
    res = notify.push_alert(title, detail, level)
    return {"ok": res.get("ok", False), "pushed": len(fresh), "channel": res.get("channel", ""),
            "items": items}


def start_watchdog(interval: int = WATCHDOG_INTERVAL) -> "threading.Thread":
    """服务端看门狗：小助手被关闭（或网页也关着）时，接管微信/邮件提醒。

    资源占用：每 interval 秒读一次指标（走 15s 缓存，多数命中缓存），
    未配置通知通道时直接跳过推送。
    """
    def _loop() -> None:
        time.sleep(20)  # 启动后先让服务预热
        while True:
            try:
                scan_and_push()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(max(30, interval))

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
