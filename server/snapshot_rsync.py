"""通用 Linux（无 btrfs/ZFS）硬链接增量快照后端（TimeMachine 式）。

面向裸机 / 云服务器 / ext4、xfs、f2fs 等没有块级快照能力的文件系统。

原理（与 rsync --link-dest 语义一致，关键安全性质）：
  - 首份快照：完整复制真实字节
  - 后续快照：与上一份「大小 + 修改时间」一致的文件 -> 直接硬链接（零额外占用）
              变更过的文件 -> 复制真实字节
  因此源文件后续被原地篡改 / 加密（勒索）时，快照里仍是旧内容，
  具备真实的取回还原能力；而同内容文件不重复占空间。

实现：有 rsync 二进制时优先用 rsync（更快、保留更多属性），
否则用纯 Python 硬链接复制兜底 —— 零外部依赖，任何 Linux 都能跑。

接口与 btrfs / zfs / qnap / aliyun 后端一致，由 storage 分派层调用。
"""
from __future__ import annotations

import os
import re
import sys
import json
import shutil
import subprocess
from datetime import datetime

from storage import StorageError

# 单份快照的规模护栏：超过就中止，避免一次快照把磁盘跑满 / 卡死服务
MAX_FILES = 300_000

# 快照仓库根目录（放在 state 下，与配置同生命周期）
_REPO_DIRNAME = "rsyncsnap"

# 已知的数据目录 -> 友好展示名
_LINUX_TARGETS = [
    ("/data", "数据盘"),
    ("/home", "用户目录"),
    ("/srv", "服务数据"),
    ("/var/www", "网站目录"),
    ("/opt", "应用目录"),
    ("/share", "共享目录"),
]

# 需要扫描子目录作为独立目标的父目录
_SCAN_PARENTS = [
    ("/mnt", "挂载盘"),
    ("/media", "外置盘"),
    ("/volume", "存储卷"),
]

# /proc/mounts 里要忽略的伪文件系统
_SKIP_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "overlay", "squashfs", "iso9660", "fusectl", "bpf", "pstore", "mqueue",
    "securityfs", "debugfs", "tracefs", "configfs", "hugetlbfs", "autofs",
    "binfmt_misc", "nsfs", "ramfs", "rpc_pipefs", "efivarfs",
}
# /proc/mounts 里要忽略的挂载点前缀（系统目录，不该当数据盘保护）
_SKIP_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/var/lib/docker")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _which(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def _self_state_dir() -> str:
    """TS Safe 自身 state 目录绝对路径（快照源必须排除，防自我套娃）。"""
    try:
        from storage import state_dir
        return os.path.abspath(state_dir())
    except Exception:
        return os.path.abspath(os.path.join(os.getcwd(), "state"))


def _state_root() -> str:
    """快照仓库的父目录（跟随 storage 的 state 目录）。"""
    try:
        from storage import state_dir
        base = state_dir()
    except Exception:
        base = os.path.join(os.getcwd(), "state")
    return os.path.join(base, _REPO_DIRNAME)


def _slug(text: str, limit: int = 24) -> str:
    """把快照名压成安全的目录片段（挡掉路径穿越与怪字符）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", (text or "").strip())
    s = s.strip("._") or "snapshot"
    return s[:limit]


def _repo_for(volume) -> str:
    """卷对应的快照仓库目录。"""
    if getattr(volume, "snapshot_dir", None):
        return volume.snapshot_dir
    key = _slug(getattr(volume, "name", "") or getattr(volume, "mountpoint", "") or "vol")
    return os.path.join(_state_root(), key)


def _meta_path(snapshot_dir: str) -> str:
    return os.path.join(snapshot_dir, ".nassafe_meta.json")


def _read_meta(snapshot_dir: str) -> dict:
    try:
        with open(_meta_path(snapshot_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_meta(snapshot_dir: str, data: dict) -> None:
    try:
        with open(_meta_path(snapshot_dir), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _latest_snapshot_dir(repo: str, exclude: str = "") -> str:
    """仓库里最新的一份快照目录（用于 --link-dest）。"""
    if not os.path.isdir(repo):
        return ""
    cands = []
    for name in os.listdir(repo):
        p = os.path.join(repo, name)
        if not os.path.isdir(p) or os.path.abspath(p) == os.path.abspath(exclude):
            continue
        cands.append((name, p))
    if not cands:
        return ""
    cands.sort(key=lambda x: x[0])          # 名字以时间戳开头，字典序即时间序
    return cands[-1][1]


# ---------------------------------------------------------------------------
# 枚举保护目标
# ---------------------------------------------------------------------------

def _real_mountpoints() -> list[str]:
    """从 /proc/mounts 取真实数据盘挂载点（排除系统目录与伪文件系统）。"""
    out: list[str] = []
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mount, fstype = parts[0], parts[1], parts[2]
                if fstype in _SKIP_FSTYPES:
                    continue
                if not dev.startswith("/"):
                    continue
                if mount == "/" or mount.startswith(_SKIP_MOUNTS):
                    continue
                out.append(mount)
    except OSError:
        return []
    # 去掉被其它挂载点包含的（只保留最外层），避免重复
    out = sorted(set(out))
    top = []
    for m in out:
        if not any(m != o and m.startswith(o.rstrip("/") + "/") for o in out):
            top.append(m)
    return top


def list_volumes() -> list:
    """枚举可作为保护目标的目录。

    仅 Linux 生效（Windows 走 VSS、macOS 走 APFS）；
    目标不可读或已是快照仓库时跳过。
    """
    if not sys.platform.startswith("linux"):
        return []

    from storage import Volume

    repo_root = os.path.abspath(_state_root())
    seen: set[str] = set()
    volumes: list = []

    def add(path: str, label: str) -> None:
        path = os.path.abspath(path)
        if path in seen or not os.path.isdir(path) or not os.access(path, os.R_OK):
            return
        if path == repo_root or path.startswith(repo_root + os.sep):
            return
        seen.add(path)
        key = _slug(path)
        volumes.append(Volume(
            name=f"{label}（{path}）",
            mountpoint=path,
            fs_type="rsync",
            snapshot_dir=os.path.join(repo_root, key),
            backend="fs",
        ))

    for path, label in _LINUX_TARGETS:
        add(path, label)

    for parent, label in _SCAN_PARENTS:
        if not os.path.isdir(parent):
            continue
        try:
            for name in sorted(os.listdir(parent)):
                sub = os.path.join(parent, name)
                if os.path.isdir(sub):
                    add(sub, f"{label} {name}")
        except OSError:
            pass

    for mount in _real_mountpoints():
        add(mount, "数据盘")

    return volumes


# ---------------------------------------------------------------------------
# 快照列举 / 创建 / 删除
# ---------------------------------------------------------------------------

def list_snapshots(volume) -> list:
    from storage import Snapshot
    repo = _repo_for(volume)
    if not os.path.isdir(repo):
        return []
    out: list = []
    for name in sorted(os.listdir(repo), reverse=True):
        p = os.path.join(repo, name)
        if not os.path.isdir(p):
            continue
        meta = _read_meta(p)
        created = meta.get("created_at")
        if not created:
            try:
                created = datetime.fromtimestamp(
                    os.stat(p).st_mtime).isoformat(timespec="seconds")
            except OSError:
                created = None
        display = meta.get("name") or (name.split("__", 1)[-1] if "__" in name else name)
        desc = ""
        if meta.get("files") is not None:
            desc = f"硬链接增量 · 共 {meta['files']} 个文件（新增 {meta.get('copied', 0)}）"
        out.append(Snapshot(
            name=display,
            volume=getattr(volume, "name", ""),
            created_at=created,
            path=p,
            size_bytes=meta.get("bytes"),
            readonly=True,
            description=desc,
            fs_type="rsync",
            snapshot_id=name,          # 目录名即稳定唯一 id（浏览/取回统一入口靠它定位）
            vital=bool(meta.get("vital", False)),
            backend="fs",
        ))
    return out


def _hardlink_copy(src: str, prev: str, dst: str) -> dict:
    """纯 Python 的 TimeMachine 式增量复制。

    与上一份「大小 + 修改时间」一致的文件 -> 硬链接（零占用）
    其余文件 -> 复制真实字节
    返回统计信息。
    """
    src = os.path.abspath(src)
    prev_abs = os.path.abspath(prev) if prev else ""
    files = copied = linked = 0
    total_bytes = 0

    for root, dirs, filenames in os.walk(src, followlinks=False):
        # 不递归进快照仓库自身，避免自我套娃
        dirs[:] = [
            d for d in dirs
            if os.path.abspath(os.path.join(root, d)) != os.path.abspath(dst)
            and not (prev_abs and os.path.abspath(os.path.join(root, d)) == prev_abs)
            and os.path.abspath(os.path.join(root, d)) != _self_state_dir()
        ]
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)

        for fn in filenames:
            s = os.path.join(root, fn)
            t = os.path.join(target_root, fn)
            files += 1
            if files > MAX_FILES:
                raise StorageError(
                    f"目录文件数超过 {MAX_FILES}，已中止快照（请缩小保护范围）")

            try:
                st = os.stat(s)
            except OSError:
                continue
            total_bytes += st.st_size

            # 符号链接：原样重建，不跟随
            if os.path.islink(s):
                try:
                    if not os.path.lexists(t):
                        os.symlink(os.readlink(s), t)
                except OSError:
                    pass
                continue

            # 与上一份一致 -> 硬链接（真正的增量）
            if prev_abs:
                p = os.path.join(prev_abs, fn) if rel == "." else os.path.join(prev_abs, rel, fn)
                try:
                    pst = os.stat(p)
                    if pst.st_size == st.st_size and int(pst.st_mtime) == int(st.st_mtime):
                        os.link(p, t)
                        linked += 1
                        continue
                except OSError:
                    pass

            # 新增或变更 -> 复制真实字节
            try:
                shutil.copy2(s, t)
                copied += 1
            except OSError:
                continue

    return {"files": files, "copied": copied, "linked": linked, "bytes": total_bytes}


def create_snapshot(volume, name: str, vital: bool = True):
    """创建一份增量快照。"""
    from storage import Snapshot

    src = getattr(volume, "mountpoint", "")
    if not src or not os.path.isdir(src):
        raise StorageError(f"保护目标不存在或不可读: {src}")
    if not os.access(src, os.R_OK):
        raise StorageError(f"没有读取权限: {src}")

    repo = _repo_for(volume)
    os.makedirs(repo, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(repo, f"{ts}__{_slug(name)}")
    if os.path.exists(dst):
        raise StorageError("同名快照已存在，请换一个名字")
    os.makedirs(dst, exist_ok=True)

    prev = _latest_snapshot_dir(repo, exclude=dst)
    stats: dict = {}
    used_rsync = False

    if _which("rsync"):
        cmd = ["rsync", "-a", "--delete"]
        if prev:
            cmd.append(f"--link-dest={prev}")
        # 关键护栏：把自身 state（含快照仓库）从快照源剔除，
        # 否则拷 /opt 时会把上一轮快照也拷进去，滚雪球撑爆磁盘。
        _st = _self_state_dir()
        try:
            _rel = os.path.relpath(_st, os.path.abspath(src))
        except ValueError:
            _rel = ".."
        if not _rel.startswith(".."):
            cmd.append(f"--exclude=/{_rel}")
        cmd += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if proc.returncode == 0:
                used_rsync = True
        except (OSError, subprocess.SubprocessError):
            used_rsync = False

    if not used_rsync:
        shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(dst, exist_ok=True)
        stats = _hardlink_copy(src, prev, dst)

    if not stats:
        # rsync 路径拿不到直接的计数，用 st_nlink 反推：
        #   nlink >= 2 -> 与上一份共享 inode（硬链接复用，零额外占用）
        #   nlink == 1 -> 本次真实复制的字节
        files = copied = linked = 0
        total_bytes = 0
        for root, _d, filenames in os.walk(dst, followlinks=False):
            for fn in filenames:
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                files += 1
                total_bytes += st.st_size
                if getattr(st, "st_nlink", 1) >= 2:
                    linked += 1
                else:
                    copied += 1
        stats = {"files": files, "copied": copied, "linked": linked, "bytes": total_bytes}

    created = datetime.now().isoformat(timespec="seconds")
    _write_meta(dst, {
        "name": name,
        "volume": getattr(volume, "name", ""),
        "source": src,
        "created_at": created,
        "vital": bool(vital),
        "engine": "rsync" if used_rsync else "hardlink",
        "files": stats.get("files", 0),
        "copied": stats.get("copied", 0),
        "linked": stats.get("linked", 0),
        "bytes": stats.get("bytes", 0),
    })

    return Snapshot(
        name=name,
        volume=getattr(volume, "name", ""),
        created_at=created,
        path=dst,
        size_bytes=stats.get("bytes", 0),
        readonly=True,
        description=f"硬链接增量 · 共 {stats.get('files', 0)} 个文件"
                    f"（本次新增 {stats.get('copied', 0)}）",
        fs_type="rsync",
        snapshot_id=os.path.basename(dst),  # 目录名即稳定唯一 id
        vital=bool(vital),
        backend="fs",
    )


def delete_snapshot(snapshot) -> None:
    path = getattr(snapshot, "path", "")
    if not path or not os.path.isdir(path):
        return
    # 安全护栏：只删我们自己的快照仓库内的目录
    repo_root = os.path.abspath(_state_root())
    real = os.path.abspath(path)
    if not (real.startswith(repo_root + os.sep)):
        raise StorageError("拒绝删除：该路径不在快照仓库内")
    shutil.rmtree(real, ignore_errors=True)


# ---------------------------------------------------------------------------
# 浏览 / 取回
# ---------------------------------------------------------------------------

def _safe_join(base: str, subpath: str) -> str:
    """拼接并校验不越权（挡掉 .. 与绝对路径）。"""
    base_real = os.path.realpath(base)
    full = os.path.normpath(os.path.join(base_real, subpath.lstrip("/"))) if subpath else base_real
    if not (full == base_real or full.startswith(base_real + os.sep)):
        raise StorageError("路径越权：必须在快照目录内")
    return full


def browse_snapshot(snapshot, subpath: str = "") -> dict:
    from storage import _browse_local_dir
    root = getattr(snapshot, "path", "")
    if not root or not os.path.isdir(root):
        raise StorageError("快照目录不可用（可能已被清理）")
    full = _safe_join(root, subpath)
    if not os.path.isdir(full):
        raise StorageError(f"不是目录: {subpath or '/'}")
    return {
        "ok": True,
        "backend": "rsync",
        "local": True,
        "path": full,
        "subpath": subpath,
        "entries": _browse_local_dir(full),
    }


def restore_from_snapshot(snapshot, rel_path: str, dest: str) -> dict:
    """从快照取回文件 / 目录到 dest，绝不覆盖已存在的文件。"""
    root = getattr(snapshot, "path", "")
    if not root or not os.path.isdir(root):
        raise StorageError("快照目录不可用（可能已被清理）")
    source = _safe_join(root, rel_path)
    if not os.path.exists(source):
        raise StorageError(f"快照中不存在：{rel_path}")

    os.makedirs(dest, exist_ok=True)
    dest_path = os.path.join(dest, os.path.basename(source)) if os.path.isdir(dest) else dest
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(dest_path)
        dest_path = f"{base}.restored-{int(datetime.now().timestamp())}{ext}"

    if os.path.isdir(source):
        shutil.copytree(source, dest_path)
    else:
        shutil.copy2(source, dest_path)
    return {
        "ok": True,
        "restored_to": dest_path,
        "message": f"已从快照取回：{dest_path}",
    }
