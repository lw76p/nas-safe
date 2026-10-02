"""跨品牌 NAS 能力探测（只读、容错）。

NAS Safe 要装到所有品牌 / 系统，从安装到彻底能用，第一步是知道自己跑在什么上面、能做什么。
本模块只做「探测 + 画像」，绝不修改任何系统状态。任何探测失败都安全降级为 unknown / none，
绝不抛异常中断主流程。

品牌标识（brand）:
    qnap      威联通 QTS
    synology  群晖 DSM
    ugreen    绿联 UGOS Pro
    feiniu    飞牛 fnOS
    truenas   TrueNAS (SCALE/CORE)
    omv       OpenMediaVault
    unraid    Unraid
    generic_linux  通用 Linux / 其它 / 容器内无法识别

能力画像（capabilities）:
    smart_backend     SMART 后端: qts_native | smartctl | synology | none
    snapshot_backend  快照后端: qnap_volume | btrfs | zfs | none | auto
    filesystem        文件系统: ext4 | btrfs | zfs | xfs | unknown
    docker            是否支持 Docker（决定部署方式）
    share_hints       该品牌典型共享根路径（迁移路径映射默认提示）
    notes             白话说明（前端可直接展示）
"""

from __future__ import annotations

import os

# 品牌中文标签（界面展示用，全部白话）
BRAND_LABELS = {
    "qnap": "威联通 QNAP",
    "synology": "群晖 Synology",
    "ugreen": "绿联 UGOS",
    "feiniu": "飞牛 fnOS",
    "truenas": "TrueNAS",
    "omv": "OpenMediaVault",
    "unraid": "Unraid",
    "aliyun": "阿里云 ECS",
    "generic_linux": "通用 Linux",
    "unknown": "未知设备",
}

# 各品牌典型共享根路径（迁移路径映射的默认提示；按优先级排列）
BRAND_SHARE_HINTS = {
    "qnap": ["/share/CACHEDEV1_DATA", "/share/CACHEDEV2_DATA", "/share"],
    "synology": ["/volume1", "/volume2"],
    "ugreen": ["/volume1", "/volume2"],
    "feiniu": ["/vol1", "/vol2"],
    "truenas": ["/mnt"],
    "omv": ["/srv/dev-disk-by-uuid-"],
    "unraid": ["/mnt/user", "/mnt/disk1"],
    "generic_linux": ["/mnt", "/data", "/srv"],
    "unknown": ["/"],
}


def _state_dir() -> str:
    """复用 storage.state_dir()；storage 不可用时退化为 /app/data。"""
    try:
        import storage
        return storage.state_dir()
    except Exception:
        return "/app/data"


def _read_os_release() -> str:
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="ignore") as f:
            return f.read().lower()
    except OSError:
        return ""


def _which(name: str):
    """像 shell which 一样找可执行文件，找不到返回 None。"""
    try:
        from shutil import which as _w
        return _w(name)
    except Exception:
        # 极老环境没有 shutil.which，退化为 PATH 遍历
        for d in (os.environ.get("PATH") or "").split(os.pathsep):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None


def detect_brand() -> str:
    """识别当前运行环境的 NAS 品牌。

    优先级：环境变量 NASSAFE_BRAND（部署时可显式指定）> /etc/os-release >
    已知文件标记 > 容器/通用 Linux。任何一步失败都安全返回 generic_linux。
    """
    # 1) 部署者显式指定（最可靠，尤其容器场景宿主与容器 OS 不同）
    env_brand = (os.environ.get("NASSAFE_BRAND") or "").strip().lower()
    if env_brand in BRAND_LABELS and env_brand != "unknown":
        return env_brand

    # 1.5) 部署脚本打标的品牌文件（容器部署时由 deploy 脚本 SSH 探测宿主后写入，
    #      用于解决「容器内的 os-release 是通用 Linux、看不到宿主 QTS 标记」的误判）。
    #      读本地文件零成本，不会触发 SSH 风暴。
    for stamp in ("/app/.nassafe_brand", "/.nassafe_brand",
                  os.path.join(_state_dir(), ".nassafe_brand")):
        try:
            if os.path.exists(stamp):
                with open(stamp, "r", encoding="utf-8", errors="ignore") as f:
                    b = f.read().strip().lower()
                if b in BRAND_LABELS and b != "unknown":
                    return b
        except OSError:
            continue

    # 2) /etc/os-release 关键字
    rel = _read_os_release()
    markers = (
        ("qnap", ("qnap", "qts")),
        ("synology", ("synology", "dsm")),
        ("ugreen", ("ugreen", "ugos")),
        ("feiniu", ("feiniu", "fnos")),
        ("truenas", ("truenas", "freenas")),
        ("omv", ("openmediavault", "omv")),
        ("unraid", ("unraid",)),
    )
    for brand, keys in markers:
        if any(k in rel for k in keys):
            return brand

    # 3) 已知文件 / 目录标记
    if os.path.exists("/usr/syno"):
        return "synology"
    if os.path.exists("/etc/config/qpkg.conf"):
        return "qnap"
    if os.path.exists("/etc/ugreen") or os.path.exists("/usr/bin/ugos"):
        return "ugreen"

    # 3.5) 阿里云 ECS（元数据探测；非 ECS 环境读不到，快速降级为通用 Linux）
    try:
        from aliyun_ecs import is_ecs
        if is_ecs():
            return "aliyun"
    except Exception:
        pass

    # 4) 容器或通用 Linux
    return "generic_linux"


def detect_capabilities(brand: str | None = None) -> dict:
    """返回能力画像。brand 为 None 时自动探测。

    所有分支都安全：探测不到就降级为 none / unknown，不抛异常。
    """
    brand = brand or detect_brand()
    caps = {
        "brand": brand,
        "brand_label": BRAND_LABELS.get(brand, brand),
        "smart_backend": "none",
        "snapshot_backend": "none",
        "filesystem": "unknown",
        "docker": False,
        "share_hints": BRAND_SHARE_HINTS.get(brand, BRAND_SHARE_HINTS["unknown"]),
        "notes": [],
    }

    # ---- SMART 后端 ----
    if brand == "qnap":
        # 威联通无 smartctl，SMART 走系统原生接口（qsmart.cgi / /tmp/smart）
        caps["smart_backend"] = "qts_native"
        caps["notes"].append("威联通没有 smartctl，硬盘健康走系统原生接口，无需额外安装")
    elif brand == "synology":
        # 群晖自带 /usr/syno/bin/smartctl
        caps["smart_backend"] = "synology"
        caps["notes"].append("群晖自带 smartctl，开箱即用")
    else:
        # 其它品牌默认尝试通用 smartctl（缺失时前端提示安装 smartmontools）
        caps["smart_backend"] = "smartctl"
        if _which("smartctl"):
            caps["notes"].append("已检测到 smartctl，硬盘健康开箱即用")
        else:
            caps["notes"].append("未检测到 smartctl，安装 smartmontools 后即开启硬盘健康（Debian/Ubuntu: apt install smartmontools）")

    # ---- 快照后端 + 文件系统 ----
    if brand == "qnap":
        caps["snapshot_backend"] = "qnap_volume"
        caps["filesystem"] = "ext4"
        caps["notes"].append("威联通用卷快照（qcli_volumesnapshot），自动加 vital 永久锁")
    elif brand == "synology":
        caps["snapshot_backend"] = "btrfs"
        caps["filesystem"] = "btrfs"
        caps["notes"].append("群晖 btrfs 子卷快照，需存储池为 btrfs 且已购快照套件/支持")
    elif brand in ("ugreen", "feiniu"):
        caps["snapshot_backend"] = "btrfs"
        caps["filesystem"] = "btrfs"
        caps["notes"].append("绿联/飞牛默认 btrfs，可用子卷快照")
    elif brand == "truenas":
        caps["snapshot_backend"] = "zfs"
        caps["filesystem"] = "zfs"
        caps["notes"].append("TrueNAS 用 ZFS 快照，支持递归数据集快照")
    elif brand == "unraid":
        caps["snapshot_backend"] = "none"
        caps["filesystem"] = "xfs"
        caps["notes"].append("Unraid 默认 XFS 阵列无原生快照；可用 ZFS 格式盘或 rsync 备份兜底")
    elif brand == "omv":
        caps["snapshot_backend"] = "auto"
        caps["filesystem"] = "unknown"
        caps["notes"].append("OMV 取决于存储配置（LVM/btrfs/ZFS），安装后自动探测")
    elif brand == "aliyun":
        caps["snapshot_backend"] = "aliyun_ecs"
        caps["filesystem"] = "cloud_disk"
        try:
            from aliyun_ecs import load_credentials
            creds = load_credentials()
            if creds["protector"]:
                msg = ("阿里云 ECS 云盘快照：已配置保护凭证，自动快照走「创建专用」"
                       "密钥、无删除权限（不可删防勒索）。")
                msg += ("已配置管理凭证，可受控清理。" if creds["manager"]
                        else "未配置管理凭证：历史快照默认不可删，清理需单独授权。")
                caps["notes"].append(msg)
            else:
                caps["notes"].append(
                    "阿里云 ECS 云盘快照：未配置 AccessKey。请在环境变量或 "
                    ".aliyun_ecs.json 配置保护凭证（PROTECTOR）后开启。"
                )
        except Exception:
            caps["notes"].append("阿里云 ECS 云盘快照：凭证检测失败")
        caps["notes"].append("云盘快照为块级，单文件取回需从快照创建云盘后挂载。")
    else:
        caps["snapshot_backend"] = "auto"
        caps["filesystem"] = "unknown"
        caps["notes"].append("通用 Linux：若文件系统为 btrfs/ZFS 则可用原生快照，否则用 rsync 兜底")

    # ---- Docker 支持（决定部署方式）----
    caps["docker"] = (_which("docker") is not None) or os.path.exists("/.dockerenv")
    if not caps["docker"]:
        caps["notes"].append("未检测到 Docker，可用本机脚本直接运行（Python 3 即可）")

    return caps


def capability_summary(brand: str | None = None) -> dict:
    """给前端用的精简画像（同 detect_capabilities，便于统一调用）。"""
    return detect_capabilities(brand)
