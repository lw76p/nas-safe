"""Windows 平台辅助函数（仅标准库 ctypes）。

用于 TS Safe 跑在 Windows 电脑上时，提供卷枚举、目录列表等基础能力，
避免重复文件清理 / 磁盘清理等功能因缺少 Linux shell 而无法选择目录。
"""
from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional


def is_windows() -> bool:
    return sys.platform == "win32"


def _errcheck_bool(result, func, args):  # noqa: ARG001
    if not result:
        return None
    return args


def list_drives() -> list[dict]:
    """返回 Windows 本地固定磁盘列表（含容量信息）。

    每项：{"mount": "C:\\", "name": "本地磁盘 (C:)", "total_kb", "used_kb", "percent"}
    """
    if not is_windows():
        return []
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if bitmask & (1 << i):
            letter = f"{chr(65 + i)}:\\"
            try:
                info = _drive_info(letter)
            except Exception:
                info = None
            if info:
                drives.append(info)
    return drives


def _drive_type(path: str) -> int:
    return ctypes.windll.kernel32.GetDriveTypeW(path)


def _drive_info(path: str) -> Optional[dict]:
    """取单个盘符信息；只保留本地固定盘(DRIVE_FIXED=3)和网络盘(DRIVE_REMOTE=4)。"""
    t = _drive_type(path)
    if t not in (3, 4):
        return None
    free_b = ctypes.c_ulonglong(0)
    total_b = ctypes.c_ulonglong(0)
    ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        path, ctypes.byref(free_b), ctypes.byref(total_b), None
    )
    total_kb = total_b.value // 1024
    free_kb = free_b.value // 1024
    used_kb = max(total_kb - free_kb, 0)
    label = _volume_label(path) or "本地磁盘"
    return {
        "mount": path,
        "name": f"{label} ({path[:-1]})",
        "total_kb": total_kb,
        "used_kb": used_kb,
        "percent": round(used_kb / total_kb * 100, 1) if total_kb else 0,
    }


def _volume_label(path: str) -> Optional[str]:
    buf = ctypes.create_unicode_buffer(256)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        path, buf, 256, None, None, None, None, 0
    )
    return buf.value if ok else None


def list_dir(path: str, max_n: int = 300) -> dict:
    """Windows 本地目录 listing（只读）。"""
    if not is_windows():
        return {"path": path, "dirs": []}
    try:
        entries = os.listdir(path)
    except OSError as exc:
        return {"path": path, "dirs": [], "error": str(exc)}
    dirs = sorted(
        name for name in entries
        if os.path.isdir(os.path.join(path, name)) and not name.startswith(".")
    )[:max_n]
    return {"path": path, "dirs": dirs}


def is_safe_path(path: str, mounts: set[str]) -> bool:
    """判断 path 是否落在已知 mounts 内（支持 Windows 盘符）。"""
    if not path:
        return False
    norm = os.path.normcase(os.path.abspath(path))
    for m in mounts:
        mnorm = os.path.normcase(os.path.abspath(m))
        if norm == mnorm or norm.startswith(mnorm + os.sep):
            return True
    return False
