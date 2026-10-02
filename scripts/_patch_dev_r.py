# -*- coding: utf-8 -*-
"""后端补丁：设备重命名 + 扫描接口（一次完成，每处替换前 assert count==1）"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def patch(path, pairs):
    p = os.path.join(ROOT, path)
    with io.open(p, "r", encoding="utf-8", newline="") as f:
        s = f.read()
    for old, new in pairs:
        assert s.count(old) == 1, (path, s.count(old), old[:70])
        s = s.replace(old, new)
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    print("patched", path)


# ---------------- server/devices.py ----------------
patch("server/devices.py", [
    # 1) local summary 输出自定义名标记
    (
        '''    summary = {
        "id": dev.get("id", LOCAL_ID),
        "name": dev.get("name", "本机 NAS"),''',
        '''    summary = {
        "id": dev.get("id", LOCAL_ID),
        "name": dev.get("name") or _default_local_name(dev),
        "custom_name": bool(dev.get("custom_name")),''',
    ),
    # 2) remote summary 输出自定义名标记
    (
        '''        "id": dev.get("id"), "name": dev.get("name", "远程设备"),''',
        '''        "id": dev.get("id"), "name": dev.get("name", "远程设备"),
        "custom_name": bool(dev.get("custom_name")),''',
    ),
    # 3) add_device：同地址不重复登记（改判为更新），并支持批量字段
    (
        '''    devs = load_devices()
    dev_id = f"dev-{int(time.time())}"
    devs.append({''',
        '''    devs = load_devices()
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
    devs.append({''',
    ),
    (
        '''        "enabled": True,
        "note": "",
    })
    save_devices(devs)
    return {"ok": True, "id": dev_id}''',
        '''        "enabled": True,
        "note": "",
        "net_kind": (payload.get("net_kind") or "").strip(),
    })
    save_devices(devs)
    return {"ok": True, "id": dev_id}''',
    ),
    # 4) 新增：默认本机名 + 重命名
    (
        '''def remove_device(dev_id: str) -> dict:''',
        '''def _default_local_name(dev: dict | None = None) -> str:
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


def remove_device(dev_id: str) -> dict:''',
    ),
])

# ---------------- server/app.py ----------------
patch("server/app.py", [
    (
        '''            elif route == "/api/devices/refresh":''',
        '''            elif route == "/api/devices/scan":
                # 自动扫描：局域网 + 异地组网（Tailscale/WireGuard/VPN 等）里的 NAS Safe
                self._send_json({"ok": True, **netscan.scan(payload)})
            elif route == "/api/devices/rename":
                dev_id = (payload.get("id") or "").strip()
                name = (payload.get("name") or "").strip()
                if not dev_id:
                    raise StorageError("缺少 id 参数")
                self._send_json({"ok": True, **devices.rename_device(dev_id, name)})
            elif route == "/api/devices/refresh":''',
    ),
])

# app.py 顶部 import netscan
p = os.path.join(ROOT, "server", "app.py")
with io.open(p, "r", encoding="utf-8", newline="") as f:
    s = f.read()
if "\nimport netscan" not in s:
    m = "import devices  # noqa: E402"
    if s.count(m) == 1:
        s = s.replace(m, "import devices  # noqa: E402\nimport netscan  # noqa: E402  联网设备自动扫描")
    else:
        m2 = "import metrics  # noqa: E402"
        assert s.count(m2) == 1, s.count(m2)
        s = s.replace(m2, m2 + "\nimport netscan  # noqa: E402  联网设备自动扫描")
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    print("patched app.py imports")

print("OK")
