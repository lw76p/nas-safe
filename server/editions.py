"""版本能力矩阵（商业化抽象层）。

设计原则：
  1. 定价数字可以后调，但「版本 -> 能力」必须集中定义在一处。
     否则每加 / 改一个版本，都要到各处散落的 if 里改，必然漏。
  2. **免费档必须保留「能救命」的核心能力**（防勒索检测 + 快照 + 取回还原）。
     只砍「规模 / 自动化 / 便利 / 增值」，不砍安全性本身 ——
     否则免费用户真中毒时救不回来，砸的是招牌，也永远体验不到价值。
  3. 现阶段还没接授权模块，默认版本给完整能力（DEFAULT_EDITION="home"），
     对现有运行零影响；接入授权后改为读 state/license.json。

用法：
    from editions import limits, can, check_devices
    if not can("migrate"):  -> 提示升级
    ok, msg = check_devices(len(devices))
"""
from __future__ import annotations

import json
import os

# -1 表示不限制
UNLIMITED = -1

EDITIONS: dict = {
    # ------------------------------------------------------------------
    # 免费档：1 台电脑 + 1 台 NAS，核心防勒索能力全给
    # 砍的是「保留几份 / 多久自动存一次 / 能不能管多台 / 有没有推送告警」
    # ------------------------------------------------------------------
    "free": {
        "label": "免费版",
        "max_devices": 2,                       # 1 台电脑 + 1 台 NAS
        "max_snapshots": 3,                     # 每卷只保留最近 3 份快照
        "auto_snapshot_intervals": ["daily"],   # 只能每天自动存一次
        "console": "basic",                     # 无跨设备总控台拓扑
        "alerts": ["email"],                    # 仅邮件告警
        "migrate": False,                       # 无一键换机迁移
        "smart_history": False,                 # 无硬盘健康历史趋势
        # 以下核心能力免费档同样具备（安全能力不设卡）
        "ransomware_detect": True,
        "snapshot": True,
        "restore": True,
    },
    # ------------------------------------------------------------------
    # 家庭版：功能齐全，限制设备数
    # ------------------------------------------------------------------
    "home": {
        "label": "家庭版",
        "max_devices": 3,
        "max_snapshots": UNLIMITED,
        "auto_snapshot_intervals": ["hourly", "daily", "weekly"],
        "console": "full",                      # 完整跨设备总控台
        "alerts": ["email", "wechat"],
        "migrate": True,
        "smart_history": True,
        "ransomware_detect": True,
        "snapshot": True,
        "restore": True,
    },
    # ------------------------------------------------------------------
    # 企业版：没有任何限制
    # ------------------------------------------------------------------
    "business": {
        "label": "企业版",
        "max_devices": UNLIMITED,
        "max_snapshots": UNLIMITED,
        "auto_snapshot_intervals": ["hourly", "daily", "weekly"],
        "console": "full",
        "alerts": ["email", "wechat", "webhook"],
        "migrate": True,
        "smart_history": True,
        "ransomware_detect": True,
        "snapshot": True,
        "restore": True,
    },
}

# 现阶段未接入授权，默认给完整能力（不影响既有部署行为）
DEFAULT_EDITION = "home"

_LICENSE_FILE = "license.json"


def _state_dir() -> str:
    try:
        from storage import state_dir
        return state_dir()
    except Exception:
        return os.path.join(os.getcwd(), "state")


def get_edition() -> str:
    """当前生效的版本。读不到授权文件时回落到默认版本。"""
    path = os.path.join(_state_dir(), _LICENSE_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        ed = (data.get("edition") or "").strip()
        if ed in EDITIONS:
            return ed
    except Exception:
        pass
    return DEFAULT_EDITION


def limits(edition: str = "") -> dict:
    """取某版本（默认当前版本）的能力字典。"""
    return EDITIONS.get(edition or get_edition(), EDITIONS[DEFAULT_EDITION])


def can(feature: str, edition: str = "") -> bool:
    """该版本是否具备某项能力。未知能力一律放行，避免误伤现有功能。"""
    return bool(limits(edition).get(feature, True))


def check_devices(count: int, edition: str = "") -> tuple[bool, str]:
    """设备数是否超出版限制。返回 (是否允许, 提示文案)。"""
    lim = limits(edition)
    maxd = lim.get("max_devices", UNLIMITED)
    if maxd == UNLIMITED or count <= maxd:
        return True, ""
    return False, (
        f"当前版本（{lim.get('label', '')}）最多管理 {maxd} 台设备，"
        f"已添加 {count} 台。升级后可继续添加。"
    )


def check_snapshots(count: int, edition: str = "") -> tuple[bool, str]:
    """某卷的快照份数是否超出版限制（超出时应清理最旧的）。"""
    lim = limits(edition)
    maxs = lim.get("max_snapshots", UNLIMITED)
    if maxs == UNLIMITED or count <= maxs:
        return True, ""
    return False, (
        f"当前版本（{lim.get('label', '')}）每卷最多保留 {maxs} 份快照，"
        f"最旧的 {count - maxs} 份需要清理。升级后不再受限。"
    )


def summary() -> dict:
    """给 UI / API 用的版本概览。"""
    ed = get_edition()
    lim = limits(ed)
    return {
        "edition": ed,
        "label": lim.get("label", ""),
        "licensed": os.path.exists(os.path.join(_state_dir(), _LICENSE_FILE)),
        "max_devices": lim.get("max_devices", UNLIMITED),
        "can_migrate": bool(lim.get("migrate")),
        "console": lim.get("console", "full"),
    }
