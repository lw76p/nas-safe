"""版本能力矩阵（商业化抽象层）。

设计原则：
  1. 定价数字可以后调，但「版本 -> 能力」必须集中定义在一处。
     否则每加 / 改一个版本，都要到各处散落的 if 里改，必然漏。
  2. **免费版必须保留「能救命」的核心能力**（防勒索检测 + 快照 + 取回还原），
     且本机功能全部开放（自动快照/日报/清理/SMART 全不设卡）。
     只砍「多设备 / 迁移 / 自定义接口」，不砍安全性本身。
  3. 本项目开源：付费卖的是「省事 + 支持 + 服务」（一键部署、持续更新、
     微信推送中继），不是功能锁。升级文案要坦诚说明这一点。

档位（2026-10-02 与用户定稿）：免费版（0 元）/ 家庭版（8 元）/ 专业版（18 元）。
价格在升级页实时显示（服务端 /api/license/prices，state/prices.json 可覆盖默认值）。

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
    # 免费版（0 元）：本机功能全开，可管 2 台设备（本机 + 1 台，尝到总控台甜头）
    # ------------------------------------------------------------------
    "free": {
        "label": "免费版",
        "price": 0,
        "max_devices": 2,                       # 本机 + 1 台，体验跨设备管理
        "max_snapshots": UNLIMITED,             # 本机功能全开，快照不限量
        "auto_snapshot_intervals": ["hourly", "daily", "weekly"],
        "console": "basic",                     # 无完整跨设备控制台拓扑
        "alerts": ["email", "wechat"],          # 邮件 + 微信服务号推送
        "custom_alerts": False,                 # 无自定义通知接口
        "ai_cloud_sources": 3,                  # 3 种常见云端 AI
        "ai_local": False,                      # 不扫本地模型
        "ai_custom_key": False,                 # 不能自定义 API 接入点
        "ai_quota": 20,                         # 每月 AI 调用 20 次（问答+管家共用）
        "remote_devices": False,                # 不支持异地组网设备
        "migrate": False,                       # 无一键换机迁移
        "smart_history": True,                  # 本机 SMART 历史属本机功能，全开
        # 核心能力不设卡（安全能力免费档同样具备）
        "ransomware_detect": True,
        "snapshot": True,
        "restore": True,
    },
    # ------------------------------------------------------------------
    # 家庭版（8 元）：多设备 + 换机迁移 + 全部常见云端 AI + 本地模型
    # ------------------------------------------------------------------
    "home": {
        "label": "家庭版",
        "price": 8,
        "max_devices": 5,
        "max_snapshots": UNLIMITED,
        "auto_snapshot_intervals": ["hourly", "daily", "weekly"],
        "console": "full",                      # 完整跨设备控制台
        "alerts": ["email", "wechat"],
        "custom_alerts": False,
        "ai_cloud_sources": UNLIMITED,          # 内置的常见云端 AI 全给
        "ai_local": True,                       # 自动扫描本地 AI 模型并加入
        "ai_custom_key": False,
        "ai_quota": UNLIMITED,                  # 家庭版 AI 不限量
        "remote_devices": False,
        "migrate": True,                        # 一键备份 / 换机迁移
        "smart_history": True,
        "ransomware_detect": True,
        "snapshot": True,
        "restore": True,
    },
    # ------------------------------------------------------------------
    # 专业版（18 元）：设备不限 + 异地组网 + 自定义通知/AI 接口
    # ------------------------------------------------------------------
    "business": {
        "label": "专业版",
        "price": 18,
        "max_devices": UNLIMITED,
        "max_snapshots": UNLIMITED,
        "auto_snapshot_intervals": ["hourly", "daily", "weekly"],
        "console": "full",
        "alerts": ["email", "wechat", "webhook", "bark", "ntfy", "feishu", "custom"],
        "custom_alerts": True,                  # 自行输入接口并推送
        "ai_cloud_sources": UNLIMITED,
        "ai_local": True,
        "ai_custom_key": True,                  # 自定义 AI API 接入点随意接
        "ai_quota": UNLIMITED,                  # 专业版 AI 不限量
        "remote_devices": True,                 # 局域网 + 异地组网设备同时管
        "migrate": True,
        "smart_history": True,
        "ransomware_detect": True,
        "snapshot": True,
        "restore": True,
    },
}

# 授权层（licensing.py）已接入：无授权文件时就是免费版。
# 免费版保留全部「能救命」的核心能力与本机功能，砍的是多设备/迁移/自定义接口。
DEFAULT_EDITION = "free"

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
        "price": lim.get("price", 0),
        "licensed": os.path.exists(os.path.join(_state_dir(), _LICENSE_FILE)),
        "max_devices": lim.get("max_devices", UNLIMITED),
        "can_migrate": bool(lim.get("migrate")),
        "console": lim.get("console", "full"),
    }
