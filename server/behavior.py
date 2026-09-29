"""
NAS Safe — v3 勒索行为检测

不依赖网络情报，直接在「生产（实时）目录」上做启发式异常检测，
识别勒索软件发作时的典型行为，并在可疑时建议/触发**紧急快照**：

  1. 扩展名突变（extension mutation）：出现成批 .locked / .crypt / .encrypted 等勒索扩展名
  2. 熵值骤升（entropy spike）：大量文件头部熵值逼近随机（加密后的典型特征）
  3. 批量改名（mass rename）：某一异常扩展名在目录下突然占据多数

产出：
  - analyze_path(path)        单目录信号统计
  - detect_behavior(paths)    多目录聚合 → {suspicious, score, signals, recommendation}
  - take_emergency_snapshot(volume)  对指定卷拍一张紧急快照（best-effort，需 storage 支持）

设计约束：
  - 只读扫描生产目录的头部与元信息，不修改任何文件。
  - 有配额（max_files / 时间预算），避免扫大目录卡死。
  - 阈值偏保守，宁可少报也不误杀；所有结论都给出可读信号，便于人工复核。
  - 绝不自动删除/回滚任何东西；紧急快照是「增加一道防线」，不是破坏操作。
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime

# 已知勒索软件扩展名（小写，含点）。持续补充即可提高召回。
RANSOM_EXTS = {
    ".locked", ".lock", ".crypt", ".crypted", ".crypto", ".encrypted",
    ".enc", ".encrypt", ".cryptolocker", ".vvv", ".xyz", ".zzz", ".aaa",
    ".abc", ".micro", ".wncry", ".wcry", ".wncrypt", ".crypz", ".cryp1",
    ".aes", ".aes256", ".aes_ni", ".cerber", ".zepto", ".torrentlocker",
    ".lol!", ".omg!", ".fun", ".kraken", ".gitlock", ".ryuk", ".makop",
    ".stop", ".djvu", ".bip", ".npsk", ".booa", ".peet", ".derp",
}

# 正常的"高熵"文件（本就是压缩/加密格式），不应误判为勒索
SAFE_HIGH_ENTROPY_EXTS = {
    ".gz", ".bz2", ".xz", ".zip", ".7z", ".rar", ".tgz", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".mp4", ".mkv", ".avi", ".mov", ".mp3",
    ".flac", ".pdf", ".docx", ".xlsx", ".pptx", ".wav", ".m4a", ".heic",
    ".sqlite", ".db", ".iso", ".bin", ".dmg", ".apk", ".ttf", ".woff2",
}

HIGH_ENTROPY_THRESHOLD = 7.5     # 熵值 ≥ 此值视为"接近随机"
RANSOM_RATIO_THRESHOLD = 0.02     # 勒索扩展名文件占比 ≥ 2% 即可疑
ENTROPY_RATIO_THRESHOLD = 0.30    # 高熵文件占比 ≥ 30% 且非安全格式即可疑
MASS_RENAME_RATIO_THRESHOLD = 0.30  # 单一异常扩展名占比 ≥ 30% 即可疑

DEFAULT_MAX_FILES = 500
DEFAULT_SAMPLE_BYTES = 65536
TIME_BUDGET_SECONDS = 5.0


def shannon_entropy(data: bytes) -> float:
    """计算字节序列的香农熵（0~8）。空输入返回 0。"""
    if not data:
        return 0.0
    freq: dict[int, int] = {}
    for b in data:
        freq[b] = freq.get(b, 0) + 1
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _ext(name: str) -> str:
    _, ext = os.path.splitext(name)
    return ext.lower()


def analyze_path(path: str, max_files: int = DEFAULT_MAX_FILES,
                 sample_bytes: int = DEFAULT_SAMPLE_BYTES,
                 time_budget: float = TIME_BUDGET_SECONDS) -> dict:
    """扫描单个目录（含一层递归，配额受限），返回该目录的信号统计。"""
    if not path or not os.path.isdir(path):
        return {"path": path, "exists": False, "total_files": 0,
                "ransom_files": 0, "high_entropy_files": 0,
                "sampled": 0, "ext_histogram": {}, "truncated": False}

    total = 0
    ransom = 0
    high_entropy = 0
    sampled = 0
    ext_hist: dict[str, int] = {}
    truncated = False
    start = time.monotonic()

    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            if total >= max_files or (time.monotonic() - start) > time_budget:
                truncated = True
                break
            total += 1
            ext = _ext(name)
            ext_hist[ext] = ext_hist.get(ext, 0) + 1
            if ext in RANSOM_EXTS:
                ransom += 1
                continue
            # 熵检测：仅对"非天然高熵"格式抽样，避免把照片/压缩包误判
            if ext not in SAFE_HIGH_ENTROPY_EXTS:
                fp = os.path.join(dirpath, name)
                try:
                    with open(fp, "rb") as fh:
                        head = fh.read(sample_bytes)
                except (OSError, IsADirectoryError):
                    continue
                sampled += 1
                if shannon_entropy(head) >= HIGH_ENTROPY_THRESHOLD:
                    high_entropy += 1
        if truncated:
            break

    return {
        "path": path,
        "exists": True,
        "total_files": total,
        "ransom_files": ransom,
        "high_entropy_files": high_entropy,
        "sampled": sampled,
        "ext_histogram": ext_hist,
        "truncated": truncated,
    }


def detect_behavior(paths: list[str]) -> dict:
    """多目录聚合检测。返回 {suspicious, score, signals[], recommendation, per_path[]}。"""
    per_path = [analyze_path(p) for p in paths if p]
    signals: list[dict] = []

    total_files = sum(p["total_files"] for p in per_path if p.get("exists"))
    ransom_files = sum(p["ransom_files"] for p in per_path if p.get("exists"))
    high_entropy = sum(p["high_entropy_files"] for p in per_path if p.get("exists"))
    sampled = sum(p["sampled"] for p in per_path if p.get("exists"))

    ransom_ratio = (ransom_files / total_files) if total_files else 0.0
    entropy_ratio = (high_entropy / sampled) if sampled else 0.0

    # 批量改名：跨目录统计出现最多的异常扩展名（排除常见正常扩展名）占比
    ext_hist: dict[str, int] = {}
    for p in per_path:
        for ext, c in (p.get("ext_histogram") or {}).items():
            ext_hist[ext] = ext_hist.get(ext, 0) + c
    common_normals = {".jpg", ".jpeg", ".png", ".mp4", ".mkv", ".pdf", ".docx",
                      ".xlsx", ".txt", ".md", ".mp3", ".gif", ".zip", ".webp"}
    suspicious_exts = {e: c for e, c in ext_hist.items()
                       if e not in common_normals and e not in SAFE_HIGH_ENTROPY_EXTS
                       and e not in RANSOM_EXTS}
    dominant_ext = max(suspicious_exts.items(), key=lambda kv: kv[1]) \
        if suspicious_exts else (None, 0)
    mass_rename_ratio = (dominant_ext[1] / total_files) if total_files else 0.0

    score = 0
    if ransom_ratio >= RANSOM_RATIO_THRESHOLD:
        score += int(min(100, ransom_ratio * 400))
        signals.append({
            "type": "extension_mutation",
            "level": "critical",
            "summary": (
                f"检测到 {ransom_files}/{total_files} 个文件携带勒索软件扩展名"
                f"（占比 {ransom_ratio:.1%}），典型扩展名突变攻击。"
            ),
        })
    if entropy_ratio >= ENTROPY_RATIO_THRESHOLD:
        score += int(min(100, entropy_ratio * 250))
        signals.append({
            "type": "entropy_spike",
            "level": "critical",
            "summary": (
                f"抽样文件中 {high_entropy}/{sampled} 个头部熵值逼近随机"
                f"（占比 {entropy_ratio:.1%}），疑似被加密。"
            ),
        })
    if mass_rename_ratio >= MASS_RENAME_RATIO_THRESHOLD:
        score += int(min(100, mass_rename_ratio * 300))
        signals.append({
            "type": "mass_rename",
            "level": "warn",
            "summary": (
                f"扩展名「{dominant_ext[0]}」在 {dominant_ext[1]}/{total_files} 个文件中"
                f"集中出现（占比 {mass_rename_ratio:.1%}），疑似批量改名。"
            ),
        })

    score = min(100, score)
    suspicious = score >= 50

    recommendation = "未检测到明显勒索行为，继续保持监控。"
    if suspicious:
        recommendation = (
            "检测到疑似勒索行为！建议立即对数据卷拍一张紧急快照以保留最后可恢复副本，"
            "并断开该目录的写入 / 隔离主机。"
        )

    return {
        "ok": True,
        "suspicious": suspicious,
        "score": score,
        "ransom_files": ransom_files,
        "total_files": total_files,
        "high_entropy_files": high_entropy,
        "sampled": sampled,
        "mass_rename_ext": dominant_ext[0],
        "mass_rename_ratio": round(mass_rename_ratio, 4),
        "signals": signals,
        "recommendation": recommendation,
        "per_path": per_path,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
    }


def take_emergency_snapshot(volume_mountpoint: str) -> dict:
    """对指定卷（mountpoint 或 name）拍一张紧急快照，best-effort。

    仅创建只读快照并登记为受保护，不删除/不回滚任何东西。
    失败时返回 {ok: False, error}。
    """
    import storage  # 懒加载，避免循环导入
    try:
        volumes = storage.list_all_volumes()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"列卷失败: {exc}"}
    target = None
    for vol in volumes:
        if vol.mountpoint == volume_mountpoint or vol.name == volume_mountpoint:
            target = vol
            break
    if target is None:
        return {"ok": False, "error": f"未找到存储单元: {volume_mountpoint}"}
    try:
        name = f"emergency-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        snap = storage.create_snapshot(target, name, vital=True)
        storage.register_protected(snap)
        return {"ok": True, "snapshot": snap.to_dict(),
                "message": f"紧急快照已创建：{name}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"创建紧急快照失败: {exc}"}
