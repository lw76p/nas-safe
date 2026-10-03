"""macOS APFS 快照后端（tmutil 本地快照 + mount_apfs 只读挂载）。

统一接口，与 storage.py 里的 btrfs / zfs / qnap / aliyun / vss 后端保持一致，
由 storage 的分派层调用（fs_type="apfs"）。

设计：
  - 保护目标：本机 APFS 宗卷（/ 与 /System/Volumes/Data，以及 /Volumes 下的 APFS 盘）
  - 创建：tmutil localsnapshot（Time Machine 本地快照，APFS COW 零拷贝）
  - 列举：tmutil listlocalsnapshots <挂载点>（系统自己登记，无需本地元数据仓库）
  - 浏览/取回：mount_apfs -s 只读挂载到 /tmp/.nassafe_apfs/<日期>/（需要 root）
  - 删除：tmutil deletelocalsnapshots <日期>
权限：
  - 创建/列举：管理员用户即可
  - 挂载浏览：需要 root（mount_apfs 限制）；非 root 时给出明确中文提示
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from storage import StorageError

# APFS 快照名格式：com.apple.TimeMachine.2026-10-03-204512.local
_RE_SNAP = re.compile(r"com\.apple\.TimeMachine\.(\d{4}-\d{2}-\d{2}-\d{6})\.local")
_RE_CREATED = re.compile(r"Created local snapshot with date[:：]\s*(\S+)")
_MOUNT_BASE = "/tmp/.nassafe_apfs"

# 挂载表里的系统私有 APFS 挂载点，不作为保护目标
_MOUNT_DENY = ("/System/Volumes/VM", "/System/Volumes/Preboot", "/System/Volumes/Update",
               "/System/Volumes/xarts", "/System/Volumes/Hardware", "/System/Volumes/Storage",
               "/private/var/vm", "/System/Volumes/Mounts")


# ---------------------------------------------------------------------------
# 环境守卫
# ---------------------------------------------------------------------------

def _need_macos() -> None:
    if sys.platform != "darwin":
        raise StorageError("APFS 快照只在 macOS（苹果电脑）上可用")


def _which(name: str) -> str:
    p = shutil.which(name) or {
        "tmutil": "/usr/bin/tmutil",
        "mount_apfs": "/sbin/mount_apfs",
        "diskutil": "/usr/sbin/diskutil",
        "mount": "/sbin/mount",
    }.get(name, "")
    if not p or not os.path.exists(p):
        raise StorageError(f"找不到系统工具 {name}（本机可能不是 macOS）")
    return p


def _run(cmd: list, timeout: int = 60) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise StorageError(f"找不到命令：{cmd[0]}")
    except subprocess.TimeoutExpired:
        raise StorageError(f"命令超时：{' '.join(cmd)}")
    out = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        detail = out.strip().splitlines()
        detail = detail[-1] if detail else f"退出码 {proc.returncode}"
        raise StorageError(f"{' '.join(cmd[:2])} 失败：{detail[:200]}")
    return out


def _is_root() -> bool:
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


# ---------------------------------------------------------------------------
# 宗卷枚举
# ---------------------------------------------------------------------------

def _apfs_mounts() -> list:
    """从挂载表取 APFS 宗卷挂载点（去掉系统私有挂载）。"""
    out = _run([_which("mount")])
    mounts = []
    for line in out.splitlines():
        m = re.match(r"(.+?) on (.+?) \(apfs", line)
        if not m:
            continue
        mp = m.group(2)
        if mp in _MOUNT_DENY:
            continue
        if mp.startswith("/System/Volumes/") and mp != "/System/Volumes/Data":
            continue
        if mp in ("/", "/System/Volumes/Data") or mp.startswith("/Volumes/"):
            mounts.append(mp)
    # 去重保序，根卷放最前
    return sorted(set(mounts), key=lambda p: (p != "/", p))


def _volume_label(mp: str) -> str:
    if mp == "/":
        return "系统卷（/）"
    if mp == "/System/Volumes/Data":
        return "数据卷（用户文件）"
    return f"APFS 盘（{os.path.basename(mp.rstrip('/')) or mp}）"


def list_volumes() -> list:
    _need_macos()
    from storage import Volume

    volumes = []
    for mp in _apfs_mounts():
        volumes.append(Volume(
            name=_volume_label(mp),
            mountpoint=mp,
            fs_type="apfs",
            backend="fs",
            device=None,
        ))
    return volumes


# ---------------------------------------------------------------------------
# 快照列举 / 创建 / 删除
# ---------------------------------------------------------------------------

def _snapshot_date(sn: str) -> str:
    m = _RE_SNAP.search(sn or "")
    return m.group(1) if m else (sn or "")


def _fmt_date(d: str) -> str:
    """2026-10-03-204512 → 2026-10-03 20:45:12"""
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})-(\d{2})(\d{2})(\d{2})", d)
    if not m:
        return d
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"


def _mount_dir(date: str) -> str:
    return os.path.join(_MOUNT_BASE, date)


def list_snapshots(volume) -> list:
    _need_macos()
    from storage import Snapshot

    mp = getattr(volume, "mountpoint", "/")
    out = _run([_which("tmutil"), "listlocalsnapshots", mp])
    snaps = []
    for line in out.splitlines():
        m = _RE_SNAP.search(line)
        if not m:
            continue
        date = m.group(1)
        snaps.append(Snapshot(
            name=f"com.apple.TimeMachine.{date}.local",
            volume=getattr(volume, "name", mp),
            created_at=_fmt_date(date),
            snapshot_id=date,
            readonly=True,
            vital=False,      # tmutil 本地快照由系统按空间自动淘汰，无法永久锁定
            description="APFS 本地快照 · 零拷贝 · 系统可能按磁盘空间自动清理",
            fs_type="apfs",
            backend="fs",
            path=None,
            mount_path=_mount_dir(date) if os.path.isdir(_mount_dir(date)) else None,
        ))
    snaps.sort(key=lambda s: s.snapshot_id or "", reverse=True)
    return snaps


def create_snapshot(volume, name: str, vital: bool = True):
    _need_macos()
    from storage import Snapshot

    out = _run([_which("tmutil"), "localsnapshot"], timeout=120)
    m = _RE_CREATED.search(out)
    if not m:
        raise StorageError("创建本地快照失败：" + out.strip()[:200])
    date = m.group(1)
    return Snapshot(
        name=f"com.apple.TimeMachine.{date}.local",
        volume=getattr(volume, "name", getattr(volume, "mountpoint", "/")),
        created_at=_fmt_date(date),
        snapshot_id=date,
        readonly=True,
        vital=False,
        description="APFS 本地快照 · 零拷贝 · 创建成功",
        fs_type="apfs",
        backend="fs",
        path=None,
    )


def delete_snapshot(snapshot) -> None:
    _need_macos()
    date = getattr(snapshot, "snapshot_id", "") or _snapshot_date(getattr(snapshot, "name", ""))
    if not date:
        raise StorageError("缺少快照日期，无法删除")
    _run([_which("tmutil"), "deletelocalsnapshots", date], timeout=120)
    _unmount(date)


# ---------------------------------------------------------------------------
# 挂载 / 浏览 / 取回
# ---------------------------------------------------------------------------

def _ensure_mounted(snapshot) -> str:
    """把快照只读挂到 /tmp/.nassafe_apfs/<日期>/，返回挂载点（需要 root）。"""
    _need_macos()
    if not _is_root():
        raise StorageError(
            "浏览 APFS 快照需要管理员权限（mount_apfs 限制）：请用 sudo 运行 TS Safe，"
            "或改用 Time Machine 界面查看")
    date = getattr(snapshot, "snapshot_id", "") or _snapshot_date(getattr(snapshot, "name", ""))
    mp = _mount_dir(date)
    if not os.path.isdir(mp):
        os.makedirs(mp, exist_ok=True)
        vol = str(getattr(snapshot, "volume", "") or "")
        src_vol = "/System/Volumes/Data" if "数据卷" in vol else "/"
        try:
            _run([_which("mount_apfs"), "-s",
                  f"com.apple.TimeMachine.{date}.local", src_vol, mp], timeout=60)
        except StorageError:
            try:
                os.rmdir(mp)
            except OSError:
                pass
            raise
    return mp


def _unmount(date: str) -> None:
    mp = _mount_dir(date)
    if os.path.isdir(mp) and os.path.ismount(mp):
        try:
            subprocess.run(["/usr/sbin/diskutil", "unmount", "force", mp],
                           capture_output=True, timeout=60)
        except Exception:  # noqa: BLE001
            pass
    try:
        os.rmdir(mp)
    except OSError:
        pass


def _safe_join(root: str, subpath: str) -> str:
    sub = (subpath or "").lstrip("/\\")
    dest = os.path.abspath(os.path.join(root, sub))
    if dest != root and not dest.startswith(root + os.sep):
        raise StorageError("非法路径")
    return dest


def browse_snapshot(snapshot, subpath: str = "") -> dict:
    root = _ensure_mounted(snapshot)
    target = _safe_join(root, subpath)
    if not os.path.exists(target):
        raise StorageError("路径不存在：" + subpath)
    if os.path.isfile(target):
        st = os.stat(target)
        return {"ok": True, "type": "file", "path": subpath,
                "size": st.st_size, "mtime": int(st.st_mtime)}
    entries = []
    for name in sorted(os.listdir(target))[:500]:
        p = os.path.join(target, name)
        try:
            is_dir = os.path.isdir(p)
            st = os.stat(p, follow_symlinks=False)
            entries.append({"name": name, "type": "dir" if is_dir else "file",
                            "size": None if is_dir else st.st_size})
        except OSError:
            continue
    return {"ok": True, "type": "dir", "path": subpath, "entries": entries}


def restore_from_snapshot(snapshot, rel_path: str, dest: str) -> dict:
    root = _ensure_mounted(snapshot)
    src = _safe_join(root, rel_path)
    if not os.path.isfile(src):
        raise StorageError("只支持取回文件（不支持整个目录）")
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    return {"ok": True, "dest": dest, "size": os.path.getsize(dest)}
