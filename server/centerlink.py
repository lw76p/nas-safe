"""TS Safe — 完整版 → 来源总控台 的自动报到与心跳。

为什么需要这个模块
------------------
总控台可能在公网（例如 47.108.213.178），而被管设备通常在内网（例如
192.168.8.x）。总控台**无法反向连接**内网设备，所以「这台设备在不在线」不能靠
总控台主动去拉探测，必须由**设备自己主动上报**。本模块就是这条上报链路。

工作方式
--------
1. 启动时若安装目录里有 ``install_source.json``（打包完整版时随包下发的
   「来源总控台地址 + 一次性票据」），就向该总控台 ``POST /api/auto-claim``
   换一张长期心跳 token，成功后写入 ``state/center_link.json``；
2. 之后每 60 秒 ``POST {center}/api/agent/heartbeat``，让总控台把本机判为在线；
3. 任何一步失败都**静默重试**，绝不打断本机自身的服务（这是旁路能力，不是主链路）。

这样装完完整版后，用户回到原来的总控台（NAS 或云端）就能立刻看到这台设备在线，
不需要总控台能连到本机。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALL_ROOT = os.path.dirname(SERVER_DIR)
SOURCE_FILE = os.path.join(INSTALL_ROOT, "install_source.json")
LINK_FILE_NAME = "center_link.json"
HEARTBEAT_INTERVAL = 60


def _state_dir() -> str:
    try:
        import storage  # 延迟导入，避免独立运行时的循环依赖
        d = storage.state_dir()
    except Exception:  # noqa: BLE001
        d = os.environ.get("NASSAFE_STATE_DIR") or os.path.join(
            os.path.dirname(INSTALL_ROOT), "state")
    os.makedirs(d, exist_ok=True)
    return d


def _link_path() -> str:
    return os.path.join(_state_dir(), LINK_FILE_NAME)


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _post_json(url: str, payload: dict, timeout: int = 8) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "TS-Safe-Agent/1.0"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "返回内容不是 JSON"}


def lan_ip() -> str:
    """取本机在局域网里的地址（只建立 UDP 套接字、不发包，不会真的联网）。"""
    for host in ("10.255.255.255", "8.8.8.8", "192.168.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.4)
            s.connect((host, 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            try:
                s.close()
            except OSError:
                pass
    return ""


def _os_label() -> str:
    """给品牌识别用的系统标签。

    注意：Windows 上 os.name='nt'、sys.platform='win32'，两者都不含 "windows"
    字样，直接上报会被判成 generic_linux（曾导致设备显示成「通用设备」）。
    """
    p = sys.platform
    if p.startswith("win"):
        return "windows " + p
    if p == "darwin":
        return "macos " + p
    if p.startswith("linux"):
        return "linux " + p
    return p or os.name


def _machine_id() -> str:
    try:
        import storage
        return storage.machine_id()
    except Exception:  # noqa: BLE001
        return ""


def read_link() -> dict:
    return _load_json(_link_path())


def _claim(center: str, ticket: str) -> dict:
    url = center.rstrip("/") + "/api/auto-claim"
    return _post_json(url, {
        "ticket": ticket,
        "machine_id": _machine_id(),
        "hostname": socket.gethostname(),
        "os": _os_label(),
        "host": lan_ip(),
        "port": int(os.environ.get("NASSAFE_PORT") or 8848),
    })


def bootstrap() -> dict:
    """确保本机已在来源总控台登记并拿到心跳 token。幂等，可重复调用。"""
    link = read_link()
    if link.get("token") and link.get("center"):
        if "://" not in str(link.get("center")):
            link["center"] = "http://" + str(link["center"]).strip().rstrip("/")
            _save_json(_link_path(), link)
        return link
    src = _load_json(SOURCE_FILE)
    center = str(src.get("center") or "").strip().rstrip("/")
    if center and "://" not in center:
        center = "http://" + center
    ticket = str(src.get("ticket") or "").strip()
    if not center or not ticket:
        return {}
    try:
        res = _claim(center, ticket)
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    if not res.get("ok") or not res.get("token"):
        return {}
    link = {
        "center": center,
        "token": res.get("token"),
        "device_id": res.get("id"),
        "device_name": res.get("name"),
        "claimed_at": time.time(),
    }
    _save_json(_link_path(), link)
    # 票据已核销，删掉安装目录里的来源文件，避免重复使用
    try:
        os.remove(SOURCE_FILE)
    except OSError:
        pass
    return link


def _heartbeat(center: str, token: str) -> bool:
    url = center.rstrip("/") + "/api/agent/heartbeat"
    try:
        res = _post_json(url, {"token": token}, timeout=6)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return bool(res.get("ok"))


def _loop() -> None:
    while True:
        link = read_link()
        if not (link.get("token") and link.get("center")):
            link = bootstrap()
        if link.get("token") and link.get("center"):
            if not _heartbeat(link["center"], link["token"]):
                # 心跳被拒（设备被删/令牌失效）时，尝试用安装目录里的来源文件重新报到
                bootstrap()
        time.sleep(HEARTBEAT_INTERVAL)


def start() -> None:
    """启动上报线程（幂等；非阻塞）。任何异常都不影响本机服务启动。"""
    try:
        if read_link() or os.path.isfile(SOURCE_FILE):
            th = threading.Thread(target=_loop, name="center-link", daemon=True)
            th.start()
    except Exception:  # noqa: BLE001
        pass


def status() -> dict:
    """供 /api/system 等自检端点读取：当前是否已向总控台报到。"""
    link = read_link()
    return {
        "linked": bool(link.get("token") and link.get("center")),
        "center": link.get("center", ""),
        "device_id": link.get("device_id", ""),
        "pending_source": os.path.isfile(SOURCE_FILE),
    }


if __name__ == "__main__":
    info = bootstrap()
    print(json.dumps(info or status(), ensure_ascii=False, indent=2))
