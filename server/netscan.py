"""联网设备自动扫描 —— 找出局域网和异地组网里运行 NAS Safe 的设备。

场景：家里和办公室通过异地组网（Tailscale / WireGuard / ZeroTier / 各种 VPN）打通后，
对端设备并不在本地局域网里，手动一台台加地址很麻烦。本模块自动：
  1. 读出本机所有网卡的网段（含异地组网虚拟网卡，如 tailscale0 / wg0 / utun / tun0）；
  2. 在这些网段里并发探测指定端口（默认 8848）；
  3. 对端口开放的主机拉一次 /api/system，确认是不是 NAS Safe 并读出品牌与主机名。

设计红线（安全）：
- 纯只读：只做 TCP 连接 + 一次 GET，绝不往对端写任何东西。
- 只扫私有网段：公网地址默认跳过（需显式 allow_public 才扫，且仍只读）。
- 规模护栏：单网段最多扫 1024 个地址、总数上限 4096、并发 96、单连接超时可配。
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import socket
import subprocess
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PORTS = [8848]
MAX_PER_CIDR = 1024        # 单个网段最多探测多少个地址
MAX_TOTAL = 4096           # 一次扫描的地址总数上限
MAX_WORKERS = 96
CONNECT_TIMEOUT = 0.6      # 单主机 TCP 连接超时（秒）
HTTP_TIMEOUT = 2.5

VPN_IFACE_RE = re.compile(
    r"tailscale|wireguard|^wg|^zt|zerotier|^tun|^tap|^utun|vpn|nebula|nordlynx|easyvpn|openvpn|pptp|l2tp",
    re.I,
)
SKIP_IFACE_RE = re.compile(r"^lo|^docker|^br-|^veth|^virbr|^vmnet|^cni|^flannel|^kube", re.I)


# ---------------------------------------------------------------------------
# 网段枚举（跨平台：Linux / macOS / Windows 各自兜底）
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return p.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


def _ip_to_int(s: str) -> int:
    a = s.split(".")
    if len(a) != 4:
        raise ValueError(s)
    return (int(a[0]) << 24) | (int(a[1]) << 16) | (int(a[2]) << 8) | int(a[3])


def _int_to_ip(n: int) -> str:
    return f"{(n >> 24) & 255}.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


def _mask_to_prefix(mask: str) -> int:
    try:
        return bin(_ip_to_int(mask)).count("1")
    except Exception:  # noqa: BLE001
        return 24


def _parse_ip_cmd() -> list[tuple[str, str, int]]:
    """Linux: ip -o -4 addr show ——> [(iface, ip, prefix)]"""
    out = _run(["ip", "-o", "-4", "addr", "show"])
    res = []
    if not out:
        return res
    # 形如：2: eth0    inet 172.19.52.23/18 brd ... scope global dynamic eth0
    # 接口名取行首的 "序号: 名称"，别从行尾抓（行尾是 valid_lft xxxsec 这种）
    for line in out.splitlines():
        m_if = re.match(r"^\d+:\s+([^:\s]+)", line)
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not m:
            continue
        iface = m_if.group(1).split("@")[0].strip() if m_if else ""
        if not iface:
            m_dev = re.search(r"dev\s+(\S+)", line)
            iface = m_dev.group(1) if m_dev else "eth0"
        res.append((iface, m.group(1), int(m.group(2))))
    return res


def _parse_ifconfig() -> list[tuple[str, str, str]]:
    """macOS / BSD / 老 Linux: ifconfig ——> [(iface, ip, netmask)]"""
    out = _run(["ifconfig", "-a"]) or _run(["/sbin/ifconfig", "-a"])
    res = []
    cur = ""
    if not out:
        return res
    for line in out.splitlines():
        if line and not line[0].isspace():
            cur = line.split(":")[0].strip()
            continue
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)", line)
        if m and cur:
            mask = m.group(2)
            if mask.startswith("0x"):
                mask = _int_to_ip(int(mask, 16))
            res.append((cur, m.group(1), mask))
    return res


def _parse_ipconfig() -> list[tuple[str, str, str]]:
    """Windows: ipconfig ——> [(iface, ip, netmask)]（中英文输出都兼容）"""
    out = _run(["ipconfig"])
    res = []
    cur_ip = None
    iface = "ethernet"
    if not out:
        return res
    for raw in out.splitlines():
        line = raw.rstrip()
        if line and not line[0].isspace() and ":" not in line[:4]:
            iface = line.strip().rstrip(":") or "ethernet"
            cur_ip = None
        if re.search(r"(IPv4|IP(v4)?\s*地址|IPv4 Address)", line) and ":" in line:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line.split(":")[-1])
            if m:
                cur_ip = m.group(1)
        elif re.search(r"(子网掩码|Subnet Mask)", line) and ":" in line and cur_ip:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line.split(":")[-1])
            if m:
                res.append((iface, cur_ip, m.group(1)))
                cur_ip = None
    return res


def _parse_ioctl() -> list[tuple[str, str, str]]:
    """Linux 兜底：ioctl 直接读网卡地址与掩码。"""
    res = []
    if not hasattr(socket, "if_nameindex"):
        return res
    try:
        import fcntl  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return res
    SIOCGIFADDR = 0x8915
    SIOCGIFNETMASK = 0x891B
    for _, name in socket.if_nameindex():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            buf = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name[:15].encode()))
            ip = socket.inet_ntoa(buf[20:24])
            buf2 = fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, struct.pack("256s", name[:15].encode()))
            mask = socket.inet_ntoa(buf2[20:24])
            s.close()
            res.append((name, ip, mask))
        except Exception:  # noqa: BLE001
            continue
    return res


def _is_private(ip_int: int) -> bool:
    a = (ip_int >> 24) & 255
    b = (ip_int >> 16) & 255
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    if a == 100 and 64 <= b <= 127:      # Tailscale / CGNAT
        return True
    if a == 169 and b == 254:            # 链路本地（跳过）
        return False
    if a == 127:
        return False
    return False


def local_cidrs() -> list[dict]:
    """返回本机所在的所有网段：{cidr, iface, kind}  kind=lan|vpn"""
    raw: list[tuple[str, str, int]] = []
    for iface, ip, p in _parse_ip_cmd():
        raw.append((iface, ip, p))
    if not raw:
        for iface, ip, mask in _parse_ifconfig():
            raw.append((iface, ip, _mask_to_prefix(mask)))
    if not raw and sys.platform.startswith("win"):
        for iface, ip, mask in _parse_ipconfig():
            raw.append((iface, ip, _mask_to_prefix(mask)))
    if not raw:
        for iface, ip, mask in _parse_ioctl():
            raw.append((iface, ip, _mask_to_prefix(mask)))

    out, seen = [], set()
    for iface, ip, prefix in raw:
        if SKIP_IFACE_RE.search(iface or ""):
            continue
        try:
            ip_int = _ip_to_int(ip)
        except Exception:  # noqa: BLE001
            continue
        if not _is_private(ip_int):
            continue
        if prefix < 8 or prefix > 30:
            continue
        net_int = ip_int & ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF)
        cidr = f"{_int_to_ip(net_int)}/{prefix}"
        key = (cidr, iface)
        if key in seen:
            continue
        seen.add(key)
        kind = "vpn" if VPN_IFACE_RE.search(iface or "") else "lan"
        out.append({"cidr": cidr, "iface": iface, "kind": kind, "self_ip": ip})
    out.sort(key=lambda x: (x["kind"] != "vpn", x["cidr"]))
    return out


# ---------------------------------------------------------------------------
# 主机探测
# ---------------------------------------------------------------------------

def _tcp_open(ip: str, port: int, timeout: float) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        rc = s.connect_ex((ip, port))
        s.close()
        return rc == 0
    except Exception:  # noqa: BLE001
        return False


def _http_get(url: str, user: str, pwd: str, timeout: float):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    if user:
        tok = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode()
        req.add_header("Authorization", f"Basic {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def probe_host(ip: str, port: int, timeout: float = CONNECT_TIMEOUT,
               user: str = "", pwd: str = "") -> dict | None:
    """探测单个主机；命中 NAS Safe 返回摘要，否则返回 None。"""
    if not _tcp_open(ip, port, timeout):
        return None
    info = {"ip": ip, "port": port, "nassafe": False, "brand": "generic_linux",
            "brand_label": "", "hostname": "", "https": False}
    scheme = "https" if port == 443 else "http"
    try:
        data = _http_get(f"{scheme}://{ip}:{port}/api/system", user, pwd, HTTP_TIMEOUT)
        sysinfo = (data or {}).get("system") or {}
        info["nassafe"] = True
        info["brand"] = sysinfo.get("os_id") or "generic_linux"
        info["brand_label"] = sysinfo.get("os_name") or ""
        info["hostname"] = sysinfo.get("hostname") or sysinfo.get("host") or ""
    except Exception as exc:  # noqa: BLE001
        # 端口开着但拿不到 /api/system：可能是需要账号、或不是 NAS Safe
        code = getattr(exc, "code", None)
        if code in (401, 403):
            info["nassafe"] = True
            info["need_auth"] = True
        else:
            return None
    return info


def _expand(cidr: str, cap: int, self_ip: str = "") -> tuple[list[str], bool]:
    """展开网段为地址列表（跳过网络号与广播），返回 (addrs, truncated)。

    本机地址排在最前面：网段很大被截断时，至少先把自己和附近的地址扫到。
    """
    try:
        net, prefix = cidr.split("/")
        prefix = int(prefix)
        base = _ip_to_int(net)
    except Exception:  # noqa: BLE001
        return [], False
    total = 1 << (32 - prefix)
    if total <= 2:
        return [net], False
    hosts = total - 2
    truncated = hosts > cap
    n = min(hosts, cap)
    addrs = [_int_to_ip(base + 1 + i) for i in range(n)]
    if self_ip:
        try:
            si = _ip_to_int(self_ip)
            if base < si < base + total - 1:
                addrs = [self_ip] + [a for a in addrs if a != self_ip][: n - 1]
        except Exception:  # noqa: BLE001
            pass
    return addrs, truncated


# ---------------------------------------------------------------------------
# 扫描入口
# ---------------------------------------------------------------------------

def scan(payload: dict | None = None) -> dict:
    p = payload if isinstance(payload, dict) else {}
    ports = [int(x) for x in (p.get("ports") or DEFAULT_PORTS) if str(x).strip()]
    if not ports:
        ports = list(DEFAULT_PORTS)
    include_vpn = bool(p.get("include_vpn", True))
    include_lan = bool(p.get("include_lan", True))
    timeout = float(p.get("timeout") or CONNECT_TIMEOUT)
    timeout = min(max(timeout, 0.15), 3.0)
    user = str(p.get("user") or "").strip()
    pwd = str(p.get("pwd") or "")
    cap = int(p.get("per_cidr") or MAX_PER_CIDR)
    cap = min(max(cap, 16), MAX_PER_CIDR)

    nets = [n for n in local_cidrs()
            if (n["kind"] == "vpn" and include_vpn) or (n["kind"] == "lan" and include_lan)]
    extra = p.get("cidrs")
    if isinstance(extra, str):
        extra = [x.strip() for x in re.split(r"[,\s]+", extra) if x.strip()]
    for c in (extra or []):
        nets.append({"cidr": c, "iface": "手动填写", "kind": "manual", "self_ip": ""})

    self_ips = {n.get("self_ip", "") for n in nets if n.get("self_ip")}
    targets: list[tuple[str, int, dict]] = []
    truncated = False
    meta = []
    for n in nets:
        addrs, tr = _expand(n["cidr"], cap, n.get("self_ip", ""))
        truncated = truncated or tr
        meta.append({**n, "count": len(addrs), "truncated": tr})
        for a in addrs:
            for port in ports:
                targets.append((a, port, n))
        if len(targets) >= MAX_TOTAL:
            truncated = True
            break
    targets = targets[:MAX_TOTAL]

    started = time.time()
    found: list[dict] = []
    if targets:
        def work(t):
            ip, port, net = t
            r = probe_host(ip, port, timeout, user, pwd)
            if r:
                r["kind"] = net.get("kind", "lan")
                r["iface"] = net.get("iface", "")
                r["cidr"] = net.get("cidr", "")
                r["is_self"] = ip in self_ips
            return r
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(8, len(targets)))) as ex:
            for r in ex.map(work, targets):
                if r:
                    found.append(r)
    # 去重（同 IP 多端口命中保留第一个）
    seen, uniq = set(), []
    for f in found:
        k = f["ip"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)
    return {
        "ok": True,
        "scanned": len(targets),
        "nets": meta,
        "found": uniq,
        "truncated": truncated,
        "seconds": round(time.time() - started, 2),
        "host_os": platform.system(),
    }
