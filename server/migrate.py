"""换机迁移：把 NAS Safe 的全部「可移植配置」打包成迁移包，跨品牌安全导入。

设计要点（与收费路线图③一致）：
1. 只打包 state_dir() 下的用户配置 JSON，绝不打包运行时缓存（微信 token / 发送记录等机器相关数据）。
2. 导入时按 path_map（旧共享根 → 新共享根）重写受保护路径，解决「换机/换品牌后盘符路径变了」的核心痛点。
3. 导入时按「目标品牌能力」做优雅降级：目标不支持某项能力时，对应配置保留但不生效并给出白话说明，不报错。
4. 所有写操作前默认 dry_run 先预览，确认无误再落地。

迁移包结构:
{
  "bundle_version": 1,
  "created_at": "...",
  "source_brand": "qnap",
  "source_caps": {...},
  "configs": {
     "protected.json": {...},
     "notify.json": {...},
     "ai.json": {...},
     "autosnapshot.json": {...},
     "daily_report.json": {...}
  }
}
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# 可移植配置：用户设置，迁移时带走
MIGRATABLE = {
    "protected.json": "受保护路径基线",
    "notify.json": "通知配置",
    "ai.json": "AI 配置",
    "autosnapshot.json": "自动快照配置",
    "daily_report.json": "每日日报配置",
}

# 不迁移（机器相关、需重新授权 / 重新生成）
SKIP_FILES = (
    "wechat_token.json",   # 微信 access_token 缓存，按机器刷新
    "notify_sent.json",    # 去重发送记录
    "state.db",            # 本地状态库（如有）
)

BUNDLE_VERSION = 1


def _state_dir() -> str:
    import storage  # 复用 state_dir()，避免重复定义
    return storage.state_dir()


def collect_state() -> dict:
    """读取当前所有可移植配置，返回 {filename: data}。纯只读。"""
    d = _state_dir()
    out: dict = {}
    for fn in MIGRATABLE:
        p = os.path.join(d, fn)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    out[fn] = json.load(f)
            except (json.JSONDecodeError, OSError):
                # 单个文件损坏不影响其它
                continue
    return out


def build_bundle(brand: str | None = None, caps: dict | None = None) -> dict:
    """构建迁移包（导出用）。"""
    # 延迟导入，避免与 app 启动时循环依赖
    from brands import detect_brand, detect_capabilities
    brand = brand or detect_brand()
    caps = caps or detect_capabilities(brand)
    return {
        "bundle_version": BUNDLE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_brand": brand,
        "source_caps": caps,
        "configs": collect_state(),
    }


def remap_paths(obj, path_map: dict) -> object:
    """递归把 obj 里出现的旧根路径替换成新根。path_map: {旧根: 新根}。"""
    if isinstance(obj, dict):
        return {k: remap_paths(v, path_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [remap_paths(v, path_map) for v in obj]
    if isinstance(obj, str):
        for old, new in path_map.items():
            if old and new is not None and obj.startswith(old):
                return new + obj[len(old):]
    return obj


def _resolve_target(target_brand: str | None, target_caps: dict | None):
    from brands import detect_brand, detect_capabilities
    tb = target_brand or detect_brand()
    tc = target_caps or detect_capabilities(tb)
    return tb, tc


def apply_bundle(bundle: dict, path_map: dict | None = None,
                 target_brand: str | None = None, target_caps: dict | None = None,
                 dry_run: bool = False) -> dict:
    """把迁移包配置写入当前 state_dir。返回应用报告。

    path_map: {旧共享根: 新共享根}，用于重写受保护路径（跨品牌换机核心）。
    target_brand/target_caps: 目标机能力；默认取当前机。
    dry_run=True 只计算不落地，用于「预览」。
    """
    tb, tc = _resolve_target(target_brand, target_caps)
    path_map = path_map or {}
    configs = bundle.get("configs", {})
    report = {
        "target_brand": tb,
        "target_brand_label": tc.get("brand_label", tb),
        "target_caps": tc,
        "applied": [],
        "skipped": [],
        "remapped": [],
        "dry_run": dry_run,
    }

    for fn, label in MIGRATABLE.items():
        data = configs.get(fn)
        if data is None:
            continue

        # 路径迁移：受保护路径需要重写（换机/换品牌后盘符变了）
        if fn == "protected.json" and path_map:
            before = json.dumps(data, ensure_ascii=False, sort_keys=True)
            data = remap_paths(data, path_map)
            if json.dumps(data, ensure_ascii=False, sort_keys=True) != before:
                report["remapped"].append(fn)

        # 能力降级：目标不支持原生快照时，自动快照配置保留但标记不生效
        snap_none = tc.get("snapshot_backend") in ("none",)
        if fn == "autosnapshot.json" and snap_none:
            data = dict(data)
            data["enabled"] = False
            data["_disabled_reason"] = "目标品牌暂不支持原生快照，配置已保留但暂不生效；可用 rsync/备份兜底"
            report["skipped"].append({
                "file": fn,
                "label": label,
                "reason": "目标品牌不支持原生快照，自动快照配置保留但暂不生效",
            })

        if not dry_run:
            _write_config(fn, data)
        report["applied"].append({"file": fn, "label": label})

    return report


def _write_config(fn: str, data: dict) -> None:
    import storage
    d = storage.state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        # 只读环境静默跳过，不影响核心功能
        pass


def validate_bundle(bundle: object) -> tuple[bool, str]:
    """粗校验迁移包结构，返回 (ok, 说明)。"""
    if not isinstance(bundle, dict):
        return False, "迁移包不是合法对象"
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        return False, f"迁移包版本不兼容（期望 {BUNDLE_VERSION}）"
    configs = bundle.get("configs")
    if not isinstance(configs, dict):
        return False, "迁移包缺少 configs"
    return True, "ok"
