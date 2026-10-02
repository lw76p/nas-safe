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
    """跑命令取输出。中文 Windows 的 ipconfig 是 GBK，直接按 utf-8 解码会炸，这里逐级兜底。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=8)
        out = p.stdout or b""
    except Exception:  # noqa: BLE001
        return ""
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return out.decode(enc)
        except Exception:  # noqa: BLE001
            continue
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
                # 中文 Windows 输出形如「以太网适配器 以太网」，去掉冗余词只留接口名
                nice = re.sub(r"适配器\s*", "", iface).strip() or iface
                res.append((nice, cur_ip, m.group(1)))
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
# 设备指纹 + AI 辅助识别
# ---------------------------------------------------------------------------

FINGER_PORTS = [22, 80, 139, 443, 445, 548, 554, 631, 3389, 5000, 5001, 5900, 8080, 8848]
BANNER_PORTS = [80, 443, 5000, 5001, 8080, 8848]
MAX_FINGER = 192          # 最多给多少台主机采指纹
MAX_AI = 12               # 最多让 AI 判断多少台（省时间、省 token）


def _grab_banner(ip: str, port: int, timeout: float = 1.6) -> str:
    """抓 HTTP 的 Server 头与网页标题，够用来猜品牌了。"""
    import ssl
    import urllib.request
    scheme = "https" if port in (443,) else "http"
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    try:
        req = urllib.request.Request(f"{scheme}://{ip}:{port}/",
                                     headers={"User-Agent": "nassafe-scan"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            server = (r.headers.get("Server") or "").strip()
            body = r.read(20000).decode("utf-8", "ignore")
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
        return " / ".join([x for x in (server, title) if x])[:120]
    except Exception:  # noqa: BLE001
        return ""


def fingerprint(ip: str, ports: list | None = None, timeout: float = CONNECT_TIMEOUT) -> dict:
    """采一台主机的指纹：开放端口 + HTTP 标题 + 主机名。只读探测。"""
    ports = ports or FINGER_PORTS
    open_ports: list[int] = []
    with ThreadPoolExecutor(max_workers=min(16, len(ports))) as ex:
        for port, ok in zip(ports, ex.map(lambda p: _tcp_open(ip, p, timeout), ports)):
            if ok:
                open_ports.append(port)
    banners = {}
    for p in BANNER_PORTS:
        if p in open_ports:
            b = _grab_banner(ip, p)
            if b:
                banners[str(p)] = b
    hostname = ""
    try:
        hostname = socket.getnameinfo((ip, 0), 0)[0] or ""
    except Exception:  # noqa: BLE001
        hostname = ""
    return {"ip": ip, "open_ports": open_ports, "banners": banners, "hostname": hostname}


def _rule_identify(fp: dict) -> dict:
    """本地规则兜底：端口组合 + 网页标题猜设备类型与品牌。"""
    ports = set(fp.get("open_ports") or [])
    blob = " ".join((fp.get("banners") or {}).values()) + " " + (fp.get("hostname") or "")
    low = blob.lower()
    out = {"device_type": "unknown", "brand_label": "", "suggest_name": "",
           "suggest_group": "", "confidence": 0.3, "by": "rule"}
    host = (fp.get("hostname") or "").lower()
    if 8848 in ports:
        out.update({"device_type": "nas_safe", "brand_label": "NAS Safe", "confidence": 0.9})
    elif 5000 in ports or 5001 in ports or "synology" in low or "dsm" in low:
        out.update({"device_type": "nas", "brand_label": "群晖 NAS", "confidence": 0.75})
    elif "qnap" in low or (8080 in ports and 443 in ports):
        out.update({"device_type": "nas", "brand_label": "威联通 NAS", "confidence": 0.6})
    elif 3389 in ports or 445 in ports or "win" in host:
        out.update({"device_type": "pc", "brand_label": "Windows 电脑", "confidence": 0.6})
    elif 5900 in ports or 548 in ports or 88 in ports or "macbook" in host or "mac-" in host:
        out.update({"device_type": "pc", "brand_label": "macOS 电脑", "confidence": 0.5})
    elif 22 in ports or "ubuntu" in low or "debian" in low:
        out.update({"device_type": "server", "brand_label": "Linux 服务器", "confidence": 0.45})
    elif 554 in ports or "camera" in low or "hikvision" in low or "dahua" in low:
        out.update({"device_type": "camera", "brand_label": "摄像头", "confidence": 0.55})
    elif 80 in ports or 443 in ports:
        out.update({"device_type": "unknown", "brand_label": "网络设备", "confidence": 0.3})
    if not out["brand_label"] and not ports:
        return out
    tail = (fp.get("ip") or "").split(".")[-1]
    out["suggest_name"] = f"{out['brand_label'] or '设备'} {tail}"
    return out


_AI_PROMPT = """你是网络设备识别助手。下面是内网/异地组网里扫到的主机指纹（开放端口、HTTP 标题、主机名）。
请逐台判断：是什么设备（nas / pc / server / camera / router / nas_safe / unknown）、品牌中文名、
一个好认的中文名字建议（不超过 8 个字，可带位置线索）、分组建议（家里 / 公司 / 机房 / 其它）、把握多大（0-1）。
只输出 JSON 数组，不要解释，格式：
[{"ip":"1.2.3.4","device_type":"nas","brand_label":"群晖","suggest_name":"办公室群晖","suggest_group":"公司","confidence":0.8}]
指纹：
"""


def _ai_identify(fps: list[dict], timeout_s: float = 25.0) -> dict:
    """把指纹交给 AI 批量判断；AI 没配/超时/返回不合法 → 返回 {}（由调用方退回规则）。"""
    try:
        import ai  # noqa: PLC0415
        if not ai.is_ready():
            return {}
    except Exception:  # noqa: BLE001
        return {}
    fps = fps[:MAX_AI]
    lines = []
    for f in fps:
        lines.append(
            f"- {f.get('ip')} 开放端口={','.join(str(p) for p in (f.get('open_ports') or [])) or '无'}"
            f" 标题={' | '.join((f.get('banners') or {}).values()) or '无'}"
            f" 主机名={f.get('hostname') or '无'}"
        )
    q = _AI_PROMPT + "\n".join(lines)
    try:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=1) as ex:
            fut = ex.submit(lambda: ai.answer(q))
            text, _provider = fut.result(timeout=timeout_s)
    except Exception:  # noqa: BLE001
        return {}
    text = (text or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    if isinstance(arr, list):
        for it in arr:
            if not isinstance(it, dict):
                continue
            ip = str(it.get("ip") or "").strip()
            if not ip:
                continue
            out[ip] = {
                "device_type": str(it.get("device_type") or "unknown"),
                "brand_label": str(it.get("brand_label") or ""),
                "suggest_name": str(it.get("suggest_name") or ""),
                "suggest_group": str(it.get("suggest_group") or ""),
                "confidence": float(it.get("confidence") or 0.5),
                "by": "ai",
            }
    return out

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
    # discover：没装 NAS Safe 的主机也采集指纹列出来；use_ai：用 AI 判断这些是什么设备
    discover = bool(p.get("discover", True))
    use_ai = bool(p.get("ai", True))
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
    hit_ips = set()
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
                    hit_ips.add(r["ip"])
    # 去重（同 IP 多端口命中保留第一个）
    seen, uniq = set(), []
    for f in found:
        k = f["ip"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)

    # 其余主机：采指纹 + AI 识别，告诉用户「网络里还有谁，装了 NAS Safe 就能纳管」
    others: list[dict] = []
    ai_used = False
    ai_note = ""
    if discover:
        cand_ips = []
        for ip, _port, _net in targets:
            if ip in hit_ips or ip in cand_ips:
                continue
            cand_ips.append(ip)
            if len(cand_ips) >= MAX_FINGER:
                break
        fps: list[dict] = []
        if cand_ips:
            with ThreadPoolExecutor(max_workers=min(48, len(cand_ips))) as ex:
                for fp in ex.map(lambda ip: fingerprint(ip, timeout=timeout), cand_ips):
                    if fp.get("open_ports") or fp.get("hostname"):
                        fps.append(fp)
        ai_map = {}
        if fps and use_ai:
            ai_map = _ai_identify(fps)
            ai_used = bool(ai_map)
            if not ai_map:
                ai_note = "AI 未启用或没返回结果，已用内置规则判断"
        for fp in fps:
            info = ai_map.get(fp["ip"]) or _rule_identify(fp)
            if not info.get("brand_label") and not fp.get("open_ports"):
                continue
            others.append({**fp, **info})

    return {
        "ok": True,
        "scanned": len(targets),
        "nets": meta,
        "found": uniq,
        "others": others,
        "ai_used": ai_used,
        "ai_note": ai_note,
        "truncated": truncated,
        "seconds": round(time.time() - started, 2),
        "host_os": platform.system(),
    }
