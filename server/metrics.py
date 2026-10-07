"""
TS Safe — 系统指标采集（仪表盘数据源，仅标准库）

设计原则（跨品牌分层适配，与 probe.sh 同哲学）：
  第一层 通用 Linux（飞牛/OMV/Unraid/TrueNAS/QTS 都有）：
      /proc/stat(CPU)、/proc/meminfo(RAM)、/proc/uptime、/proc/loadavg、
      /proc/net/dev(网速)、df -kP(卷容量)、/sys/block(磁盘)、/sys/class/hwmon(温度)
  第二层 尽力而为：
      磁盘温度（内核加载 drivetemp 才有）、风扇（品牌工具/驱动存在才有）
  第三层 品牌专属（探测到才采集，QTS: /sbin/qcli_hardware -F 风扇）：
      后续扩展：群晖 syno 系列 / 绿联 ugreen 系列
  每个字段都可缺省，capabilities 告诉前端哪些可用 —— 前端按能力渲染，绝不报错刷屏。

通道：容器远程管理模式经 SSH（复用 qnap.default_client 的凭据）；
      服务跑在 NAS 本机时直接 /bin/sh -c。单次采集 = 一条批量脚本一个来回。

缓存：结果缓存 TTL 15s；网速用相邻两次采集的字节差计算 bps。
"""

from __future__ import annotations

import sys
import time

# 结果缓存
_CACHE_TTL = 15.0
_last: dict | None = None          # {"ts": float, "data": dict}
_net_prev: dict | None = None      # {"ts", "total_rx", "total_tx", "ifaces": {名: (rx, tx)}}
_io_prev: dict | None = None       # {盘名: (读扇区, 写扇区)}

# 趋势样本环形缓冲（卷用量%/内存%）与上限
_hist: list = []
_HIST_MAX = 240                    # 15s/次 × 240 ≈ 1 小时窗口（窗口越长，趋势越可信）


def _compute_trends() -> list:
    """从 _hist 线性外推每个卷的增长速度与"预计几天后存满"。

    防误报（2026-10-02 修）：原先「首尾两点求斜率 + 30 分钟窗口」，一次临时写入
    （部署文件、建快照）就会被当成持续增速 —— 实测出现 40G 盘只用了 11%、剩余 34G
    却告警「预计 6.9 天后存满」。现在四道闸：
      1) 最小二乘线性回归求斜率（不再只取首尾两点，单点噪声影响大幅降低）
      2) 样本 >=20 且窗口 >=30 分钟才外推
      3) 增速门槛 0.05 %/小时（约 1.2 %/天），低于此视为缓慢增长不预测
      4) 已用 < 60% 时不做"预计存满"预测：空间还宽敞不该吓唬人。
         空间告警由 anomalies 的 75%/90% 绝对水位负责，
         趋势只回答"已经偏紧了还要多久"。
    """
    out: list = []
    if len(_hist) < 20:
        return out
    span = _hist[-1]["ts"] - _hist[0]["ts"]
    if span < 1800:                       # 30 分钟
        return out
    mounts: set = set()
    for h in _hist:
        mounts.update(h["vols"].keys())
    for mount in sorted(mounts):
        pts = [(h["ts"], h["vols"][mount]) for h in _hist
               if h["vols"].get(mount) is not None]
        if len(pts) < 20:
            continue
        pct = pts[-1][1]
        if pct is None or pct <= 1.0:
            continue
        # 最小二乘：slope = Σ(t-t̄)(p-p̄) / Σ(t-t̄)²   单位 %/小时
        t0 = pts[0][0]
        xs = [(t - t0) / 3600.0 for t, _ in pts]
        ys = [p for _, p in pts]
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        if den <= 0:
            continue
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        if slope <= 0.05:
            continue
        if pct < 60.0:                     # 还宽敞：不预测"几天后存满"
            continue
        days = (100.0 - pct) / slope / 24.0
        out.append({
            "mount": mount,
            "percent": round(pct, 1),
            "growth_pct_per_hour": round(slope, 3),
            "days_to_full": round(days, 1) if days <= 90 else None,
        })
    return out

# ---------------------------------------------------------------------------
# 容量告警判据（网页 / 日报 / 看门狗三端共用一套，避免各写一套阈值导致误报）
# ---------------------------------------------------------------------------
# 这些挂载不参与容量告警：系统内部挂载或虚拟文件系统，满了也不影响用户数据
_VOL_SKIP_PREFIX = ("/boot", "/dev", "/proc", "/sys", "/run", "/snap", "/mnt/snapshot")


def space_alerts(vols: list, trends: list | None = None) -> list:
    """返回 [{mount, percent, free_gb, sev, kind, days}]，sev: 2=严重 1=注意。

    两条硬门槛（缺一不报），杜绝「盘还很空却说要满了」：
      · 比例门槛：已用 >= 80%
      · 绝对门槛：剩余 < 10GB（严重档：已用 >= 92% 且剩余 < 3GB）
    """
    out: list = []
    for v in vols or []:
        try:
            total = int(v.get("total_kb") or 0)
            used = int(v.get("used_kb") or 0)
        except (TypeError, ValueError):
            continue
        if total <= 0:
            continue
        mount = str(v.get("mount") or "")
        if any(mount == p or mount.startswith(p + "/") for p in _VOL_SKIP_PREFIX):
            continue
        if total < 16 * 1024 * 1024:      # <16GB 视作系统内部卷
            continue
        pct = round(used / total * 100.0, 1)
        free_gb = round((total - used) / 1024 / 1024, 1)
        sev = 0
        if pct >= 92 and free_gb < 3:
            sev = 2
        elif pct >= 80 and free_gb < 10:
            sev = 1
        if not sev:
            continue
        out.append({"mount": mount, "percent": pct, "free_gb": free_gb, "sev": sev, "kind": "vol"})
    for t in trends or []:
        days = t.get("days_to_full")
        if not days:
            continue
        try:
            days = float(days)
        except (TypeError, ValueError):
            continue
        pct = float(t.get("percent") or 0)
        # 预测只提示「30 天内且已经用了七成以上」的情况，远的、宽的都不提
        if days > 30 or pct < 70:
            continue
        out.append({"mount": t.get("mount"), "percent": pct, "free_gb": None,
                    "sev": 1, "kind": "trend", "days": round(days, 1)})
    return out


_BATCH = r"""
echo "#STAT"
head -1 /proc/stat 2>/dev/null
sleep 0.5
head -1 /proc/stat 2>/dev/null
echo "#UP"
cat /proc/uptime 2>/dev/null
echo "#LOAD"
cat /proc/loadavg 2>/dev/null
echo "#MEM"
head -8 /proc/meminfo 2>/dev/null
echo "#NET"
cat /proc/net/dev 2>/dev/null
echo "#DF"
df -kP 2>/dev/null
echo "#BLK"
for d in /sys/block/*; do
  n=$(basename "$d")
  case "$n" in loop*|ram*|zram*|dm-*|md*|sr*|drbd*|nbd*) continue ;; esac
  [ -e "$d" ] || continue
  m=$(cat "$d/device/model" 2>/dev/null)
  [ -n "$m" ] || m=$(cat "$d/device/name" 2>/dev/null)
  echo "$n|$(cat "$d/size" 2>/dev/null)|$(echo "$m" | tr -d '\\t')"
done
echo "#DISKSTAT"
for d in /sys/block/*; do
  n=$(basename "$d")
  case "$n" in loop*|ram*|zram*|dm-*|md*|sr*|drbd*|nbd*) continue ;; esac
  [ -e "$d" ] || continue
  echo "$n|$(cat "$d/stat" 2>/dev/null)"
done
echo "#TEMP"
for h in /sys/class/hwmon/hwmon*; do
  [ -e "$h" ] || continue
  n=$(cat "$h/name" 2>/dev/null)
  for f in "$h"/temp*_input; do
    [ -e "$f" ] && echo "$n|$(basename "$f")|$(cat "$f" 2>/dev/null)"
  done
done 2>/dev/null
echo "#HOST"
hostname 2>/dev/null
echo "#FAN"
/sbin/qcli_hardware -F 2>/dev/null | grep fan_speed
echo "#END"
"""


def _run_cmd(script: str) -> str:
    """执行一条只读 shell 命令：复用 qnap 客户端通道（SSH 远程或本地均由其处理）。"""
    try:
        from qnap import default_client

        return default_client().run_shell(script)
    except Exception:
        pass
    import subprocess

    proc = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=60
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _run_batch() -> str:
    """执行批量采集脚本（单次 SSH/本地来回）。"""
    return _run_cmd(_BATCH)


def list_dirs(path: str, max_n: int = 300) -> dict:
    """列出生产目录的一级子目录（只读，供监控路径选择器用）。

    路径合法性（必须在卷挂载点内、无 ..）由调用方（app.py）校验。
    Windows 平台走本地 os.listdir，Linux 继续走 ls 管道。
    """
    if sys.platform == "win32":
        from winutils import list_dir
        return list_dir(path, max_n=max_n)

    import shlex as _shlex

    script = f"ls -Ap {_shlex.quote(path)} 2>/dev/null | grep '/$' | head -{max_n}"
    out = _run_cmd(script)
    dirs = sorted({
        line.rstrip("/").strip()
        for line in out.splitlines()
        if line.rstrip().endswith("/") and not line.rstrip("/").strip().startswith(".")
        and line.rstrip("/").strip()
    })
    return {"path": path, "dirs": dirs}


def _split_sections(text: str) -> dict:
    secs: dict[str, list] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("#") and line[1:].strip().isupper():
            cur = line[1:].strip()
            secs[cur] = []
        elif cur is not None:
            secs[cur].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in secs.items()}


def _parse_cpu(lines: list) -> float | None:
    rows = [l for l in lines if l.startswith("cpu ")]
    if len(rows) < 2:
        return None
    def busy_idle(row: str):
        vals = [int(x) for x in row.split()[1:9]]
        # user nice system irq softirq 属于忙；idle iowait 属于闲
        idle = vals[3] + vals[4]
        busy = vals[0] + vals[1] + vals[2] + vals[5] + vals[6]
        return busy, idle
    b1, i1 = busy_idle(rows[0])
    b2, i2 = busy_idle(rows[1])
    total = (b2 + i2) - (b1 + i1)
    if total <= 0:
        return None
    return round((b2 - b1) / total * 100.0, 1)


def _parse_mem(lines: list) -> dict | None:
    kv = {}
    for l in lines:
        p = l.split(":")
        if len(p) == 2:
            try:
                kv[p[0].strip()] = int(p[1].strip().split()[0])  # kB
            except (ValueError, IndexError):
                pass
    total = kv.get("MemTotal")
    if not total:
        return None
    avail = kv.get("MemAvailable")
    if avail is None:
        avail = kv.get("MemFree", 0) + kv.get("Buffers", 0) + kv.get("Cached", 0)
    used = max(total - avail, 0)
    return {
        "total_kb": total,
        "available_kb": avail,
        "used_kb": used,
        "percent": round(used / total * 100.0, 1),
    }


def _parse_net(lines: list) -> dict:
    """各网卡累计字节数（排除 lo）；bps 由与上次采样的差值计算。"""
    ifaces = []
    for l in lines:
        if ":" not in l:
            continue
        name, rest = l.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("bond", "dummy")):
            continue
        f = rest.split()
        if len(f) < 9:
            continue
        try:
            rx, tx = int(f[0]), int(f[8])
        except ValueError:
            continue
        ifaces.append({"iface": name, "rx_bytes": rx, "tx_bytes": tx})
    return {"ifaces": ifaces}


def _parse_diskstat(lines: list) -> dict:
    """各磁盘累计读/写扇区数（/sys/block/sdX/stat 第 3、7 字段）。"""
    out = {}
    for l in lines:
        p = l.strip().split("|")
        if len(p) != 2 or not p[0]:
            continue
        f = p[1].split()
        if len(f) < 7:
            continue
        try:
            out[p[0]] = (int(f[2]), int(f[6]))  # (读扇区, 写扇区)
        except ValueError:
            continue
    return out


def _diff_bps(cur: dict, prev: dict | None, ts: float, prev_ts: float | None) -> dict:
    """按同名键计算 每秒字节数差值；首轮无 prev 时返回空。"""
    if not prev or prev_ts is None:
        return {}
    dt = ts - prev_ts
    if dt <= 0:
        return {}
    out = {}
    for k, cur_val in cur.items():
        if k in prev:
            out[k] = (max(int((cur_val[0] - prev[k][0]) / dt), 0),
                      max(int((cur_val[1] - prev[k][1]) / dt), 0))
    return out


def _parse_df(lines: list) -> list:
    vols = []
    for l in lines:
        f = l.split()
        if len(f) < 6 or not f[0].startswith("/dev/"):
            continue
        try:
            total_kb, used_kb = int(f[1]), int(f[2])
        except ValueError:
            continue
        if total_kb < 16 * 1024 * 1024:  # <16GB 的是系统内部挂载，跳过
            continue
        vols.append({
            "mount": f[5],
            "total_kb": total_kb,
            "used_kb": used_kb,
            "percent": round(used_kb / total_kb * 100.0, 1) if total_kb else 0,
        })
    return vols


def _parse_blk(lines: list) -> list:
    disks = []
    for l in lines:
        p = l.strip().split("|")
        if len(p) < 2 or not p[0]:
            continue
        name = p[0].replace("/dev/", "").split("/")[-1] if "/" in p[0] else p[0]
        name = p[0].strip()
        try:
            sectors = int(p[1])
        except ValueError:
            continue
        size_b = sectors * 512
        model = (p[2].strip() if len(p) > 2 else "") or "未知型号"
        if size_b < 100 * 1024**3:  # <100GB 视为 USB 启动盘等，不进磁盘面板
            continue
        disks.append({"name": name, "size_b": size_b, "model": model})
    return disks


def _parse_temps(lines: list) -> tuple[float | None, list]:
    """返回 (cpu_temp, disk_temps)。k10temp/coretemp/cpu_thermal -> CPU；
    drivetemp -> 按顺序映射到 sdX（尽力而为）。"""
    cpu = None
    disk_temps: list = []
    for l in lines:
        p = l.strip().split("|")
        if len(p) != 3:
            continue
        chip, field, raw = p
        try:
            val = int(raw) / 1000.0  # 毫摄氏度
        except ValueError:
            continue
        if not (0 < val < 120):
            continue
        if chip in ("k10temp", "coretemp", "cpu_thermal", "zenpower") and field == "temp1_input":
            cpu = round(val, 1)
        elif chip == "drivetemp":
            disk_temps.append(round(val, 1))
    return cpu, disk_temps


def _parse_fan(lines: list) -> dict | None:
    kv = {}
    for l in lines:
        p = l.split()
        if len(p) == 2:
            kv[p[0]] = p[1]
    if not kv:
        return None
    def rpm(k):
        v = kv.get(k, "--")
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {
        "fan_rpm": rpm("fan_speed"),
        "cpu_fan_rpm": rpm("cpu_fan_speed"),
    }


def _fmt_uptime(seconds: float) -> dict:
    s = int(seconds)
    return {"days": s // 86400, "hours": (s % 86400) // 3600, "minutes": (s % 3600) // 60}


def _collect_windows() -> dict:
    """Windows 平台简化指标：提供卷列表，其余字段安全降级。"""
    from winutils import list_drives

    vols = list_drives()
    return {
        "hostname": "Windows",
        "uptime_s": 0,
        "uptime": {"days": 0, "hours": 0, "minutes": 0},
        "cpu": {"percent": None, "load1": None, "temp_c": None},
        "mem": None,
        "net": {"rx_bps": None, "tx_bps": None, "ifaces": []},
        "volumes": vols,
        "trends": [],
        "disks": [],
        "fan": None,
        "capabilities": {
            "cpu_percent": False,
            "cpu_temp": False,
            "mem": False,
            "net": False,
            "volumes": bool(vols),
            "disks": False,
            "disk_io": False,
            "disk_temp": False,
            "fan": False,
        },
    }


def collect(force: bool = False) -> dict:
    """采集系统指标（带缓存）。返回可直接 JSON 化的 dict。"""
    global _last, _net_prev, _io_prev
    now = time.time()
    if not force and _last and now - _last["ts"] < _CACHE_TTL:
        return _last["data"]

    if sys.platform == "win32":
        data = _collect_windows()
        _last = {"ts": now, "data": data}
        return data

    raw = _run_batch()
    secs = _split_sections(raw)
    if "STAT" not in secs or "END" not in raw:
        raise RuntimeError("指标采集脚本输出异常（SSH 通道或 /proc 不可用）")

    lines = {k: (v.splitlines() if isinstance(v, str) else []) for k, v in secs.items()}

    cpu_percent = _parse_cpu(lines.get("STAT", []))
    mem = _parse_mem(lines.get("MEM", []))
    net = _parse_net(lines.get("NET", []))
    vols = [v for v in _parse_df(lines.get("DF", []))
            if not v.get("mount", "").startswith(("/mnt/snapshot/", "/.snapshots", "/mnt/snaphot/"))]
    disks = _parse_blk(lines.get("BLK", []))
    cpu_temp, disk_temps = _parse_temps(lines.get("TEMP", []))
    fan = _parse_fan(lines.get("FAN", []))
    diskstat = _parse_diskstat(lines.get("DISKSTAT", []))

    # 网速 bps：与上次各网卡累计字节数做差（首轮无差值）
    rx_bps = tx_bps = None
    dt = now - _net_prev["ts"] if _net_prev else None
    if _net_prev and dt and dt > 0:
        total_rx = sum(i["rx_bytes"] for i in net["ifaces"])
        total_tx = sum(i["tx_bytes"] for i in net["ifaces"])
        rx_bps = max(int((total_rx - _net_prev["total_rx"]) / dt), 0)
        tx_bps = max(int((total_tx - _net_prev["total_tx"]) / dt), 0)
        for i in net["ifaces"]:
            p = _net_prev["ifaces"].get(i["iface"])
            if p:
                i["rx_bps"] = max(int((i["rx_bytes"] - p[0]) / dt), 0)
                i["tx_bps"] = max(int((i["tx_bytes"] - p[1]) / dt), 0)
    _net_prev = {
        "ts": now,
        "total_rx": sum(i["rx_bytes"] for i in net["ifaces"]),
        "total_tx": sum(i["tx_bytes"] for i in net["ifaces"]),
        "ifaces": {i["iface"]: (i["rx_bytes"], i["tx_bytes"]) for i in net["ifaces"]},
    }

    # 磁盘读写 B/s：与上次扇区差值 × 512
    io_bps = _diff_bps(diskstat, _io_prev, now, _last["ts"] if _last else None)
    _io_prev = diskstat
    if io_bps:
        for d in disks:
            bps = io_bps.get(d["name"])
            if bps:
                d["read_bps"], d["write_bps"] = bps[0] * 512, bps[1] * 512

    # 磁盘温度映射（有 drivetemp 才有）
    if disk_temps:
        for i, d in enumerate(disks):
            if i < len(disk_temps):
                d["temp_c"] = disk_temps[i]

    uptime_s = 0.0
    if lines.get("UP"):
        try:
            uptime_s = float(lines["UP"][0].split()[0])
        except (ValueError, IndexError):
            pass
    load1 = None
    if lines.get("LOAD"):
        try:
            load1 = float(lines["LOAD"][0].split()[0])
        except (ValueError, IndexError):
            pass

    # 趋势预测（规则版，AIOps"预计何时存满"的轻量实现）：
    # 维护近期样本环形缓冲，对卷用量做线性外推。样本够（>=10 次、跨 >=5 分钟）才输出。
    _hist.append({
        "ts": now,
        "mem": (mem or {}).get("percent"),
        "vols": {v["mount"]: v.get("percent") for v in vols if v.get("mount")},
    })
    if len(_hist) > _HIST_MAX:
        del _hist[: len(_hist) - _HIST_MAX]
    trends = _compute_trends()

    data = {
        "hostname": (lines.get("HOST") or [""])[0].strip() or "NAS",
        "uptime_s": uptime_s,
        "uptime": _fmt_uptime(uptime_s),
        "cpu": {"percent": cpu_percent, "load1": load1, "temp_c": cpu_temp},
        "mem": mem,
        "net": {"rx_bps": rx_bps, "tx_bps": tx_bps, "ifaces": net["ifaces"]},
        "volumes": vols,
        "trends": trends,
        "disks": disks,
        "fan": fan,
        "capabilities": {
            "cpu_percent": cpu_percent is not None,
            "cpu_temp": cpu_temp is not None,
            "mem": mem is not None,
            "net": bool(net["ifaces"]),           # 有网卡就显示网速行（首轮 -- 占位）
            "volumes": bool(vols),
            "disks": bool(disks),
            "disk_io": bool(io_bps),
            "disk_temp": bool(disk_temps),
            "fan": bool(fan and (fan.get("fan_rpm") or fan.get("cpu_fan_rpm"))),
        },
    }
    _last = {"ts": now, "data": data}
    return data
