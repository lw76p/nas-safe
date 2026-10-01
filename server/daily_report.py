"""
NAS Safe — 每日健康日报（免费留存钩子，复用现有通知链路）

目标：每天定时把一份「快照保护 / 告警 / 可释放空间 / 硬盘健康」卡片推到用户
已配置的通道（微信服务号 / 邮件 / 群机器人 / Bark 等），让用户天天看到产品在
干活 —— 这是续费感最强的免费功能（见 docs/BUSINESS.md：日报免费做获客钩子）。

数据源（全部复用既有模块，零新增权限）：
  · 快照保护状态  → storage.list_all_volumes / list_all_snapshots
  · 实时告警      → anomalies.collect_anomalies
  · 可释放空间    → junk.load_report（用户先扫过才有数字，否则只提示「未扫描」）
  · 硬盘健康/容量 → metrics.collect（温度、卷用量、存满趋势）

推送：复用 notify.dispatch（events 形态），自动走用户已启用的所有通道，不配通道则空操作。
调度：后台守护线程，按用户设定的 hour:minute 每天跑一次（默认 08:00）。
"""

from __future__ import annotations

import json
import os
import threading
import time

import storage
import anomalies
import metrics
import junk
import notify


# ---------------------------------------------------------------------------
# 配置 / 上次报告持久化（均落 state 目录，绝不进仓库）
# ---------------------------------------------------------------------------

def _path(name: str) -> str:
    return os.path.join(storage.state_dir(), name)


def config_path() -> str:
    return _path("daily_report.json")


def load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    cfg.setdefault("enabled", False)
    cfg.setdefault("hour", 8)
    cfg.setdefault("minute", 0)
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(storage.state_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_last() -> dict | None:
    try:
        with open(_path("daily_report_last.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_last(report: dict) -> None:
    try:
        with open(_path("daily_report_last.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 报告聚合
# ---------------------------------------------------------------------------

def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def build_report() -> dict:
    """聚合四大维度，返回结构化报告。任一子系统故障不影响整体。"""
    # —— 快照保护 ——
    snap_summary = _safe(_snapshot_summary, None)
    # —— 告警 ——
    alerts = _safe(lambda: anomalies.collect_anomalies(), []) or []
    crit = sum(1 for a in alerts if a.get("sev") == 2)
    warn = sum(1 for a in alerts if a.get("sev") == 1)
    # —— 可释放空间 ——
    freeable = _safe(_freeable_summary, None)
    # —— 硬盘健康 / 容量 ——
    disk = _safe(_disk_summary, None)

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshots": snap_summary,
        "alerts": {"total": len(alerts), "critical": crit, "warn": warn,
                   "items": [{"title": a.get("title"), "sev": a.get("sev")} for a in alerts]},
        "freeable": freeable,
        "disk": disk,
        "summary": "",
    }
    report["summary"] = _summary_line(report)
    return report


def _snapshot_summary() -> dict:
    vols = storage.list_all_volumes()
    total = 0
    protected = 0
    latest = None
    for vol in vols:
        try:
            snaps = storage.list_all_snapshots(vol)
        except Exception:  # noqa: BLE001
            snaps = []
        total += len(snaps)
        if snaps:
            protected += 1
            dated = [s for s in snaps if getattr(s, "created_at", None)]
            lt = sorted(dated, key=lambda s: s.created_at)[-1].created_at if dated else snaps[-1].name
            if latest is None or lt > latest:
                latest = lt
    return {"volume_count": len(vols), "snapshot_count": total,
            "protected_volumes": protected, "latest_snapshot": latest}


def _freeable_summary() -> dict:
    rpt = junk.load_report()
    if not rpt or not rpt.get("categories"):
        return {"scanned": False, "total_bytes": 0, "categories": []}
    cats = []
    total = 0
    for c in rpt["categories"]:
        if c.get("key") == "docker":
            continue  # Docker 可回收空间不计入「可立即清理」字节数
        b = c.get("total_bytes", 0)
        n = len(c.get("items", []))
        if b <= 0 and n == 0:
            continue
        total += b
        cats.append({"key": c["key"], "name": c["name"], "bytes": b, "items": n})
    return {"scanned": True, "total_bytes": total, "categories": cats,
            "scanned_at": rpt.get("scanned_at")}


def _disk_summary() -> dict:
    m = metrics.collect(force=True)
    vols = m.get("volumes") or []
    tight = [{"mount": v.get("mount"), "percent": v.get("percent")}
             for v in vols if (v.get("percent") or 0) >= 75]
    trends = [t for t in (m.get("trends") or []) if t.get("days_to_full")]
    disks = m.get("disks") or []
    hot = [{"name": d.get("name"), "temp_c": d.get("temp_c")}
           for d in disks if d.get("temp_c") is not None and d["temp_c"] >= 50]
    return {
        "hostname": m.get("hostname"),
        "cpu_temp": (m.get("cpu") or {}).get("temp_c"),
        "volume_count": len(vols),
        "tight_volumes": tight,
        "days_to_full": trends,
        "hot_disks": hot,
    }


def _summary_line(r: dict) -> str:
    parts = []
    snap = r.get("snapshots")
    if snap:
        if snap.get("protected_volumes"):
            parts.append(f"{snap['protected_volumes']} 个卷已上锁保护、共 {snap['snapshot_count']} 张快照")
        else:
            parts.append("暂未对任何卷创建快照（建议立即拍一张）")
    al = r.get("alerts") or {}
    if al.get("total"):
        parts.append(f"{al['critical']} 项严重、{al['warn']} 项需注意的告警")
    else:
        parts.append("无新增告警")
    fb = r.get("freeable")
    if fb and fb.get("scanned"):
        if fb.get("total_bytes"):
            parts.append(f"可清理释放 {_fmt_bytes(fb['total_bytes'])}")
        else:
            parts.append("暂无可清理空间")
    else:
        parts.append("垃圾未扫描（去「磁盘清理」扫一次可出数字）")
    return "；".join(parts) + "。"


def _fmt_bytes(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def format_text(report: dict) -> str:
    """把结构化报告压成一条适合推送的纯文本卡片。"""
    lines = ["📋 NAS Safe 每日健康日报", f"⏰ {report.get('generated_at','')}", ""]
    snap = report.get("snapshots") or {}
    lines.append("【快照保护】")
    if snap:
        lines.append(f"  · 存储卷 {snap.get('volume_count')} 个，已保护 {snap.get('protected_volumes')} 个")
        lines.append(f"  · 快照总数 {snap.get('snapshot_count')} 张，最新：{snap.get('latest_snapshot') or '无'}")
    else:
        lines.append("  · 数据暂不可用")

    al = report.get("alerts") or {}
    lines.append("【告警】")
    if al.get("total"):
        lines.append(f"  · 共 {al['total']} 项（严重 {al['critical']} / 注意 {al['warn']}）")
        for it in (al.get("items") or [])[:5]:
            icon = "🔴" if it.get("sev") == 2 else "🟠"
            lines.append(f"    {icon} {it.get('title')}")
    else:
        lines.append("  · 无新增告警 ✓")

    fb = report.get("freeable")
    lines.append("【可释放空间】")
    if fb and fb.get("scanned"):
        if fb.get("total_bytes"):
            lines.append(f"  · 可清理释放 {_fmt_bytes(fb['total_bytes'])}")
            for c in fb.get("categories") or []:
                lines.append(f"    - {c['name']}：{c['items']} 项 / {_fmt_bytes(c['bytes'])}")
        else:
            lines.append("  · 暂无可清理空间")
    else:
        lines.append("  · 尚未扫描（建议去「磁盘清理」扫一次）")

    dk = report.get("disk") or {}
    lines.append("【硬盘健康】")
    if dk:
        if dk.get("cpu_temp") is not None:
            lines.append(f"  · CPU 温度 {dk['cpu_temp']}°C")
        if dk.get("hot_disks"):
            for h in dk["hot_disks"][:4]:
                lines.append(f"  · 磁盘 {h['name']} 温度 {h['temp_c']}°C")
        if dk.get("tight_volumes"):
            for v in dk["tight_volumes"][:4]:
                lines.append(f"  · 卷 {v['mount']} 已用 {v['percent']}%")
        if dk.get("days_to_full"):
            for t in dk["days_to_full"][:3]:
                lines.append(f"  · 卷 {t['mount']} 约 {t['days_to_full']} 天后存满")
        if not (dk.get("cpu_temp") is not None or dk.get("hot_disks")
                or dk.get("tight_volumes") or dk.get("days_to_full")):
            lines.append("  · 各项指标正常 ✓")
    else:
        lines.append("  · 数据暂不可用")

    lines.append("")
    lines.append("一句话：" + (report.get("summary") or ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 发送（复用 notify 通道，零新增权限）
# ---------------------------------------------------------------------------

def send_daily() -> dict:
    """生成报告 → 推送到已启用通道 → 存最近一次报告。返回结果。"""
    report = build_report()
    text = format_text(report)
    cfg = load_config()
    sent = {"enabled": cfg.get("enabled", False), "sent": [], "skipped": "通知未启用" if not cfg.get("enabled") else ""}
    if cfg.get("enabled"):
        # 复用 notify.dispatch：把日报作为「事件」推送，自动走所有已启用通道
        sent = notify.dispatch(
            alerts=[],
            events=[{"title": "每日健康日报", "detail": text}],
        )
    save_last({"report": report, "text": text, "sent_at": report.get("generated_at"), "dispatch": sent})
    return {"report": report, "dispatch": sent}


# ---------------------------------------------------------------------------
# 定时调度（后台守护线程）
# ---------------------------------------------------------------------------

def start_scheduler() -> "threading.Thread":
    """后台守护线程：每天在用户设定的 hour:minute 推送日报。"""
    def _next_target(cfg: dict) -> float:
        lt = time.localtime()
        y, mo, d = lt.tm_year, lt.tm_mon, lt.tm_mday
        # 今天的目标时刻；已过则顺延到明天
        t = time.mktime((y, mo, d, cfg.get("hour", 8), cfg.get("minute", 0), 0, 0, 0, -1))
        if t <= time.time() + 30:
            t += 86400
        return t

    def _loop() -> None:
        while True:
            try:
                cfg = load_config()
                if cfg.get("enabled"):
                    wait = _next_target(cfg) - time.time()
                    if wait > 0:
                        # 最多睡 1 小时再检查（用户可能改了时间/开关），到点前不发送
                        time.sleep(min(wait, 3600))
                        continue
                    try:
                        send_daily()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            time.sleep(300)  # 兜底轮询间隔

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
