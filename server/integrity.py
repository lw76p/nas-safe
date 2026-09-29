"""
NAS Safe — v2 内容完整性校验

在 v1「基线对比法」（受保护快照消失 / 锁定解除即告警）之上，进一步校验
受保护快照的**实际内容**是否被动过：

  - 创建 / 锁定快照时，对可读的快照实体（本地 btrfs/zfs 子卷、QNAP 本地只读挂载）
    生成一份轻量清单（manifest）：文件数、总字节、按 (相对路径, 大小, mtime) 排序
    算出的签名，以及对少量抽样文件做「头部内容哈希」。
  - 巡检时重新生成清单并比对：签名变化 → 文件被增删/改名；抽样头部哈希变化 →
    内容被替换（即便大小没变也能发现）。

设计约束：
  - 绝不在默认 30s 巡检里全量遍历大快照（会卡接口）。完整校验通过
    `scan_integrity()` 显式触发（UI「深度校验」按钮 / 环境变量 NASSAFE_INTEGRITY_CHECK=1
    时并入 /api/alerts）。
  - 只读访问快照，不修改任何内容。
  - 对无法本地读取的快照（如 QNAP SSH 远程管理模式）best-effort 跳过，不报错。
  - 与 storage 解耦：本模块只依赖 os / hashlib / 标准库；仅在 scan_integrity 内懒加载 storage，
    避免循环导入。
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime

# 默认上限：防止超大快照卡死。超过则 truncated=True，仅校验已扫到的部分。
DEFAULT_MAX_FILES = 2000
DEFAULT_SAMPLE_FILES = 32          # 多少份抽样文件做头部内容哈希
DEFAULT_SAMPLE_BYTES = 65536       # 每份抽样读取前多少个字节


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _head_sha256(path: str, sample_bytes: int) -> str:
    try:
        with open(path, "rb") as fh:
            return _sha256(fh.read(sample_bytes))
    except (OSError, IsADirectoryError):
        return ""


def build_manifest(root: str, max_files: int = DEFAULT_MAX_FILES,
                  sample_files: int = DEFAULT_SAMPLE_FILES,
                  sample_bytes: int = DEFAULT_SAMPLE_BYTES) -> dict:
    """遍历可读的快照实体目录，生成轻量完整性清单。

    返回 dict（见模块说明）；root 不存在 / 不可读时返回 None。
    """
    if not root or not os.path.isdir(root):
        return None

    files: list[tuple[str, int, int]] = []   # (relpath, size, mtime_ns)
    total_bytes = 0
    truncated = False
    start = time.monotonic()

    for dirpath, _dirnames, filenames in os.walk(root):
        if time.monotonic() - start > 30.0:
            truncated = True
            break
        for name in filenames:
            if len(files) >= max_files:
                truncated = True
                break
            fp = os.path.join(dirpath, name)
            try:
                st = os.lstat(fp)   # lstat 不跟随符号链接，避免乱码/越界
            except OSError:
                continue
            if not os.path.isfile(fp) and not os.path.islink(fp):
                continue
            rel = os.path.relpath(fp, root).replace(os.sep, "/")
            files.append((rel, st.st_size, st.st_mtime_ns))
            total_bytes += st.st_size
        if truncated:
            break

    # 签名：按 (路径, 大小, mtime) 排序后拼接哈希 —— 增删/改名/大小变化都会改变它
    sig_src = "\n".join(f"{r}\x00{s}\x00{m}" for r, s, m in sorted(files))
    signature = _sha256(sig_src.encode("utf-8", "surrogateescape"))

    # 抽样头部哈希：固定取排序后的前 sample_files 个，保证两次校验取同一批
    sampled = []
    for rel, _s, _m in sorted(files)[:sample_files]:
        fp = os.path.join(root, rel)
        sampled.append({"relpath": rel, "head_sha256": _head_sha256(fp, sample_bytes)})

    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "sampled_count": len(sampled),
        "signature": signature,
        "sampled": sampled,
        "truncated": truncated,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
    }


def build_manifest_for_snapshot(snap, max_files: int = DEFAULT_MAX_FILES,
                               sample_files: int = DEFAULT_SAMPLE_FILES) -> dict | None:
    """从 Snapshot 对象解析本地可读根目录并生成清单；不可本地读返回 None。"""
    root = getattr(snap, "path", None) or getattr(snap, "mount_path", None)
    root = root or getattr(snap, "snapshot_path", None)
    if not root:
        return None
    return build_manifest(root, max_files=max_files, sample_files=sample_files)


def compare_manifest(old: dict, new: dict) -> dict:
    """比对两份清单，给出内容完整性差异。"""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return {"changed": False, "file_count_delta": 0,
                "content_changed_count": 0, "common_sampled": 0}
    changed = old.get("signature") != new.get("signature")
    file_count_delta = (new.get("file_count", 0) - old.get("file_count", 0))

    old_map = {s["relpath"]: s.get("head_sha256", "") for s in old.get("sampled", [])}
    new_map = {s["relpath"]: s.get("head_sha256", "") for s in new.get("sampled", [])}
    common = set(old_map) & set(new_map)
    content_changed = sum(1 for k in common if old_map[k] != new_map[k])

    return {
        "changed": changed,
        "file_count_delta": file_count_delta,
        "content_changed_count": content_changed,
        "common_sampled": len(common),
        "old_file_count": old.get("file_count"),
        "new_file_count": new.get("file_count"),
    }


def scan_integrity() -> list:
    """巡检所有受保护快照的内容完整性，返回篡改告警列表（空 = 正常）。

    仅对「仍在、且本地可读」的受保护快照做深度比对；不可读的（如 QNAP SSH 远程）
    优雅跳过。不抛异常（巡检失败不影响接口）。
    """
    import storage  # 懒加载，避免循环导入

    alerts: list = []
    try:
        volumes = storage.list_all_volumes()
    except Exception:
        return alerts

    current: dict = {}
    for vol in volumes:
        try:
            for s in storage.list_all_snapshots(vol):
                current[storage.snapshot_key(s)] = s
        except Exception:
            continue

    for entry in storage.load_protected().get("entries", []):
        baseline = entry.get("integrity")
        if not baseline:
            continue
        snap = current.get(entry.get("key"))
        if not snap:
            continue  # 消失由 scan_tamper 负责
        new = build_manifest_for_snapshot(snap)
        if not new:
            continue
        cmp = compare_manifest(baseline, new)
        if cmp["changed"]:
            detail = (
                f"受保护快照「{entry.get('name')}」（位于 {entry.get('volume')}）"
                f"的清单签名发生变化：文件数变化 {cmp['file_count_delta']:+d}，"
            )
            if cmp["content_changed_count"] > 0:
                detail += (
                    f"其中 {cmp['content_changed_count']} 个抽样文件头部内容哈希不一致——"
                    f"疑似内容被替换（即便大小未变）。"
                )
            else:
                detail += "疑似文件被增删或改名。"
            alerts.append({
                "level": "warn",
                "type": "integrity_changed",
                "title": "受保护快照内容疑似被篡改",
                "detail": detail,
                "volume": entry.get("volume"),
                "snapshot": entry.get("name"),
                "key": entry.get("key"),
                "compare": cmp,
                "detected_at": datetime.now().isoformat(timespec="seconds"),
            })
    return alerts
