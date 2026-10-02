"""三期：应急响应自动化（专业版专属）。

把已有的「快照 / 篡改检测 / 卷枚举」能力组合成一键应急动作：
  - report()        ：生成处置报告（健康 + 告警 + 快照概况 + 建议）
  - snapshot_now()  ：立刻给指定卷打一份 vital 锁快照（应急留底）
  - isolate()       ：把可疑文件/目录移入隔离区（软删除，可恢复）
  - clean_old()     ：清理某卷超过保留份数的最旧快照

所有动作都走 storage 已有原语，失败给出可读错误；不破坏数据（快照只读、隔离可恢复）。
"""
from __future__ import annotations

import os
import shutil
import time

import storage


def _state_dir() -> str:
    try:
        return storage.state_dir()
    except Exception:  # noqa: BLE001
        return os.path.join(os.getcwd(), "state")


def _quarantine_root() -> str:
    p = os.path.join(_state_dir(), "emergency_quarantine")
    os.makedirs(p, exist_ok=True)
    return p


def _find_volume(ident: str):
    """按 id 或挂载点匹配一个 Volume 对象。"""
    vols = storage.list_all_volumes()
    for v in vols:
        if ident in (getattr(v, "id", ""), getattr(v, "mountpoint", ""), getattr(v, "name", "")):
            return v
    # 退化为挂载点前缀匹配
    for v in vols:
        mp = getattr(v, "mountpoint", "")
        if mp and ident.startswith(mp):
            return v
    return None


def report() -> dict:
    """汇总当前健康 / 告警 / 快照，给出应急处置建议。"""
    sections: list = []
    health = {}
    try:
        import metrics
        m = metrics.collect()
        cpu = (m.get("cpu") or {}).get("percent")
        mem = (m.get("mem") or {}).get("percent")
        health = {"cpu": cpu, "mem": mem,
                  "volumes": [(v.get("mount"), v.get("percent")) for v in (m.get("volumes") or [])[:8]]}
        sections.append("【系统】CPU %s%%，内存 %s%%" % (cpu, mem))
    except Exception as exc:  # noqa: BLE001
        sections.append("【系统】采集失败：%s" % exc)

    alerts = []
    try:
        alerts = storage.scan_tamper()
        if alerts:
            sections.append("【篡改告警】共 %d 条：%s" % (
                len(alerts), "；".join("[%s]%s" % (a.get("level"), a.get("type")) for a in alerts[:8])))
        else:
            sections.append("【篡改告警】无")
    except Exception as exc:  # noqa: BLE001
        sections.append("【篡改告警】检测失败：%s" % exc)

    snap_lines = []
    total = 0
    try:
        for v in storage.list_all_volumes():
            try:
                snaps = storage.list_all_snapshots(v)
            except Exception:  # noqa: BLE001
                snaps = []
            total += len(snaps)
            if snaps:
                snap_lines.append("%s：%d 份" % (getattr(v, "mountpoint", "?"), len(snaps)))
        sections.append("【快照】共 %d 份；%s" % (total, "；".join(snap_lines) or "暂无"))
    except Exception as exc:  # noqa: BLE001
        sections.append("【快照】枚举失败：%s" % exc)

    advice = (
        "建议：① 若发现篡改告警，立即对受影响卷打应急快照（snapshot_now）留底；"
        "② 把可疑文件隔离（isolate）而非直接删除，便于回溯；"
        "③ 快照超过保留份数时用 clean_old 清理最旧份数以释放空间。")
    sections.append("【处置建议】" + advice)
    return {"text": "\n\n".join(sections), "health": health,
            "alerts": len(alerts), "snapshots": total}


def snapshot_now(volume_ident: str, label: str = "") -> dict:
    v = _find_volume(volume_ident)
    if not v:
        raise ValueError("找不到卷：%s（请用列表中返回的 id 或挂载点）" % volume_ident)
    name = "emergency-%s-%d" % ((label or "manual"), int(time.time()))
    try:
        snap = storage.create_snapshot(v, name, vital=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("打快照失败：%s" % exc)
    return {"ok": True, "volume": getattr(v, "mountpoint", ""),
            "snapshot": getattr(snap, "name", name),
            "path": getattr(snap, "path", "")}


def isolate(path: str) -> dict:
    if not path or not os.path.exists(path):
        raise ValueError("路径不存在：%s" % path)
    root = _quarantine_root()
    batch = "q-%d" % int(time.time())
    dest_parent = os.path.join(root, batch)
    os.makedirs(dest_parent, exist_ok=True)
    dest = os.path.join(dest_parent, os.path.basename(path.rstrip("/\\")))
    if os.path.exists(dest):
        raise ValueError("隔离目标已存在：%s" % dest)
    try:
        shutil.move(path, dest)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("隔离移动失败：%s" % exc)
    return {"ok": True, "from": path, "to": dest, "batch": batch,
            "note": "已移入隔离区（可恢复），不是永久删除"}


def clean_old(volume_ident: str, keep: int = 10) -> dict:
    v = _find_volume(volume_ident)
    if not v:
        raise ValueError("找不到卷：%s" % volume_ident)
    try:
        snaps = storage.list_all_snapshots(v)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("枚举快照失败：%s" % exc)
    snaps.sort(key=lambda s: getattr(s, "created", 0) or 0)
    remove = snaps[:-keep] if keep > 0 else snaps
    removed = 0
    errors = []
    for s in remove:
        try:
            storage.delete_snapshot(s)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (getattr(s, "name", "?"), exc))
    return {"ok": True, "volume": getattr(v, "mountpoint", ""),
            "kept": max(0, len(snaps) - len(remove)), "removed": removed,
            "errors": errors}
