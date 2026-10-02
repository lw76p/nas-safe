"""跨品牌多设备总控制台 —— 设备注册与分层聚合（只读探测，绝不改系统状态）。

设计（与 NAS Safe 部署形态一致）：
- 每个 NAS 都运行一个 NAS Safe 代理（容器 / 脚本），暴露同一套 /api 接口。
- 其中一台被指定为「总控制台」（本模块所在实例），它把本机当作 local 设备，
  并把其它 NAS 的访问地址登记为 remote 设备，统一拉取健康快照。
- 远程设备通过 HTTP 调其 /api/system/metrics + /api/system 聚合，离线/超时安全降级。
- 一键迁移（见 migrate.py）：导出本机配置包，导入到任意新设备并按品牌能力降级。

health 等级（与前端四级一致）：0=正常(绿) 1=注意(黄) 2=警告(橙) 3=异常(红)
"""

from __future__ import annotations

import json
import os
import time

import storage  # noqa: E402
import brands as brandmod  # noqa: E402  品牌识别
import smartd  # noqa: E402  硬盘健康
import metrics  # noqa: E402  硬件/卷指标

DEVICES_FILE = "devices.json"
LOCAL_ID = "local"


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def _state_dir() -> str:
    return storage.state_dir()


def load_devices() -> list:
    """返回设备列表（含 local 占位，若未登记则创建）。"""
    d = _state_dir()
    p = os.path.join(d, DEVICES_FILE)
    devs = []
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                devs = json.load(f)
        except (json.JSONDecodeError, OSError):
            devs = []
    if not isinstance(devs, list):
        devs = []
    if not any(x.get("id") == LOCAL_ID for x in devs):
        devs.insert(0, _local_template())
    return devs


def save_devices(devs: list) -> None:
    d = _state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, DEVICES_FILE), "w", encoding="utf-8") as f:
            json.dump(devs, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _local_template() -> dict:
    return {
        "id": LOCAL_ID,
        "name": "本机",
        "group": "本地设备",
        "brand": brandmod.detect_brand(),
        "type": "local",
        "host": "",
        "port": 0,
        "token": "",
        "enabled": True,
        "note": "当前这台运行 NAS Safe 控制台的设备",
    }


# ---------------------------------------------------------------------------
# 单设备健康汇总
# ---------------------------------------------------------------------------

def _smart_summary(smart: dict | None) -> dict:
    s = smart or {}
    if not s.get("available"):
        return {"available": False, "worst": 0, "ok": 0, "warn": 0, "bad": 0,
                "na": 0, "disk_count": 0}
    counts = {"ok": 0, "warn": 0, "bad": 0, "na": 0}
    for d in (s.get("disks") or []):
        h = (d.get("health") or "unknown")
        if h == "fail":
            counts["bad"] += 1
        elif h == "warn":
            counts["warn"] += 1
        elif h in (None, "unknown"):
            counts["na"] += 1
        else:
            counts["ok"] += 1
    return {
        "available": True,
        "worst": int(s.get("worst") or 0),
        "ok": counts["ok"], "warn": counts["warn"], "bad": counts["bad"],
        "na": counts["na"], "disk_count": len(s.get("disks") or []),
    }


def _snapshot_summary(m: dict | None) -> dict:
    vols = (m or {}).get("volumes") or []
    total = len(vols)
    protected = sum(1 for v in vols if v.get("protected"))
    snaps = sum(int(v.get("snapshot_count") or 0) for v in vols)
    return {"total_units": total, "protected_units": protected,
            "unprotected_units": total - protected, "snap_count": snaps}


def _grade_combine(*grades) -> int:
    g = 0
    for x in grades:
        if isinstance(x, int):
            g = max(g, x)
    return g


def collect_local_summary(dev: dict) -> dict:
    """本机健康汇总：直接调用本地模块（最快、最全）。"""
    summary = {
        "id": dev.get("id", LOCAL_ID),
        "name": dev.get("name") or _default_local_name(dev),
        "custom_name": bool(dev.get("custom_name")),
        "group": dev.get("group", "本地设备"),
        "brand": brandmod.detect_brand(),
        "brand_label": "",
        "type": "local",
        "enabled": dev.get("enabled", True),
        "status": "online",
        "host": "", "port": 0,
        "last_seen": time.time(),
        "note": dev.get("note", ""),
        "smart": {"available": False},
        "snapshot": {"total_units": 0, "protected_units": 0,
                     "unprotected_units": 0, "snap_count": 0},
        "guard": {"level": "ok", "label": "正常"},
        "caps": {},
        "health": 0, "health_label": "正常",
    }
    try:
        caps = brandmod.detect_capabilities(summary["brand"])
        summary["caps"] = caps
        summary["brand_label"] = caps.get("brand_label", summary["brand"])

        m = metrics.collect(force=False)
        smart = None
        try:
            smart = smartd.collect(force=False)
        except Exception:  # noqa: BLE001
            smart = None
        summary["smart"] = _smart_summary(smart)
        summary["snapshot"] = _snapshot_summary(m)

        # 防勒索看护等级：依据篡改告警 + 自动快照是否开启
        alerts = []
        try:
            alerts = storage.scan_tamper(include_integrity=True)
        except Exception:  # noqa: BLE001
            alerts = []
        guard_level = "ok"
        guard_label = "受保护"
        if alerts:
            guard_level = "bad"
            guard_label = f"{len(alerts)} 项风险"
        elif summary["snapshot"]["unprotected_units"] > 0:
            guard_level = "warn"
            guard_label = f"{summary['snapshot']['unprotected_units']} 卷未保护"
        summary["guard"] = {"level": guard_level, "label": guard_label}

        # 综合健康等级
        g_smart = 0
        if summary["smart"]["available"]:
            g_smart = 1 if summary["smart"]["warn"] else 0
            if summary["smart"]["bad"]:
                g_smart = 3
        g_snap = 2 if summary["snapshot"]["unprotected_units"] == summary["snapshot"]["total_units"] and summary["snapshot"]["total_units"] > 0 else (1 if summary["snapshot"]["unprotected_units"] > 0 else 0)
        g_guard = {"ok": 0, "warn": 1, "bad": 3}.get(guard_level, 0)
        summary["health"] = _grade_combine(g_smart, g_snap, g_guard)
    except Exception as exc:  # noqa: BLE001  本地采集兜底
        summary["health"] = 1
        summary["note"] = f"本机采集部分失败：{exc}"
    summary["health_label"] = {0: "正常", 1: "注意", 2: "警告", 3: "异常"}.get(summary["health"], "正常")
    return summary


def collect_remote_summary(dev: dict) -> dict:
    """远程设备健康汇总：HTTP 拉取对端 NAS Safe 的接口，超时安全降级。"""
    brand = dev.get("brand") or "generic_linux"
    out = {
        "id": dev.get("id"), "name": dev.get("name", "远程设备"),
        "custom_name": bool(dev.get("custom_name")),
        "group": dev.get("group", "远程设备"), "brand": brand,
        "brand_label": brandmod.BRAND_LABELS.get(brand, brand),
        "type": "remote", "enabled": dev.get("enabled", True),
        "status": "offline", "host": dev.get("host", ""), "port": dev.get("port", 0),
        "last_seen": dev.get("last_seen", 0), "note": dev.get("note", ""),
        "smart": {"available": False}, "snapshot": {"total_units": 0, "protected_units": 0,
                                                   "unprotected_units": 0, "snap_count": 0},
        "guard": {"level": "ok", "label": "未知"},
        "caps": {}, "health": 1, "health_label": "注意",
    }
    host = (dev.get("host") or "").strip()
    port = int(dev.get("port") or 0)
    if not host or not port:
        out["note"] = "缺少访问地址（host/port）"
        return out
    base = f"http{'s' if dev.get('https') else ''}://{host}:{port}"
    token = (dev.get("token") or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        m = _http_get(base + "/api/system/metrics", headers, timeout=5)
        if m.get("ok"):
            data = m.get("metrics", m)
            smart = data.get("smart") or {}
            out["smart"] = _smart_summary(smart)
            out["snapshot"] = _snapshot_summary(data)
            out["status"] = "online"
            out["last_seen"] = time.time()
            # 综合健康
            g_smart = 0
            if out["smart"]["available"]:
                g_smart = 1 if out["smart"]["warn"] else 0
                if out["smart"]["bad"]:
                    g_smart = 3
            tot = out["snapshot"]["total_units"]
            unp = out["snapshot"]["unprotected_units"]
            g_snap = 2 if (tot and unp == tot) else (1 if unp > 0 else 0)
            out["health"] = _grade_combine(g_smart, g_snap)
        else:
            out["note"] = "对端返回异常"
    except Exception as exc:  # noqa: BLE001
        out["status"] = "offline"
        out["note"] = f"无法连接（{type(exc).__name__}）"
        out["health"] = 1
    out["health_label"] = {0: "正常", 1: "注意", 2: "警告", 3: "异常"}.get(out["health"], "注意")
    return out


def _http_get(url: str, headers: dict, timeout: int = 5):
    import urllib.request
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "ignore")
    return json.loads(body)


# ---------------------------------------------------------------------------
# 全量聚合（分层分级）
# ---------------------------------------------------------------------------

def collect_all(force: bool = False) -> dict:
    """汇总所有设备，按 group 分层，返回树 + 总体统计。"""
    devs = load_devices()
    summaries = []
    for dev in devs:
        if not dev.get("enabled", True):
            continue
        if dev.get("type") == "local" or dev.get("id") == LOCAL_ID:
            summaries.append(collect_local_summary(dev))
        else:
            summaries.append(collect_remote_summary(dev))

    # 分层：group -> 设备
    groups: dict[str, list] = {}
    for s in summaries:
        groups.setdefault(s.get("group") or "未分组", []).append(s)

    # 总体统计（用于控制台顶部总览）
    totals = {
        "devices": len(summaries),
        "online": sum(1 for s in summaries if s["status"] == "online"),
        "offline": sum(1 for s in summaries if s["status"] == "offline"),
        "smart_disks": sum(s["smart"].get("disk_count", 0) for s in summaries),
        "smart_bad": sum(s["smart"].get("bad", 0) for s in summaries),
        "smart_warn": sum(s["smart"].get("warn", 0) for s in summaries),
        "snap_count": sum(s["snapshot"].get("snap_count", 0) for s in summaries),
        "protected_units": sum(s["snapshot"].get("protected_units", 0) for s in summaries),
        "unprotected_units": sum(s["snapshot"].get("unprotected_units", 0) for s in summaries),
        "health_bad": sum(1 for s in summaries if s["health"] >= 3),
        "health_warn": sum(1 for s in summaries if s["health"] in (1, 2)),
    }
    return {"devices": summaries, "groups": groups, "totals": totals}


# ---------------------------------------------------------------------------
# 增删改
# ---------------------------------------------------------------------------

def add_device(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise storage.StorageError("参数格式错误")
    host = (payload.get("host") or "").strip()
    port = int(payload.get("port") or 0)
    if not host or not port:
        raise storage.StorageError("远程设备需要填写访问地址（host 和 port）")
    devs = load_devices()
    # 同一台设备（同地址同端口）重复添加时，改为更新，避免控制台出现两台一样的
    for d in devs:
        if (d.get("host") or "").strip() == host and int(d.get("port") or 0) == port:
            d["name"] = (payload.get("name") or d.get("name") or f"{host}:{port}").strip()
            d["custom_name"] = bool(payload.get("name"))
            if payload.get("group"):
                d["group"] = str(payload.get("group")).strip()
            if payload.get("brand"):
                d["brand"] = str(payload.get("brand")).strip()
            if payload.get("token"):
                d["token"] = str(payload.get("token")).strip()
            d["https"] = bool(payload.get("https"))
            d["enabled"] = True
            d["net_kind"] = (payload.get("net_kind") or d.get("net_kind") or "").strip()
            save_devices(devs)
            return {"ok": True, "id": d.get("id"), "updated": True}

    dev_id = f"dev-{int(time.time())}"
    devs.append({
        "id": dev_id,
        "name": (payload.get("name") or f"{host}:{port}").strip(),
        "group": (payload.get("group") or "远程设备").strip(),
        "brand": (payload.get("brand") or "generic_linux").strip(),
        "type": "remote",
        "host": host,
        "port": port,
        "https": bool(payload.get("https")),
        "token": (payload.get("token") or "").strip(),
        "enabled": True,
        "note": "",
        "net_kind": (payload.get("net_kind") or "").strip(),
    })
    save_devices(devs)
    return {"ok": True, "id": dev_id}


def _default_local_name(dev: dict | None = None) -> str:
    """本机默认显示名：品牌 + 本机（用户改过名就不走这里）。"""
    try:
        label = brandmod.detect_capabilities(brandmod.detect_brand()).get("brand_label", "")
    except Exception:  # noqa: BLE001
        label = ""
    return (label or "本机").strip()


def rename_device(dev_id: str, name: str) -> dict:
    """给任意设备（含本机）改名，方便在控制台和总控动画里一眼认出来。"""
    name = (name or "").strip()
    if not name:
        raise storage.StorageError("名字不能为空")
    if len(name) > 24:
        raise storage.StorageError("名字最多 24 个字")
    devs = load_devices()
    hit = None
    for d in devs:
        if d.get("id") == dev_id:
            hit = d
            break
    if hit is None:
        raise storage.StorageError("设备不存在")
    hit["name"] = name
    hit["custom_name"] = True
    save_devices(devs)
    return {"ok": True, "id": dev_id, "name": name}


def remove_device(dev_id: str) -> dict:
    if dev_id == LOCAL_ID:
        raise storage.StorageError("本机设备不可删除")
    devs = load_devices()
    new = [d for d in devs if d.get("id") != dev_id]
    if len(new) == len(devs):
        raise storage.StorageError("设备不存在")
    save_devices(new)
    return {"ok": True}
