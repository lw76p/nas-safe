"""
NAS Safe — 硬盘 SMART 健康采集（跨品牌，100% 只读）

设计原则（与 probe_smart.sh / metrics.py 同哲学，分层适配，绝不报错刷屏）：
  通道 A（通用，优先）：smartctl 跨品牌采集
      —— 参考业界最成熟的跨厂商方案 scrutiny(analogj/scrutiny)：
         · 设备发现用 `smartctl --scan -j`（JSON），自动枚举全盘 + 类型，
           不依赖 /dev 猜测；失败回退 /sys/block 枚举整盘。
         · 逐盘 `smartctl -a -j` 取结构化 JSON；JSON 优先，旧版 smartctl
           无 -j 时回退文本解析（-H -A -i）。
         · 健康判定：smart_status.passed + 关键属性（重映射5/待映射197/
           无法校正198）反推；smartctl 非零退出码多为告警位，JSON 可解析
           即用，不丢弃（只有「命令错/打不开设备」两位才致命）。
         · 用 wwn/serial 做稳定身份（供趋势去重）。
      · smartctl 路径按出现概率探测：标准路径 + 群晖 /usr/syno/bin。
      · 群晖/绿联/飞牛/OMV/Unraid/TrueNAS 走此通道开箱即用。
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
  · NVMe critical_warning>0 → fail
  · 待映射扇区 > 0          → fail（盘快不行了）
  · 离线无法校正 > 0        → fail
  · 重映射扇区 > 0          → warn（已出现坏道苗头）
  · 其余（温度/通电时长/CRC）只展示，不参与告警
"""

from __future__ import annotations

import json
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


def _empty_disk(name: str) -> dict:
    """所有解析函数共用的最小结构（前端/日报按 name/health 取数）。"""
    return {
        "name": name,
        "device": ("/dev/" + name) if name else None,
        "serial": None,
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
# 通道 A：smartctl 跨品牌采集（参考 scrutiny）
# ---------------------------------------------------------------------------

def _scan_devices(smartctl: str) -> list[tuple[str, str | None]]:
    """用 `smartctl --scan -j` 自动发现全盘（跨品牌最稳的方法，源自 scrutiny）。
    返回 [(设备名如 sda, 类型), ...]；扫描失败则回退 /sys/block 枚举整盘。"""
    devs: list[tuple[str, str | None]] = []
    try:
        out = _run_cmd(f"{smartctl} --scan -j 2>/dev/null")
        data = json.loads(out)
        for d in data.get("devices", []):
            name = (d.get("name") or "").replace("/dev/", "")
            if name:
                devs.append((name, d.get("type")))
    except Exception:
        pass
    if devs:
        return devs
    # 回退：/sys/block 整盘（排除分区与 loop/ram）
    try:
        out2 = _run_cmd("ls /sys/block 2>/dev/null")
        for n in out2.split():
            n = n.strip()
            if re.match(r"^(sd[a-z]+|nvme[0-9]+n[0-9]+|hd[a-z]|vd[a-z]|mmcblk[0-9]+)$", n):
                devs.append((n, None))
    except Exception:
        pass
    return devs


def _parse_smartctl_json(obj: dict, name: str) -> dict:
    """解析 smartctl -a -j 的 JSON 输出（scrutiny 同款结构化来源）。"""
    info = _empty_disk(name)
    dev = obj.get("device", {}) or {}
    info["model"] = obj.get("model_name")
    info["serial"] = obj.get("serial_number")
    if dev.get("name"):
        info["device"] = dev["name"]

    ss = obj.get("smart_status") or {}
    passed = ss.get("passed")
    temp = (obj.get("temperature") or {}).get("current")
    pot = (obj.get("power_on_time") or {}).get("hours")

    attrs: dict[str, int] = {}
    for a in (obj.get("ata_smart_attributes") or {}).get("table") or []:
        try:
            int(a.get("id"))
        except (TypeError, ValueError):
            continue
        raw = (a.get("raw") or {}).get("value")
        aname = a.get("name")
        if raw is not None and aname:
            attrs[aname] = raw
    info["attrs"] = attrs

    nvme = obj.get("nvme_smart_health_information_log")
    if nvme is not None:
        cw = nvme.get("critical_warning", 0) or 0
        used = nvme.get("percentage_used")
        if used is not None:
            info["life_left_pct"] = max(0, 100 - int(used))
        info["power_on_hours"] = nvme.get("power_on_hours")
        if temp is None:
            temp = nvme.get("temperature")  # NVMe 已是摄氏度
        info["reallocated"] = nvme.get("media_errors")
        info["health"] = "fail" if (cw and cw > 0) or passed is False else "ok"
    else:
        # ATA / SATA
        info["reallocated"] = attrs.get("Reallocated_Sector_Ct")
        info["pending"] = attrs.get("Current_Pending_Sector")
        ou = attrs.get("Offline_Uncorrectable")
        if ou is None:
            ou = attrs.get("Reported_Uncorrect")
        info["uncorrectable"] = ou
        info["power_on_hours"] = pot
        if temp is None:
            t = attrs.get("Temperature_Celsius")
            if t is not None:
                try:
                    temp = int(str(t).split()[0])
                except ValueError:
                    temp = None
        if passed is False:
            info["health"] = "fail"
        else:
            pend = info["pending"] or 0
            unc = info["uncorrectable"] or 0
            rea = info["reallocated"] or 0
            if pend > 0 or unc > 0:
                info["health"] = "fail"
            elif rea > 0:
                info["health"] = "warn"
            else:
                info["health"] = "ok"

    if temp is not None:
        info["temp_c"] = int(temp)
    return info


def _parse_block(text: str, name: str) -> dict:
    """smartctl 文本输出解析（旧版无 -j 时的回退）。"""
    info = _empty_disk(name)
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


def _parse_smartctl_block_or_json(body: str, name: str) -> dict:
    """优先按 JSON 解析（smartctl -j），失败回退到文本解析（旧版 smartctl）。"""
    body = body.strip()
    if body.startswith("{"):
        try:
            return _parse_smartctl_json(json.loads(body), name)
        except Exception:
            pass
    return _parse_block(body, name)


def _collect_smartctl(smartctl: str, devices: list[tuple[str, str | None]]) -> list[dict]:
    """通道 A：用 smartctl -a -j 逐盘采集（JSON 优先，文本回退）。单脚本批量跑。"""
    if not devices:
        return []
    lines = [f"SC={smartctl}"]
    for dev, dtype in devices:
        opt = f" --device {dtype}" if dtype and dtype not in ("ata", "sat", "scsi", "nvme") else ""
        lines.append(
            f'echo "===SMART /dev/{dev} ==="; '
            f'{smartctl} -a -j{opt} "/dev/{dev}" 2>&1; '
            f'echo "===END /dev/{dev} ==="'
        )
    script = "\n".join(lines)
    raw = _run_cmd(script)
    disks: list[dict] = []
    blocks = re.split(r"===SMART (/dev/\S+) ===", raw)
    for i in range(1, len(blocks), 2):
        dev = blocks[i]
        body = blocks[i + 1].split(f"===END {dev} ===")[0] if i + 1 < len(blocks) else ""
        name = dev.replace("/dev/", "")
        disks.append(_parse_smartctl_block_or_json(body, name))
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
    info = _empty_disk(None)
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
        # 通电时长：QTS 不同固件命名不一（Power-On_Hours / Power-On-Hours /
        # Power_On_Hours / Power On Hours），用「含 power 且含 hour」模糊匹配
        poh = None
        for k, v in attrs.items():
            if "power" in k.lower() and "hour" in k.lower() and isinstance(v, int):
                poh = v
                break
        info["power_on_hours"] = poh
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
        # 用 smartctl --scan -j 自动发现全盘（源自 scrutiny 的跨品牌方案），
        # 失败回退 /sys/block 枚举；逐盘 smartctl -a -j 解析（JSON 优先）。
        devices = _scan_devices(smartctl)
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
