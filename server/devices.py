"""跨品牌多设备总控制台 —— 设备注册与分层聚合（只读探测，绝不改系统状态）。

设计（与 TS Safe 部署形态一致）：
- 每个 NAS 都运行一个 TS Safe 代理（容器 / 脚本），暴露同一套 /api 接口。
- 其中一台被指定为「总控制台」（本模块所在实例），它把本机当作 local 设备，
  并把其它 NAS 的访问地址登记为 remote 设备，统一拉取健康快照。
- 远程设备通过 HTTP 调其 /api/system/metrics + /api/system 聚合，离线/超时安全降级。
- 一键迁移（见 migrate.py）：导出本机配置包，导入到任意新设备并按品牌能力降级。

health 等级（与前端四级一致）：0=正常(绿) 1=注意(黄) 2=警告(橙) 3=异常(红)
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

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
        "note": "当前这台运行 TS Safe 控制台的设备",
        "agent": {"status": "installed", "label": "监控中心"},
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
        "agent": {"status": "installed", "label": "监控中心"},
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
    """远程设备健康汇总：HTTP 拉取对端 TS Safe 的接口，超时安全降级。"""
    brand = dev.get("brand") or "generic_linux"
    out = {
        "id": dev.get("id"), "name": dev.get("name", "远程设备"),
        "custom_name": bool(dev.get("custom_name")),
        "group": dev.get("group", "远程设备"), "brand": brand,
        "brand_label": dev.get("brand_label") or brandmod.BRAND_LABELS.get(brand, brand),
        "type": "remote", "enabled": dev.get("enabled", True),
        "full_server": bool(dev.get("full_server")),
        "status": "offline", "host": dev.get("host", ""), "port": dev.get("port", 0),
        "last_seen": dev.get("last_seen", 0), "note": dev.get("note", ""),
        "agent": dev.get("agent") or {},
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
        # 探测超时 2.5s：局域网设备毫秒级返回，离线设备快速失败；
        # 避免单台离线把控制台首屏拖到 N×5s（原 5s，实测冷启动 ~6s -> ~2.5s）
        m = _http_get(base + "/api/system/metrics", headers, timeout=2.5)
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
    # 服务端探测失败，但轻量代理近期有真实心跳：视为在线（与设置页 agent 判定一致），
    # 避免「设置页在线、联机设备/动画页离线」这种两页不一致的困惑。
    if out["status"] != "online" and dev.get("type") != "local" and _online_status(dev) == "online":
        out["status"] = "online"
        if not out.get("note") or str(out.get("note", "")).startswith(("无法连接", "对端返回", "探测失败")):
            out["note"] = "仅代理在线（对端 TS Safe 服务未响应，仅确认设备存活）"
        out["health"] = min(int(out.get("health") or 1), 1)
    return out


def _http_get(url: str, headers: dict, timeout: int = 5):
    import urllib.request
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "ignore")
    return json.loads(body)


# ---------------------------------------------------------------------------
# 联机设备的快照预览（快照页切到某台联机设备时，由本控制台代理拉取）
# ---------------------------------------------------------------------------

def remote_snapshots(dev_id: str, volume: str = "") -> dict:
    """读取某台设备的快照列表，供快照页做「联机设备快照预览」。

    远程设备走 HTTP 代理对端的公开 GET 接口（对端 GET 数据接口默认免登录），
    只读、不做任何写操作，连不上就安全降级并说明原因。
    """
    devs = load_devices()
    dev = next((d for d in devs if d.get("id") == dev_id), None)
    if dev is None:
        return {"ok": False, "error": "找不到这台设备"}
    if dev.get("type") == "local" or dev.get("id") == LOCAL_ID:
        return {"ok": False, "local": True, "error": "本机快照请走 /api/snapshots"}

    host = (dev.get("host") or "").strip()
    port = int(dev.get("port") or 0)
    if not host or not port:
        return {"ok": False, "error": "这台设备还没登记访问地址，连不上"}
    base = f"http{'s' if dev.get('https') else ''}://{host}:{port}"
    token = (dev.get("token") or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        v = _http_get(base + "/api/volumes", headers, timeout=8)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"连不上这台设备（{type(exc).__name__}），可能已关机或地址变了"}
    vols = v.get("volumes") or []
    if not volume and vols:
        volume = str(vols[0].get("mountpoint") or vols[0].get("id") or "")

    snaps: list = []
    err = ""
    if volume:
        try:
            s = _http_get(base + "/api/snapshots?volume=" + quote(volume, safe=""),
                          headers, timeout=15)
            snaps = s.get("snapshots") or []
        except Exception as exc:  # noqa: BLE001
            err = f"这台设备的卷列表拿到了，但快照没读出来（{type(exc).__name__}）"

    return {
        "ok": True,
        "device": {"id": dev.get("id"), "name": dev.get("name") or "远程设备",
                   "brand_label": dev.get("brand_label") or "",
                   "host": host, "port": port},
        "volumes": vols,
        "snapshots": snaps,
        "volume": volume,
        "readonly": True,          # 联机设备快照在这里只看不动，避免跨机破坏性操作
        "warning": err,
    }


# ---------------------------------------------------------------------------
# 全量聚合（分层分级）
# ---------------------------------------------------------------------------

# 轻量缓存：重复进入控制台时不重复探测（远程设备串行探测很慢）。
# 仅缓存非 force 的结果，TTL 内直接返回，首次/强刷仍实时拉取。
_COLLECT_LOCK = threading.Lock()
_COLLECT_CACHE = {"data": None, "ts": 0.0}
_COLLECT_TTL = 8.0


def collect_all(force: bool = False) -> dict:
    """汇总所有设备，按 group 分层，返回树 + 总体统计。

    性能优化：本机直接采集；远程设备用线程池并行探测，避免逐台串行
    超时（每台最多 2.5s）把控制台拖到 N×2.5s。配合 8s 缓存，重复进入秒开。
    """
    if not force:
        with _COLLECT_LOCK:
            if _COLLECT_CACHE["data"] is not None and (time.time() - _COLLECT_CACHE["ts"]) < _COLLECT_TTL:
                return _COLLECT_CACHE["data"]

    devs = load_devices()
    local_summaries = []
    remote_devs = []
    for dev in devs:
        if not dev.get("enabled", True):
            continue
        if dev.get("type") == "local" or dev.get("id") == LOCAL_ID:
            local_summaries.append(collect_local_summary(dev))
        else:
            remote_devs.append(dev)

    # 远程设备并行探测
    remote_summaries = []
    if remote_devs:
        with ThreadPoolExecutor(max_workers=min(16, len(remote_devs))) as ex:
            futs = {ex.submit(collect_remote_summary, d): d for d in remote_devs}
            for fut in as_completed(futs):
                try:
                    remote_summaries.append(fut.result())
                except Exception:  # noqa: BLE001 兜底：单台异常不影响整体
                    d = futs[fut]
                    remote_summaries.append({
                        "id": d.get("id"), "name": d.get("name", "远程设备"), "type": "remote",
                        "status": "offline", "health": 1, "health_label": "注意",
                        "agent": d.get("agent") or {}, "smart": {"available": False},
                        "snapshot": {"total_units": 0, "protected_units": 0,
                                     "unprotected_units": 0, "snap_count": 0},
                        "guard": {"level": "ok", "label": "未知"},
                        "caps": {}, "brand": d.get("brand", "generic_linux"),
                        "brand_label": d.get("brand_label") or brandmod.BRAND_LABELS.get(d.get("brand") or "generic_linux", d.get("brand") or "generic_linux"),
                        "group": d.get("group", "远程设备"), "host": d.get("host", ""),
                        "port": d.get("port", 0), "enabled": True,
                        "last_seen": d.get("last_seen", 0), "note": "探测失败",
                    })
    summaries = local_summaries + remote_summaries

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
    result = {"devices": summaries, "groups": groups, "totals": totals}
    # 写入缓存（仅非强刷路径使用）
    with _COLLECT_LOCK:
        _COLLECT_CACHE["data"] = result
        _COLLECT_CACHE["ts"] = time.time()
    return result


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
            # 扫描到真·TS Safe 服务端（不是只装了轻量代理）时标记为完整服务端，
            # 前端据此隐藏「安装完整版」入口（已经是了，不必再推）。
            if "full_server" in payload:
                d["full_server"] = bool(payload.get("full_server"))
            d["net_kind"] = (payload.get("net_kind") or d.get("net_kind") or "").strip()
            d["brand_label"] = (payload.get("brand_label") or d.get("brand_label") or "").strip()
            if payload.get("agent_request") or payload.get("install_agent"):
                d["agent"] = _agent_pending(d.get("agent"))
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
        "brand_label": (payload.get("brand_label") or "").strip(),
        "agent": _agent_pending(None) if (payload.get("agent_request") or payload.get("install_agent")) else {},
        # 扫描探测到这本身就是一台 TS Safe 服务端（nassafe=True）时为 True；
        # 轻量代理端点 / 仅监控设备为 False（前端会推「安装完整版」）。
        "full_server": bool(payload.get("full_server")),
    })
    save_devices(devs)
    return {"ok": True, "id": dev_id}


# ---------------------------------------------------------------------------
# 轻量代理（v0：回连注册 + 心跳，后续扩展快照/迁移指令通道）
# ---------------------------------------------------------------------------

def _agent_pending(cur: dict | None) -> dict:
    """生成/复用「待安装」状态，令牌不变避免重复安装命令失效。"""
    ag = dict(cur or {})
    if ag.get("status") != "installed":
        ag["status"] = "pending"
        if not ag.get("token"):
            ag["token"] = secrets.token_hex(8)
        ag["requested_at"] = time.time()
    return ag


def set_agent_request(dev_id: str) -> dict:
    """中控里手动补装：把设备标记为待装并下发专属安装令牌。"""
    devs = load_devices()
    hit = None
    for d in devs:
        if d.get("id") == dev_id:
            hit = d
            break
    if hit is None:
        raise storage.StorageError("设备不存在")
    hit["agent"] = _agent_pending(hit.get("agent"))
    save_devices(devs)
    return {"ok": True, "id": dev_id, "agent": hit["agent"]}


def _find_by_agent_token(devs: list, token: str):
    token = (token or "").strip()
    if not token:
        return None
    for d in devs:
        if (d.get("agent") or {}).get("token") == token:
            return d
    return None


def agent_register(token: str, info: dict | None = None) -> dict:
    """被控端运行安装脚本后回连注册：标记为已安装。"""
    devs = load_devices()
    hit = _find_by_agent_token(devs, token)
    if hit is None:
        raise storage.StorageError("安装令牌无效")
    ag = hit.get("agent") or {}
    ag["status"] = "installed"
    ag["installed_at"] = time.time()
    if info:
        ag["info"] = info
        # 代理上报的 OS 就是设备真实身份：品牌未知时按 OS 修正，跨平台识别更准。
        # 只在未知（generic_linux 等）时覆盖，不碰扫描时已认出的 NAS 品牌。
        if (hit.get("brand") or "generic_linux") in ("", "generic_linux"):
            _os = str(info.get("os") or "").lower()
            if "windows" in _os:
                hit["brand"] = "windows"
                hit["brand_label"] = "Windows 电脑"
            elif "darwin" in _os or "mac os" in _os or "macos" in _os:
                hit["brand"] = "macos"
                hit["brand_label"] = "Mac 电脑"
            elif "linux" in _os or "nas" in _os:
                hit["brand"] = "linux"
                hit["brand_label"] = "Linux 电脑"
    hit["agent"] = ag
    hit["last_seen"] = time.time()
    save_devices(devs)
    return {"ok": True, "id": hit.get("id"), "name": hit.get("name")}


def agent_heartbeat(token: str) -> dict:
    """被控端每分钟心跳：更新在线时间。"""
    devs = load_devices()
    hit = _find_by_agent_token(devs, token)
    if hit is None:
        return {"ok": False, "error": "安装令牌无效"}
    ag = hit.get("agent") or {}
    ag["last_checkin"] = time.time()
    if ag.get("status") != "installed":
        ag["status"] = "installed"
        ag.setdefault("installed_at", time.time())
    hit["agent"] = ag
    hit["last_seen"] = time.time()
    save_devices(devs)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 完整版「自动报到」：装完的完整版主动向来源总控台登记 + 持续心跳
#
# 为什么要这套：总控台可能在公网（比如 47.108.213.178），而被管设备在内网
# （192.168.8.x）。总控台无法反向连接内网设备，所以「在线状态」不能靠总控台
# 去拉探测；必须由设备主动登记 + 主动心跳。
#
# 流程：中控点「装完整版」时签发一次性票据 → 票据随安装包下发 → 新机器启动后
# 用票据换一张长期心跳 token → 每分钟 POST /api/agent/heartbeat。
# ---------------------------------------------------------------------------

FULL_TICKET_FILE = "full_ticket.json"
FULL_TICKET_TTL = 1800  # 30 分钟内有效


def _full_ticket_path() -> str:
    return os.path.join(_state_dir(), FULL_TICKET_FILE)


def _load_full_tickets() -> dict:
    try:
        with open(_full_ticket_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_full_tickets(data: dict) -> None:
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        with open(_full_ticket_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def purge_full_tickets() -> int:
    """清掉过期票据，避免 state 文件无限增长。返回清理条数。"""
    data = _load_full_tickets()
    now = time.time()
    alive = {k: v for k, v in data.items() if float((v or {}).get("expires") or 0) > now}
    if len(alive) != len(data):
        _save_full_tickets(alive)
    return len(data) - len(alive)


def issue_full_ticket(source: str, name: str = "") -> dict:
    """签发一次性「完整版报到票据」（仅管理员可申请），随安装包一起下发。"""
    purge_full_tickets()
    data = _load_full_tickets()
    tk = secrets.token_hex(8)
    exp = time.time() + FULL_TICKET_TTL
    src = (source or "").strip().rstrip("/")
    if src and "://" not in src:
        src = "http://" + src  # 只给了 host（Host 头/location.host），补上协议
    data[tk] = {
        "source": src,
        "name": (name or "").strip()[:64],
        "created": time.time(),
        "expires": exp,
        "used": False,
    }
    _save_full_tickets(data)
    return {"ok": True, "ticket": tk, "source": data[tk]["source"], "expires": exp}


def full_ticket_source(ticket: str) -> str:
    """按票据取已规范化的来源总控台地址（空串表示票据无效/不存在）。"""
    tk = (ticket or "").strip()
    if not tk:
        return ""
    rec = _load_full_tickets().get(tk)
    if not rec or rec.get("used"):
        return ""
    if time.time() > float(rec.get("expires") or 0):
        return ""
    return str(rec.get("source") or "")


def brand_from_os(os_name: str):
    """按上报的系统名推断品牌标签。"""
    o = (os_name or "").lower()
    if "windows" in o or o.startswith("win") or "win32" in o or "nt " in o:
        return "windows", "Windows 电脑"
    if "darwin" in o or "mac os" in o or "macos" in o:
        return "macos", "Mac 电脑"
    if "nas" in o:
        return "nas", "NAS"
    if "linux" in o:
        return "linux", "Linux 电脑"
    return "generic_linux", "通用设备"


def auto_claim(payload: dict) -> dict:
    """装好的完整版用它自带的票据向来源总控台报到，登记为可在线的完整服务端。

    幂等：同一 machine_id 重复报到只会更新，不会重复添加设备。
    """
    if not isinstance(payload, dict):
        raise storage.StorageError("参数格式错误")
    ticket = str(payload.get("ticket") or "").strip()
    data = _load_full_tickets()
    rec = data.get(ticket) if ticket else None
    if not rec or rec.get("used") or time.time() > float(rec.get("expires") or 0):
        raise storage.StorageError("报到票据无效或已过期：请在总控台重新点一次「装完整版」再下载")
    machine = str(payload.get("machine_id") or "").strip()[:80]
    hostname = str(payload.get("hostname") or "").strip()[:64] or "未知主机"
    os_name = str(payload.get("os") or "").strip()[:80]
    host = str(payload.get("host") or "").strip()[:64]
    port = int(payload.get("port") or 8848)
    brand, brand_label = brand_from_os(os_name)
    now = time.time()

    # 票据一次性核销
    rec = dict(rec)
    rec["used"] = True
    rec["used_at"] = now
    rec["machine_id"] = machine
    data[ticket] = rec
    _save_full_tickets(data)

    devs = load_devices()
    hit = None
    for d in devs:
        if machine and (d.get("agent") or {}).get("machine_id") == machine:
            hit = d
            break

    agent_token = secrets.token_hex(8)
    info = {"os": os_name, "hostname": hostname, "machine_id": machine,
            "port": port, "version": str(payload.get("version") or "")[:32]}

    if hit is None:
        if str(rec.get("name") or "").strip():
            hostname = str(rec["name"]).strip()[:64]
        dev_id = f"dev-{int(now)}"
        ag = {"status": "installed", "token": agent_token, "last_checkin": now,
              "installed_at": now, "machine_id": machine, "info": info,
              "via": "full_bundle"}
        devs.append({
            "id": dev_id,
            "name": hostname,
            "group": "联网设备",
            "brand": brand,
            "brand_label": brand_label,
            "type": "remote",
            "host": host,
            "port": port,
            "https": False,
            "token": "",
            "enabled": True,
            "note": "由完整版安装包自动报到",
            "full_server": True,
            "agent": ag,
        })
    else:
        hit["name"] = hostname or hit.get("name")
        hit["full_server"] = True
        hit["brand"] = hit.get("brand") or brand
        hit["brand_label"] = hit.get("brand_label") or brand_label
        if not hit.get("host"):
            hit["host"] = host
        if not hit.get("port"):
            hit["port"] = port
        ag = dict(hit.get("agent") or {})
        ag.update({"status": "installed", "token": agent_token, "last_checkin": now,
                   "machine_id": machine, "info": info, "via": "full_bundle"})
        ag.setdefault("installed_at", now)
        hit["agent"] = ag
        dev_id = hit.get("id")

    save_devices(devs)
    return {"ok": True, "token": agent_token, "id": dev_id, "name": hostname,
            "center": rec.get("source", ""), "brand": brand, "brand_label": brand_label}


AGENT_INSTALL_TEMPLATE = """#!/bin/sh
# TS Safe 轻量代理（v0）：只做「回连注册 + 每分钟心跳」，只读，不改系统配置
CENTER="__CENTER__"
TOKEN="__TOKEN__"
echo "{\\"token\\":\\"$TOKEN\\"}" > /tmp/.nassafe_hb.json 2>/dev/null || exit 1
HB="curl -sS -X POST $CENTER/api/agent/heartbeat -H Content-Type:application/json -d @/tmp/.nassafe_hb.json"
# 注册：告诉中控这台机器装好了
curl -sS -X POST "$CENTER/api/agent/register" -H Content-Type:application/json \\
  -d "{\\"token\\":\\"$TOKEN\\",\\"hostname\\":\\"$(hostname 2>/dev/null || echo unknown)\\",\\"os\\":\\"$(uname 2>/dev/null)\\"}" >/dev/null 2>&1 || true
# 心跳：优先写 crontab（重启也在）；写不进就先跑个后台循环
if command -v crontab >/dev/null 2>&1; then
  ( crontab -l 2>/dev/null | grep -v nassafe-agent; echo "* * * * * $HB >/dev/null 2>&1 # nassafe-agent" ) | crontab - >/dev/null 2>&1 || true
fi
( while :; do $HB >/dev/null 2>&1; sleep 60; done ) >/dev/null 2>&1 &
echo "[TS Safe] 代理已安装，已回连中控。"
"""


def agent_install_script(token: str, center: str) -> str:
    devs = load_devices()
    hit = _find_by_agent_token(devs, token)
    if hit is None:
        raise storage.StorageError("安装令牌无效，请先在中控「安装轻量代理」里重新生成")
    center = (center or "").rstrip("/")
    return AGENT_INSTALL_TEMPLATE.replace("__CENTER__", center).replace("__TOKEN__", token)


# Windows 版轻量代理：PowerShell 注册 + schtasks 每分钟心跳计划任务（重启也在）
AGENT_INSTALL_PS_TEMPLATE = r'''# TS Safe Windows agent v2: UI form (tray helper with built-in agent heartbeat), fallback to hidden heartbeat task
$ErrorActionPreference = "SilentlyContinue"
$Center = "__CENTER__"
$Token  = "__TOKEN__"
$Dir = Join-Path $env:ProgramData "NassafeAgent"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

# 0) Register with hostname + OS info (console flips to installed immediately)
$os = "Windows"
try { $os = "Windows " + (Get-CimInstance Win32_OperatingSystem).Caption } catch {}
$reg = @{ token = $Token; hostname = $env:COMPUTERNAME; os = $os } | ConvertTo-Json
try {
  Invoke-RestMethod -Method Post -Uri "$Center/api/agent/register" -ContentType "application/json" -Body $reg -TimeoutSec 8 | Out-Null
} catch {}

# 1) UI form: download the tray helper EXE (notification + tray + agent heartbeat in one)
$exe = Join-Path $Dir "NASSafeAgent.exe"
$dl = $false
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri "$Center/agent/NASSafeAgent.exe" -OutFile $exe -UseBasicParsing -TimeoutSec 180
  if ((Get-Item $exe).Length -gt 1MB) { $dl = $true }
} catch {}
if ($dl) {
  Start-Process -FilePath $exe -ArgumentList @("--nas", $Center, "--token", $Token)
  Write-Host "[TS Safe] 桌面助手已下载并启动：右下角托盘会出现蓝紫色盾牌图标，代理心跳由它负责（含开机自启）。"
  exit 0
}

# 2) Fallback: hidden heartbeat task (when EXE download failed)
# 1) Heartbeat payload + script
('{""token"":""__TOKEN__""}') | Set-Content -Encoding ASCII -Path (Join-Path $Dir "hb.json")
$hb = @'
$body = Get-Content -Raw -Path (Join-Path $env:ProgramData "NassafeAgent\hb.json")
try {
  Invoke-RestMethod -Method Post -Uri "__CENTER__/api/agent/heartbeat" -ContentType "application/json" -Body $body -TimeoutSec 8 | Out-Null
} catch {}
'@
$hb | Set-Content -Encoding ASCII -Path (Join-Path $Dir "heartbeat.ps1")

# 2) Register with hostname + OS info
$os = "Windows"
try { $os = "Windows " + (Get-CimInstance Win32_OperatingSystem).Caption } catch {}
$reg = @{ token = $Token; hostname = $env:COMPUTERNAME; os = $os } | ConvertTo-Json
try {
  Invoke-RestMethod -Method Post -Uri "$Center/api/agent/register" -ContentType "application/json" -Body $reg -TimeoutSec 8 | Out-Null
} catch {}

# 3) Scheduled task: heartbeat every minute (survives reboot; wscript wrapper = no console flash)
#    计划任务直接启动 powershell.exe 必闪黑框一瞬（-WindowStyle Hidden 挡不住），
#    必须经 wscript + vbs 以完全隐藏窗口的方式拉起。
$vbs = Join-Path $Dir "heartbeat.vbs"
$vbsline = 'CreateObject("Wscript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""' + "$Dir\heartbeat.ps1" + '"", 0, False'
$vbsline | Set-Content -Encoding ASCII -Path $vbs
$act = "wscript.exe `"$vbs`""
schtasks /Create /F /SC MINUTE /MO 1 /TN "NassafeAgent" /TR $act | Out-Null
# Run once right now (windowless) so the console flips to installed immediately
wscript.exe $vbs
Write-Host "[TS Safe] Windows agent installed OK. Heartbeat task: NassafeAgent (every minute)."
'''


def agent_install_script_ps(token: str, center: str) -> str:
    devs = load_devices()
    hit = _find_by_agent_token(devs, token)
    if hit is None:
        raise storage.StorageError("安装令牌无效，请先在中控「安装轻量代理」里重新生成")
    center = (center or "").rstrip("/")
    return AGENT_INSTALL_PS_TEMPLATE.replace("__CENTER__", center).replace("__TOKEN__", token)


# ---------------------------------------------------------------------------
# 傻瓜式配对（消费级：6 位配对码，免 token 复制 / 免 PowerShell）
# 流程：中控「添加 Windows 设备」→ 生成待配对设备 + 6 位码 → 用户在电脑上打开
#       助手并输入该码 → 助手用码调 /api/agent/pair 换取永久令牌 → 自动心跳在线。
# ---------------------------------------------------------------------------

PAIRING_TTL = 600  # 配对码有效期（秒）


def create_pairing(name: str | None = None) -> dict:
    """中控生成「待配对」Windows 设备 + 6 位配对码，返回给前端展示。"""
    devs = load_devices()
    # 清掉任何未过期但已废弃的待配对码，避免多码并存让用户困惑
    for d in devs:
        ag = d.get("agent") or {}
        if ag.get("pairing_code") and (ag.get("pairing_expires") or 0) > time.time():
            ag["pairing_code"] = ""
            ag["pairing_expires"] = 0
            d["agent"] = ag
    dev_id = f"dev-{int(time.time())}"
    code = f"{secrets.randbelow(1000000):06d}"
    token = secrets.token_hex(8)
    ag = {
        "status": "pending",
        "token": token,
        "pairing_code": code,
        "pairing_expires": time.time() + PAIRING_TTL,
        "requested_at": time.time(),
    }
    devs.append({
        "id": dev_id,
        "name": (name or "Windows 电脑").strip(),
        "group": "联网设备",
        "brand": "windows",
        "brand_label": "Windows 电脑",
        "type": "remote",
        "host": "",
        "port": 0,
        "https": False,
        "token": "",
        "enabled": True,
        "note": "",
        "agent": ag,
    })
    save_devices(devs)
    return {"ok": True, "id": dev_id, "code": code, "expires_in": PAIRING_TTL, "token": token}


def pair_with_code(code: str, info: dict | None = None) -> dict:
    """被控端用 6 位码换取永久令牌：绑定到对应待配对设备。一次性（用完即焚）。"""
    code = (code or "").strip()
    devs = load_devices()
    for d in devs:
        ag = d.get("agent") or {}
        if ag.get("pairing_code") == code:
            if (ag.get("pairing_expires") or 0) < time.time():
                return {"ok": False, "error": "配对码已过期，请在控制台重新生成"}
            token = ag.get("token") or secrets.token_hex(8)
            ag["token"] = token
            ag["status"] = "installed"
            ag["installed_at"] = time.time()
            ag["pairing_code"] = ""
            ag["pairing_expires"] = 0
            if info:
                ag["info"] = info
                _os = str(info.get("os") or "").lower()
                if "windows" in _os:
                    d["brand"] = "windows"
                    d["brand_label"] = "Windows 电脑"
            d["agent"] = ag
            d["last_seen"] = time.time()
            save_devices(devs)
            return {"ok": True, "token": token, "id": d.get("id"), "name": d.get("name")}
    return {"ok": False, "error": "配对码无效"}


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


# ---------------------------------------------------------------------------
# 联机设备管控（设置后台）：动态展示在线/离线数量与状态，并配置每台设备的功能开关
# ---------------------------------------------------------------------------

# 每台设备可在控制台集中配置的「功能」：重复文件清理 / 磁盘清理 / 换机迁移 / 自动快照
DEVICE_FEATURES = ["dups", "junk", "migrate", "autosnap"]


def _http_probe(host: str, port: int, https: bool = False, timeout: float = 1.5) -> bool:
    """快速探测对端 TS Safe 的 /api/system 是否可达（只读 GET）。"""
    import urllib.request
    if not host or not port:
        return False
    url = f"{'https' if https else 'http'}://{host}:{port}/api/system"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(64)
        return b'"ok":' in body or b'"ok"' in body or b'"brand"' in body
    except Exception:
        return False


def _online_status(dev: dict) -> str:
    """基于最近心跳/采集估计设备在线状态（不实时探测，避免拖慢接口）。

    注意：在线只能依据「代理真实心跳 last_checkin」。不能回退到 installed_at
    （标记安装的时间）或 netscan 的 last_seen（ICMP 存活），否则「标记安装但代理
    从未连上 / 仅 ICMP 通但 TS Safe 没跑」也会被误判为在线。

    补充：对端若是完整 TS Safe 服务端（full_server），它没有轻量代理心跳，此时
    用一次极短的 HTTP 探测 /api/system 来确认是否在线，避免「NAS 明明在线、管控页
    却显示离线」。
    """
    ag = dev.get("agent") or {}
    if dev.get("type") == "local":
        return "online"
    # 已安装代理：必须 5 分钟内有真实心跳才算在线
    lc = ag.get("last_checkin") or 0
    if ag.get("status") == "installed" and (time.time() - float(lc or 0)) < 360:
        return "online"
    # 完整服务端：没有代理心跳，直接 HTTP 探测 TS Safe 服务是否活着
    if dev.get("full_server"):
        host = (dev.get("host") or "").strip()
        port = int(dev.get("port") or 0)
        if host and port and _http_probe(host, port, bool(dev.get("https"))):
            return "online"
    if dev.get("host"):
        return "offline"
    return "unknown"


def get_manage() -> dict:
    """返回联机设备管控视图：在线统计 + 每台设备的状态、代理、功能开关。"""
    devs = load_devices()
    rows = []
    online = 0
    for d in devs:
        if not d.get("enabled", True):
            continue
        ag = d.get("agent") or {}
        st = _online_status(d)
        if st == "online":
            online += 1
        feats = dict(d.get("features") or {})
        rows.append({
            "id": d.get("id"),
            "name": d.get("name") or "设备",
            "type": d.get("type") or "remote",
            "brand": d.get("brand") or "generic_linux",
            "brand_label": d.get("brand_label") or brandmod.BRAND_LABELS.get(d.get("brand") or "", d.get("brand") or ""),
            "host": d.get("host", ""),
            "port": d.get("port", 0),
            "status": st,
            "agent_status": "installed" if (d.get("type") == "local" or d.get("id") == LOCAL_ID)
                           else (ag.get("status") or "none"),
            "full_server": bool(d.get("full_server")),
            "features": {k: bool(feats.get(k)) for k in DEVICE_FEATURES},
        })
    return {"ok": True, "online": online, "total": len(rows), "devices": rows}


def set_manage(payload: dict) -> dict:
    """配置单台设备的功能开关；可选补装轻量代理。"""
    dev_id = (payload.get("id") or "").strip()
    if not dev_id:
        raise storage.StorageError("缺少 id 参数")
    devs = load_devices()
    hit = next((d for d in devs if d.get("id") == dev_id), None)
    if hit is None:
        raise storage.StorageError("设备不存在")

    feats = dict(hit.get("features") or {})
    changed = False
    for k in DEVICE_FEATURES:
        if k in payload:
            feats[k] = bool(payload[k])
            changed = True
    if changed:
        hit["features"] = feats

    # 自动快照：已装代理的设备，最好努力把配置推到对端实例（同源接口）
    if "autosnap" in payload and feats.get("autosnap") and hit.get("agent", {}).get("status") == "installed":
        _push_remote_autosnap(hit)

    if payload.get("install_agent"):
        hit["agent"] = _agent_pending(hit.get("agent"))
    save_devices(devs)
    return {"ok": True, "id": dev_id, "features": {k: bool(feats.get(k)) for k in DEVICE_FEATURES}}


def _push_remote_autosnap(dev: dict) -> None:
    """尽力把自动快照开关推到对端 TS Safe（同源 /api/autosnapshot）。失败静默。"""
    host = (dev.get("host") or "").strip()
    port = int(dev.get("port") or 0)
    if not host or not port:
        return
    base = f"http{'s' if dev.get('https') else ''}://{host}:{port}"
    token = (dev.get("token") or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        _http_post(base + "/api/autosnapshot", headers,
                   {"enabled": True, "interval_hours": 1, "keep": 48, "volumes": []}, timeout=5)
    except Exception:  # noqa: BLE001 尽力而为，失败不影响本机
        pass


def _http_post(url: str, headers: dict, body: dict, timeout: int = 5):
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))
