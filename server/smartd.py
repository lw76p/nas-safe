"""
NAS Safe — 硬盘 SMART 健康采集（跨品牌，100% 只读）

设计原则（与 probe_smart.sh / metrics.py 同哲学，分层适配，绝不报错刷屏）：
  通道 A（通用，优先）：smartctl 多路径探测 → `smartctl -H -A -i /dev/X`
      · 标准路径 /usr/sbin|/usr/bin|/usr/local/sbin|/usr/local/bin
      · 群晖  /usr/syno/bin/smartctl、/usr/syno/sbin/smartctl
      · 绿联/飞牛/OMV/Unraid/TrueNAS：标准路径即可
      · QNAP 装了 Smartmontools QPKG 后，能在 .qpkg 目录找到
  通道 B（QTS 专用，无需装包）：威联通系统自身每分钟在
      /tmp/smart/smart_*.info 落盘的 SMART 文本（CSV，由 nasutil/
      get_hd_smartinfo 写入），经 sudo 读取（目录权限 0700，普通账号
      读不到；目录内文件 0644/0666 世界可读）；槽位→设备映射用
      /proc/partitions（注意其非严格按次设备号排序）。

每个字段都可缺省；capabilities 告诉前端「这台机器有没有 SMART 能力」——
没有就显示「装 Smartmontools 即可开启」，而不是报错。

阈值判读（只盯真正预示坏盘的指标，避免误报）：
  · 整体健康 PASSED         → ok
  · 整体健康 FAILED         → fail
  · 待映射扇区 > 0          → fail（盘快不行了）
  · 离线无法校正 > 0        → fail
  · 重映射扇区 > 0          → warn（已出现坏道苗头）
  · 其余（温度/通电时长/CRC）只展示，不参与告警
"""

from __future__ import annotations

import re
import time

# 结果缓存（SMART 采集慢，5 分钟一次足够）
_CACHE_TTL = 300.0
_last: dict | None = None


# smartctl 候选路径（按出现概率排序；第一个存在的即用）
_SMARTCTL_PATHS = [
    "smartctl",                      # 依赖 PATH
    "/usr/sbin/smartctl",
    "/usr/bin/smartctl",
    "/usr/local/sbin/smartctl",
    "/usr/local/bin/smartctl",
    "/usr/syno/bin/smartctl",        # 群晖
    "/usr/syno/sbin/smartctl",       # 群晖
    "/opt/bin/smartctl",
]

# 关键 SMART 属性 ID → (字段名, 中文名, 性质)
# 性质：fail=坏盘信号 / warn=预警 / info=展示
_ATTRS = {
    5:   ("reallocated_sector_count", "重映射扇区", "warn"),
    9:   ("power_on_hours", "通电时长(小时)", "info"),
    187: ("reported_uncorrect", "无法校正错误", "fail"),
    188: ("command_timeout", "指令超时", "warn"),
    197: ("current_pending_sector", "待映射扇区", "fail"),
    198: ("offline_uncorrectable", "离线无法校正", "fail"),
    199: ("udma_crc_errors", "CRC 错误", "warn"),
    190: ("temp_c", "温度(°C)", "info"),
    194: ("temp_c", "温度(°C)", "info"),
    231: ("life_left_pct", "剩余寿命(%)", "info"),
    233: ("nand_writes", "写入量", "info"),
}


# ---------------------------------------------------------------------------
# 执行（复用 qnap 客户端通道：SSH 远程或本机；与 metrics 一致）
# ---------------------------------------------------------------------------

def _run_cmd(script: str) -> str:
    try:
        from qnap import default_client
        return default_client().run_shell(script)
    except Exception:
        pass
    import subprocess

    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=120
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception:
        return ""


def _detect_smartctl() -> str | None:
    """返回可用的 smartctl 路径；找不到返回 None。"""
    script = (
        "p=''; "
        "for c in " + " ".join(_SMARTCTL_PATHS[1:]) + "; do "
        "[ -x \"$c\" ] && { p=\"$c\"; break; }; done; "
        "[ -z \"$p\" ] && p=$(command -v smartctl 2>/dev/null); "
        "[ -n \"$p\" ] && echo \"$p\" || echo NONE"
    )
    out = _run_cmd(script).strip()
    if out and out != "NONE":
        # 取最后一行（command -v 可能带版本头）
        for line in reversed(out.splitlines()):
            line = line.strip()
            if "smartctl" in line:
                return line
    return None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _parse_block(text: str, name: str) -> dict:
    info = {
        "name": name,
        "health": "unknown",            # ok / warn / fail / unknown
        "model": None,
        "temp_c": None,
        "power_on_hours": None,
        "reallocated": None,
        "pending": None,
        "uncorrectable": None,
        "life_left_pct": None,
        "attrs": {},
    }
    # 整体健康
    m = re.search(r"overall-health self-assessment test result:\s*(\w+)", text, re.I)
    if m:
        info["health"] = "ok" if m.group(1).upper() == "PASSED" else "fail"
    else:
        m2 = re.search(r"SMART Health Status:\s*(\w+)", text, re.I)
        if m2:
            v = m2.group(1).upper()
            info["health"] = "ok" if ("OK" in v or "PASSED" in v or "GOOD" in v) else "fail"

    # 型号
    for pat in (r"Device Model:\s*(.+)", r"Model Number:\s*(.+)",
                r"Product:\s*(.+)", r"Model:\s*(.+)"):
        mm = re.search(pat, text)
        if mm:
            info["model"] = mm.group(1).strip()
            break

    # 属性表 + NVMe 关键行
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 10 and parts[0].isdigit():
            aid = int(parts[0])
            if aid in _ATTRS:
                key = _ATTRS[aid][0]
                try:
                    val = int(parts[-1])
                except ValueError:
                    val = None
                if val is not None:
                    info["attrs"][key] = val
                    if key == "temp_c" and info["temp_c"] is None:
                        info["temp_c"] = val
                    elif key == "power_on_hours":
                        info["power_on_hours"] = val
                    elif key == "reallocated_sector_count":
                        info["reallocated"] = val
                    elif key == "current_pending_sector":
                        info["pending"] = val
                    elif key == "offline_uncorrectable":
                        info["uncorrectable"] = val
                    elif key == "life_left_pct" and info["life_left_pct"] is None:
                        info["life_left_pct"] = val
        # NVMe 的温度 / 通电时长行（smartctl 对 nvme 输出不同）
        mt = re.search(r"Temperature:\s*(\d+)\s*C?", line, re.I)
        if mt and info["temp_c"] is None:
            info["temp_c"] = int(mt.group(1))
        mp = re.search(r"Power On Hours:\s*(\d+)", line, re.I)
        if mp and info["power_on_hours"] is None:
            info["power_on_hours"] = int(mp.group(1))
        ml = re.search(r"Percentage Used:\s*(\d+)", line, re.I)
        if ml and info["life_left_pct"] is None:
            info["life_left_pct"] = max(0, 100 - int(ml.group(1)))

    # 没拿到整体结论时，用关键属性反推
    if info["health"] == "unknown":
        if (info["pending"] or 0) > 0 or (info["uncorrectable"] or 0) > 0:
            info["health"] = "fail"
        elif (info["reallocated"] or 0) > 0:
            info["health"] = "warn"
    return info


def _collect_smartctl(smartctl: str, devices: list[str]) -> list[dict]:
    """通道 A：一条批量脚本，单次 SSH 往返，逐盘采集。"""
    dev_list = " ".join(f'"{d}"' for d in devices)
    script = f"""
SC={smartctl}
for dev in {dev_list}; do
  echo "===SMART $dev ==="
  $SC -H -A -i "$dev" 2>&1
  echo "===END $dev ==="
done
"""
    raw = _run_cmd(script)
    disks: list[dict] = []
    blocks = re.split(r"===SMART (/dev/\S+) ===", raw)
    # blocks[0] 是前缀；之后每两项：(设备名, 内容)
    for i in range(1, len(blocks), 2):
        dev = blocks[i]
        body = blocks[i + 1].split(f"===END {dev} ===")[0] if i + 1 < len(blocks) else ""
        name = dev.replace("/dev/", "")
        if "NO_SMARTCTL" in body or not body.strip():
            disks.append({"name": name, "health": "unknown", "model": None,
                          "temp_c": None, "power_on_hours": None,
                          "reallocated": None, "pending": None,
                          "uncorrectable": None, "life_left_pct": None, "attrs": {}})
            continue
        disks.append(_parse_block(body, name))
    return disks


def _is_qts() -> bool:
    """是否威联通 QTS 系统（读 /etc/os-release，无需提权）。"""
    out = _run_cmd("cat /etc/os-release 2>/dev/null")
    return ("ID=qts" in out) or ('NAME="QTS"' in out)


def _collect_qts_native() -> list[dict]:
    """通道 B（QTS 原生，优先于装 QPKG）：威联通系统自己每分钟在
    /tmp/smart/smart_*.info 落盘的 SMART 文本（CSV）。

    文件名形如 smart_<控制器编号>_<槽位>.info；绝大多数单控制器机型
    （TS 全系桌面/企业款）控制器编号恒为 0 → smart_0_N.info。扩展柜 /
    双控制器机型可能出现 smart_1_N.info，glob 已放宽捕获，但跨控制器
    的槽位→设备精确映射属已知限制（见 _parse_qts）。

    该目录权限 0700（admin），普通管理员账号读不到，必须 sudo；
    目录内文件本身 0644/0666（世界可读），sudo 穿过目录即可直读。
    槽位→设备 映射：/proc/partitions 里 nvme*/sd* 整盘的有序序号
    （QTS 编号 = 内核枚举序；注意 /proc/partitions 非严格按次设备号
    排序，已实测 sdh 可排在 sdg 前，属正常）。

    返回与 _parse_block 同 schema 的磁盘列表（带 device/name）。"""
    from qnap import default_client

    client = default_client()
    try:
        script = (
            "echo ===PARTITIONS===; "
            "grep -E ' sd[a-z]+$| nvme[0-9]+n1$' /proc/partitions; "
            "echo ===SMART===; "
            "for f in /tmp/smart/smart_*.info; do "
            "  [ -f \"$f\" ] || continue; "
            "  echo \"##FILE:$(basename \"$f\")\"; "
            "  cat \"$f\"; "
            "  echo; "
            "done"
        )
        out = client.run_privileged(script)
    except Exception:
        out = ""
    finally:
        client.close()

    return _parse_qts(out)


def _parse_qts_block(body: str) -> dict:
    """解析单块盘的 CSV（QTS smart_0_N.info 格式）。

    NVMe 示例首行：15,4,0,0,0,0,0,0,37,0x0000,-1,-1,0  → 第9列=温度(℃)
    属性行（7 列）：ID,NAME,VALUE,WORST,THRESH,RAW,FLAG
    NVMe 关键：Critial Warning / Percentage Used / Composite Temperature
    SATA 关键：Retired_Block_Count / Current_Pending_Sector /
              Uncorrectable_Sector_Count / Reallocated_Event_Count
    """
    info = {
        "name": None,
        "device": None,
        "health": "unknown",
        "model": None,
        "temp_c": None,
        "power_on_hours": None,
        "reallocated": None,
        "pending": None,
        "uncorrectable": None,
        "life_left_pct": None,
        "attrs": {},
    }
    lines = [l for l in body.splitlines() if l.strip()]
    if not lines:
        return info

    # 首行 header：第 9 列（index 8）为温度 ℃
    header = lines[0].split(",")
    if len(header) > 8:
        try:
            info["temp_c"] = int(header[8])
        except ValueError:
            pass

    attrs: dict[str, int] = {}
    for line in lines[1:]:
        p = line.split(",")
        if len(p) >= 6 and p[0].isdigit():
            name = p[1].strip()
            try:
                raw = int(p[5])
            except ValueError:
                raw = None
            if name and raw is not None:
                attrs[name] = raw
    info["attrs"] = attrs

    is_nvme = (
        "Composite Temperature" in attrs
        or "Percentage Used" in attrs
        or "Critial Warning" in attrs
    )

    if is_nvme:
        cw = attrs.get("Critial Warning", 0) or 0
        used = attrs.get("Percentage Used")
        if used is not None:
            info["life_left_pct"] = max(0, 100 - used)
        info["power_on_hours"] = attrs.get("Power On Hours")
        ct = attrs.get("Composite Temperature")
        if ct is not None:
            info["temp_c"] = ct - 273  # NVMe 温度单位为开尔文
        info["health"] = "fail" if cw > 0 else "ok"
    else:
        info["reallocated"] = attrs.get("Retired_Block_Count")
        info["pending"] = attrs.get("Current_Pending_Sector")
        info["uncorrectable"] = attrs.get("Uncorrectable_Sector_Count")
        evt = attrs.get("Reallocated_Event_Count")
        info["power_on_hours"] = attrs.get("Power-On-Hours")
        pend = info["pending"] or 0
        unc = info["uncorrectable"] or 0
        rea = info["reallocated"] or 0
        if pend > 0 or unc > 0:
            info["health"] = "fail"
        elif rea > 0 or (evt or 0) > 0:
            info["health"] = "warn"
        else:
            info["health"] = "ok"
    return info


def _parse_qts(out: str) -> list[dict]:
    """从 run_privileged 输出中切出分区序 + 各盘 SMART，完成槽位→设备映射。"""
    disks: list[dict] = []

    # 分区序（整盘：nvme*/sd*），顺序即 QTS 槽位序
    dev_order: list[str] = []
    pm = re.search(r"===PARTITIONS===(.*?)===SMART===", out, re.S)
    if pm:
        for line in pm.group(1).splitlines():
            toks = line.split()
            if toks and re.match(r"^(sd[a-z]+|nvme[0-9]+n1)$", toks[-1]):
                dev_order.append(toks[-1])

    # 各盘 SMART（按 ##FILE:smart_<控制器>_<槽位>.info 切分）
    sm = re.search(r"===SMART===(.*)$", out, re.S)
    smart_section = sm.group(1) if sm else ""
    files = re.split(r"##FILE:(\S+)", smart_section)
    slot_data: dict[int, dict] = {}
    for i in range(1, len(files), 2):
        fname = files[i]
        body = files[i + 1] if i + 1 < len(files) else ""
        # 容错控制器前缀（smart_0_1 / smart_1_1 都取末尾槽位号）
        mm = re.search(r"smart_(?:\d+_)?(\d+)\.info", fname)
        slot = int(mm.group(1)) if mm else None
        slot_data[slot] = _parse_qts_block(body)

    for slot, info in slot_data.items():
        if slot and 1 <= slot <= len(dev_order):
            dev = dev_order[slot - 1]
            info["device"] = "/dev/" + dev
            info["name"] = dev
        else:
            info["name"] = f"disk{slot}" if slot else None
            info["device"] = None
        disks.append(info)
    return disks


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def collect(force: bool = False) -> dict:
    """采集全部硬盘 SMART 健康。返回结构化 dict，任一环节失败都优雅降级。"""
    global _last
    now = time.time()
    if not force and _last and now - _last["ts"] < _CACHE_TTL:
        return _last["data"]

    smartctl = _detect_smartctl()
    available = bool(smartctl)
    platform_hint = ""

    disks: list[dict] = []
    if smartctl:
        # 通道 A：smartctl 多路径（群晖/绿联/飞牛/通用 Linux 开箱即用）
        # 复用 metrics 的磁盘清单（/sys/block），拿到设备名
        try:
            import metrics  # 延迟导入，避免循环
            m = metrics.collect()
            names = [d["name"] for d in (m.get("disks") or []) if d and d.get("name")]
        except Exception:
            names = []
        if not names:
            # 兜底：直接用 /dev 下常见盘符
            names = [f"sd{x}" for x in "abcdefghijklmnop"] + \
                    [f"nvme{n}n1" for n in range(0, 4)]
        devices = [f"/dev/{n}" for n in names]
        disks = _collect_smartctl(smartctl, devices)
    elif _is_qts():
        # 通道 B：QTS 原生（威联通无需装任何 QPKG，系统自维护 /tmp/smart 即可）
        disks = _collect_qts_native()
        available = bool(disks)
        if not available:
            platform_hint = "威联通(QNAP) 未取到 SMART 数据，请确认系统 SMART 服务已开启"
    else:
        platform_hint = "未检测到 smartctl；群晖/绿联/飞牛/通用 Linux 开箱即用，可安装 Smartmontools 后自动开启"

    # 汇总评级
    grades = {"ok": 0, "warn": 1, "fail": 2, "unknown": 0}
    worst = 0
    for d in disks:
        worst = max(worst, grades.get(d.get("health", "unknown"), 0))
    health_label = {0: "正常", 1: "注意", 2: "异常"}[worst]

    counts = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
    for d in disks:
        counts[d.get("health", "unknown")] = counts.get(d.get("health", "unknown"), 0) + 1

    data = {
        "available": available,
        "smartctl": smartctl,
        "platform_hint": platform_hint,
        "disk_count": len(disks),
        "health_label": health_label,
        "worst": worst,
        "counts": counts,
        "disks": disks,
    }
    _last = {"ts": now, "data": data}
    return data


def summary_line() -> str:
    """给日报用的一句话（仅展示，不影响降级）。"""
    d = collect()
    if not d["available"]:
        return ""
    c = d["counts"]
    parts = [f"{d['disk_count']} 块盘，{c.get('ok',0)} 块正常"]
    if c.get("warn"):
        parts.append(f"{c['warn']} 块需注意（出现过坏道苗头）")
    if c.get("fail"):
        parts.append(f"{c['fail']} 块异常（建议尽快备份并换盘）")
    return "；".join(parts) + "。"
