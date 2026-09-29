"""
NAS Safe — 存储系统探测适配层

设计原则：
  1. 只读优先：探测阶段绝不修改任何数据
  2. 命令封装：所有 btrfs/zfs 操作集中在此，便于审计
  3. 品牌无关：通过实际命令探测，不硬编码品牌判断
  4. 优雅降级：命令不存在时给出可读的提示，而非抛异常

适配范围（v1.0）：
  飞牛 fnOS / 裸 Linux / TrueNAS / Unraid / OMV / 群晖(开SSH) / 威联通(开SSH)

安全约定：
  - 所有外部命令通过 subprocess 列表参数调用，绝不使用 shell=True
  - 路径参数一律经过 _validate_path 校验，防止命令注入
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import datetime
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class StorageError(Exception):
    """存储操作失败的统一异常。"""


class CommandNotFound(StorageError):
    """系统未安装所需命令（如 btrfs）。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Volume:
    """一个可快照的存储单元（btrfs 子卷 / ZFS 数据集 / QNAP 卷）。"""
    name: str                 # 展示名
    mountpoint: str           # 挂载点（QNAP 下为数字卷 ID）
    fs_type: str              # 'btrfs' | 'zfs' | 'qnap'
    uuid: Optional[str] = None
    device: Optional[str] = None
    snapshot_dir: Optional[str] = None   # 快照存放目录
    snapshots: list = field(default_factory=list)
    volume_id: Optional[str] = None      # QNAP 数字卷 ID（如 "2"）
    backend: str = "fs"                 # 'fs' | 'qnap'

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Snapshot:
    """一次快照记录。"""
    name: str
    volume: str
    created_at: Optional[str] = None
    path: Optional[str] = None        # 快照的实体路径（可挂载浏览）
    size_bytes: Optional[int] = None
    readonly: bool = True
    description: str = ""
    fs_type: str = ""                 # 'btrfs' | 'zfs' | 'qnap'
    snapshot_id: Optional[str] = None  # QNAP 数字快照 ID（如 "10001"）
    vital: bool = False                # QNAP 锁定标记（永久保留）
    status: Optional[str] = None       # QNAP 状态（Ready / Removing...）
    mount_path: Optional[str] = None   # QNAP 快照只读挂载点（本地模式用）
    backend: str = "fs"                # 'fs' | 'qnap'

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemProfile:
    """当前 NAS 的能力画像 —— 决定 UI 上显示什么、隐藏什么。"""
    os_name: str = "unknown"          # 展示用的系统名
    os_id: str = "unknown"            # 归一化标识：fnos/ugreen/truenas/unraid/omv/synology/qnap/generic
    fs_available: list = field(default_factory=list)   # ['btrfs', 'zfs']
    has_btrfs_cmd: bool = False
    has_zfs_cmd: bool = False
    has_httm: bool = False            # 单文件取回后端
    has_qcli: bool = False            # 威联通 QTS 官方快照 CLI 可用
    is_container: bool = False
    has_systemd: bool = False
    kernel: str = ""
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------

# 允许出现在路径参数里的字符白名单（保守但足够）
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_\-./@: ]+$")


def _validate_path(path: str) -> str:
    """校验路径，防止命令注入。

    这不是万无一失的安全边界（真正的边界是列表参数调用 + 不使用 shell），
    但它能拦住明显的恶意输入。
    """
    if not path or not isinstance(path, str):
        raise StorageError("路径不能为空")
    if "\x00" in path:
        raise StorageError("路径包含非法字符")
    if not path.startswith("/"):
        raise StorageError(f"路径必须是绝对路径: {path}")
    if not _SAFE_PATH_RE.match(path):
        raise StorageError(f"路径包含不支持的字符: {path}")
    if ".." in path.split("/"):
        raise StorageError(f"路径不允许包含 .. : {path}")
    return path


def run(cmd: list[str], timeout: int = 30, check: bool = True) -> str:
    """执行外部命令。

    始终使用列表参数 + shell=False，避免注入。
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise CommandNotFound(f"命令不存在: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise StorageError(f"命令超时: {' '.join(cmd)}") from exc

    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise StorageError(f"命令失败 ({proc.returncode}): {' '.join(cmd)}\n{detail}")

    return proc.stdout


def which(name: str) -> Optional[str]:
    return shutil.which(name)


# ---------------------------------------------------------------------------
# 系统探测
# ---------------------------------------------------------------------------

# 品牌识别规则：按优先级匹配 os-release 的 ID / ID_LIKE / NAME
_BRAND_RULES: list[tuple[str, str]] = [
    (r"fnos|feiniu|飞牛", "fnos"),
    (r"ugos", "ugreen"),
    (r"truenas|freenas", "truenas"),
    (r"unraid", "unraid"),
    (r"openmediavault", "omv"),
    (r"dsm|synology", "synology"),
    (r"qts|qnap|quts", "qnap"),
]


def _read_os_release() -> dict:
    """读取 /etc/os-release。"""
    data: dict[str, str] = {}
    for candidate in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    data[key.strip()] = val.strip().strip('"').strip("'")
            if data:
                break
        except OSError:
            continue
    return data


def _detect_brand(os_release: dict) -> tuple[str, str]:
    """返回 (os_id, os_name)。"""
    haystack = " ".join(
        os_release.get(k, "") for k in ("ID", "ID_LIKE", "NAME", "PRETTY_NAME")
    ).lower()

    for pattern, brand in _BRAND_RULES:
        if re.search(pattern, haystack, re.IGNORECASE):
            return brand, os_release.get("PRETTY_NAME") or os_release.get("NAME") or brand

    return "generic", os_release.get("PRETTY_NAME") or os_release.get("NAME") or "Linux"


def _is_container() -> bool:
    """判断是否运行在容器内。"""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return any(marker in content for marker in ("docker", "containerd", "kubepods", "lxc"))
    except OSError:
        return False


def probe_system() -> SystemProfile:
    """探测当前系统能力。只读操作，不做任何修改。"""
    os_release = _read_os_release()
    os_id, os_name = _detect_brand(os_release)

    profile = SystemProfile(
        os_name=os_name,
        os_id=os_id,
        has_btrfs_cmd=which("btrfs") is not None,
        has_zfs_cmd=which("zfs") is not None,
        has_httm=which("httm") is not None,
        has_qcli=which("qcli") is not None,
        is_container=_is_container(),
        has_systemd=os.path.isdir("/run/systemd/system"),
    )

    try:
        profile.kernel = os.uname().release
    except AttributeError:
        profile.kernel = "unknown"

    if profile.has_btrfs_cmd:
        profile.fs_available.append("btrfs")
    if profile.has_zfs_cmd:
        profile.fs_available.append("zfs")

    # 给出可读的警告，指导用户
    if not profile.fs_available:
        profile.warnings.append(
            "未检测到 btrfs 或 zfs 命令。快照功能需要底层文件系统支持，"
            "请确认存储池使用的是 btrfs 或 ZFS 格式。"
        )
    if not profile.has_httm:
        profile.warnings.append(
            "未检测到 httm，单文件取回将使用内置的降级方案（直接浏览快照目录）。"
        )
    if profile.has_qcli and not profile.has_btrfs_cmd and not profile.has_zfs_cmd:
        profile.fs_available.append("qnap")
        profile.warnings.append(
            "检测到 QNAP qcli，已启用官方块级快照接口（LVM 瘦快照）。"
            "创建快照默认永久锁定(vital=1)，勒索软件无法催删。"
        )
    if not profile.is_container and not profile.has_systemd:
        profile.warnings.append("未检测到 systemd，定时任务将使用内置调度器。")

    return profile


# ---------------------------------------------------------------------------
# btrfs 适配
# ---------------------------------------------------------------------------

def _btrfs_mounts() -> list[tuple[str, str]]:
    """解析 /proc/mounts，返回 [(mountpoint, device), ...]，仅 btrfs。"""
    results: list[tuple[str, str]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mountpoint, fstype = parts[0], parts[1], parts[2]
                if fstype == "btrfs":
                    results.append((mountpoint, device))
    except OSError as exc:
        raise StorageError(f"无法读取 /proc/mounts: {exc}") from exc
    return results


def _btrfs_subvolumes(mountpoint: str) -> list[dict]:
    """列出某个 btrfs 挂载点下的子卷。"""
    try:
        out = run(["btrfs", "subvolume", "list", "-o", mountpoint], timeout=30)
    except StorageError:
        return []

    subvols = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("ID "):
            continue
        # 格式: ID 256 gen 1234 top level 5 path @data
        m = re.match(r"ID\s+(\d+)\s+gen\s+(\d+)\s+top level\s+(\d+)\s+path\s+(.+)$", line)
        if not m:
            continue
        subvol_id, _gen, _top, path = m.groups()
        subvols.append({
            "id": int(subvol_id),
            "path": path.strip(),
            "full_path": os.path.join(mountpoint, path.strip()),
        })
    return subvols


def _btrfs_snapshots_of(mountpoint: str) -> list[dict]:
    """列出某个 btrfs 挂载点下的只读快照。"""
    try:
        out = run(["btrfs", "subvolume", "list", "-s", "-o", mountpoint], timeout=30)
    except StorageError:
        return []

    snaps = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("ID "):
            continue
        m = re.match(r"ID\s+(\d+)\s+gen\s+\d+\s+cgen\s+(\d+)\s+top level\s+\d+\s+otime\s+(\S+ \S+)\s+path\s+(.+)$", line)
        if m:
            _sid, _cgen, otime, path = m.groups()
            snaps.append({"path": path.strip(), "created_at": otime})
            continue
        m = re.match(r"ID\s+(\d+)\s+gen\s+\d+\s+top level\s+\d+\s+path\s+(.+)$", line)
        if m:
            snaps.append({"path": m.group(2).strip(), "created_at": None})
    return snaps


def list_btrfs_volumes() -> list[Volume]:
    """枚举所有 btrfs 存储单元。"""
    volumes: list[Volume] = []
    seen: set[str] = set()

    for mountpoint, device in _btrfs_mounts():
        if mountpoint in seen:
            continue
        seen.add(mountpoint)

        subvols = _btrfs_subvolumes(mountpoint)
        # 顶层挂载点本身也是一个可快照单元
        if not subvols:
            volumes.append(Volume(
                name=os.path.basename(mountpoint) or mountpoint,
                mountpoint=mountpoint,
                fs_type="btrfs",
                device=device,
                snapshot_dir=os.path.join(mountpoint, ".nassafe", "snapshots"),
            ))
            continue

        for sv in subvols:
            volumes.append(Volume(
                name=sv["path"],
                mountpoint=sv["full_path"],
                fs_type="btrfs",
                device=device,
                snapshot_dir=os.path.join(mountpoint, ".nassafe", "snapshots"),
            ))

    return volumes


def list_btrfs_snapshots(volume_mountpoint: str) -> list[Snapshot]:
    """列出某个 btrfs 单元的快照。"""
    _validate_path(volume_mountpoint)
    snaps = []
    for item in _btrfs_snapshots_of(volume_mountpoint):
        snaps.append(Snapshot(
            name=os.path.basename(item["path"]),
            volume=volume_mountpoint,
            created_at=item.get("created_at"),
            path=item["path"],
        ))
    return snaps


def create_btrfs_snapshot(volume_mountpoint: str, snapshot_dir: str, name: str) -> Snapshot:
    """为 btrfs 单元创建只读快照。

    -r 参数确保快照只读 —— 这是防勒索的基础。
    """
    _validate_path(volume_mountpoint)
    _validate_path(snapshot_dir)
    if not re.match(r"^[A-Za-z0-9_\-]+$", name):
        raise StorageError(f"快照名只允许字母数字横线下划线: {name}")

    os.makedirs(snapshot_dir, exist_ok=True)
    target = os.path.join(snapshot_dir, name)
    if os.path.exists(target):
        raise StorageError(f"快照已存在: {target}")

    # -r 创建只读快照，无法被意外修改
    run(["btrfs", "subvolume", "snapshot", "-r", volume_mountpoint, target], timeout=120)

    return Snapshot(
        name=name,
        volume=volume_mountpoint,
        path=target,
        readonly=True,
    )


def delete_btrfs_snapshot(snapshot_path: str) -> None:
    """删除 btrfs 快照。调用方必须先做安全校验与二次确认。"""
    _validate_path(snapshot_path)
    run(["btrfs", "subvolume", "delete", snapshot_path], timeout=120)


# ---------------------------------------------------------------------------
# ZFS 适配
# ---------------------------------------------------------------------------

def list_zfs_datasets() -> list[Volume]:
    """枚举有挂载点的 ZFS 数据集。"""
    try:
        out = run([
            "zfs", "list", "-H", "-o", "name,mountpoint,type",
            "-t", "filesystem",
        ], timeout=30)
    except (StorageError, CommandNotFound):
        return []

    volumes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, mountpoint, dtype = parts[0], parts[1], parts[2]
        if mountpoint in ("-", "none", "legacy"):
            continue
        volumes.append(Volume(
            name=name,
            mountpoint=mountpoint,
            fs_type="zfs",
            snapshot_dir=f"{name}/.nassafe",
        ))
    return volumes


def list_zfs_snapshots(dataset: str) -> list[Snapshot]:
    """列出某个 ZFS 数据集的快照。"""
    if not re.match(r"^[A-Za-z0-9_\-./:]+$", dataset):
        raise StorageError(f"数据集名非法: {dataset}")

    try:
        out = run([
            "zfs", "list", "-H", "-p", "-o", "name,creation,used",
            "-t", "snapshot", "-r", dataset,
        ], timeout=30)
    except StorageError:
        return []

    snaps = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        full_name, creation, used = parts[0], parts[1], parts[2]
        snap_name = full_name.split("@", 1)[-1]
        snaps.append(Snapshot(
            name=snap_name,
            volume=dataset,
            created_at=creation,
            path=full_name,
            size_bytes=int(used) if used.isdigit() else None,
        ))
    return snaps


def create_zfs_snapshot(dataset: str, name: str) -> Snapshot:
    """创建 ZFS 快照。ZFS 快照天然只读。"""
    if not re.match(r"^[A-Za-z0-9_\-./:]+$", dataset):
        raise StorageError(f"数据集名非法: {dataset}")
    if not re.match(r"^[A-Za-z0-9_\-]+$", name):
        raise StorageError(f"快照名只允许字母数字横线下划线: {name}")

    full = f"{dataset}@{name}"
    run(["zfs", "snapshot", full], timeout=120)
    return Snapshot(name=name, volume=dataset, path=full, readonly=True)


def hold_zfs_snapshot(snapshot_full_name: str, tag: str = "nassafe") -> None:
    """给 ZFS 快照加 hold，阻止被删除。

    注意：这不是真正的不可变 —— 有 root 权限的攻击者可以
    `zfs release` 再 `zfs destroy`。真正的不可变需要复制到独立系统。
    """
    if not re.match(r"^[A-Za-z0-9_\-./:@]+$", snapshot_full_name):
        raise StorageError(f"快照名非法: {snapshot_full_name}")
    run(["zfs", "hold", tag, snapshot_full_name], timeout=30)


def list_zfs_holds(snapshot_full_name: str) -> list[str]:
    """列出快照上的 hold 标记。"""
    if not re.match(r"^[A-Za-z0-9_\-./:@]+$", snapshot_full_name):
        raise StorageError(f"快照名非法: {snapshot_full_name}")
    try:
        out = run(["zfs", "holds", "-H", snapshot_full_name], timeout=30)
    except StorageError:
        return []
    return [line.split("\t")[-1].strip() for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def list_all_volumes() -> list[Volume]:
    """枚举所有可快照的存储单元（btrfs + zfs + qnap）。"""
    volumes: list[Volume] = []
    if which("btrfs"):
        try:
            volumes.extend(list_btrfs_volumes())
        except StorageError:
            pass
    if which("zfs"):
        try:
            volumes.extend(list_zfs_datasets())
        except StorageError:
            pass
    if which("qcli"):
        try:
            volumes.extend(list_qnap_volumes())
        except StorageError:
            pass
    return volumes


def list_all_snapshots(volume: Volume) -> list[Snapshot]:
    """按单元类型分派到对应实现。"""
    if volume.fs_type == "btrfs":
        return list_btrfs_snapshots(volume.mountpoint)
    if volume.fs_type == "zfs":
        return list_zfs_snapshots(volume.name)
    if volume.fs_type == "qnap":
        return list_qnap_snapshots(volume.volume_id or volume.mountpoint)
    raise StorageError(f"不支持的文件系统: {volume.fs_type}")


# ---------------------------------------------------------------------------
# QNAP（威联通）适配 —— 通过官方 qcli_volumesnapshot CLI
# ---------------------------------------------------------------------------

def list_qnap_volumes() -> list[Volume]:
    """把 QNAP 卷包装成统一的 Volume 结构。"""
    from qnap import list_volumes as _qv
    out: list[Volume] = []
    for v in _qv():
        out.append(Volume(
            name=v.alias or f"volume{v.volume_id}",
            mountpoint=v.volume_id,      # 用数字 ID 作为查找键
            fs_type="qnap",
            volume_id=v.volume_id,
            backend="qnap",
            device=f"qnap:{v.volume_id}",
        ))
    return out


def list_qnap_snapshots(volume_id: str) -> list[Snapshot]:
    """把 QNAP 快照包装成统一的 Snapshot 结构。"""
    from qnap import list_snapshots as _qs
    out: list[Snapshot] = []
    for s in _qs(volume_id):
        out.append(Snapshot(
            name=s.name,
            volume=volume_id,
            created_at=s.created_at,
            snapshot_id=s.snapshot_id,
            vital=s.vital,
            status=s.status,
            readonly=True,
            fs_type="qnap",
            backend="qnap",
            path=None,    # QNAP 快照经 mount 接口浏览，非直接文件系统路径
            mount_path=f"/mnt/snapshot/{volume_id}/{s.snapshot_id}",
        ))
    return out


def create_snapshot(volume: Volume, name: str, vital: bool = True) -> Snapshot:
    """统一创建快照入口，按 fs_type 分派。

    QNAP 分支默认 vital=1（永久锁定）—— 防勒索的核心保障。
    """
    if volume.fs_type == "btrfs":
        snap_dir = volume.snapshot_dir or os.path.join(volume.mountpoint, ".nassafe", "snapshots")
        snap = create_btrfs_snapshot(volume.mountpoint, snap_dir, name)
        snap.fs_type = "btrfs"
        return snap
    if volume.fs_type == "zfs":
        snap = create_zfs_snapshot(volume.name, name)
        snap.fs_type = "zfs"
        return snap
    if volume.fs_type == "qnap":
        from qnap import create_snapshot as _qc
        s = _qc(volume.volume_id or volume.mountpoint, name, vital=vital)
        return Snapshot(
            name=s.name,
            volume=volume.volume_id or volume.mountpoint,
            created_at=s.created_at,
            snapshot_id=s.snapshot_id,
            vital=s.vital,
            status=s.status,
            readonly=True,
            fs_type="qnap",
        )
    raise StorageError(f"不支持的文件系统: {volume.fs_type}")


def delete_snapshot(snapshot: Snapshot) -> None:
    """统一删除快照入口，按类型分派。绝不包含回滚(revert)操作。"""
    if snapshot.fs_type == "qnap" and snapshot.snapshot_id:
        from qnap import delete_snapshot as _qd
        _qd(snapshot.snapshot_id)
        return
    if snapshot.fs_type == "btrfs":
        delete_btrfs_snapshot(snapshot.path)
        return
    if snapshot.fs_type == "zfs":
        run(["zfs", "destroy", snapshot.path], timeout=120)
        return
    raise StorageError(f"无法删除该类型快照: {snapshot.fs_type}")


# ---------------------------------------------------------------------------
# 快照浏览 / 单文件取回
# ---------------------------------------------------------------------------

def human_size(num_bytes: Optional[int]) -> str:
    """把字节数转成人类可读字符串。"""
    if num_bytes is None:
        return ""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _browse_local_dir(path: str) -> list[dict]:
    """列举本地目录内容（提取自原 app.build_browse 核心逻辑）。

    返回 entries 列表，每项含 name/path/is_dir/size/size_human/mtime。
    安全校验（白名单、注入检查）由调用方负责。
    """
    entries: list[dict] = []
    for name in sorted(os.listdir(path)):
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        is_dir = os.path.isdir(full)
        entries.append({
            "name": name,
            "path": full,
            "is_dir": is_dir,
            "size": None if is_dir else st.st_size,
            "size_human": "" if is_dir else human_size(st.st_size),
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return entries


def browse_snapshot(snapshot: Snapshot, subpath: str = "") -> dict:
    """统一浏览快照内文件。按 backend 分派。

    - qnap + 本地模式（server 跑在 QTS 宿主且能直接访问挂载点）：直接列目录
    - qnap + SSH 模式（远程管理）：经 qcli/list 解析
    - fs（btrfs/zfs）：直接列快照目录
    """
    if getattr(snapshot, "backend", "fs") == "qnap" or snapshot.fs_type == "qnap":
        from qnap import default_client, SNAP_MOUNT_ROOT
        client = default_client()
        try:
            if (client.mode == "local"
                    and snapshot.mount_path
                    and os.path.isdir(snapshot.mount_path)):
                root = snapshot.mount_path
                full = os.path.normpath(os.path.join(root, subpath)) if subpath else root
                return {
                    "ok": True, "backend": "qnap", "local": True,
                    "path": full, "subpath": subpath,
                    "entries": _browse_local_dir(full),
                }
            raw = client.list_dir(
                snapshot.volume_id or snapshot.volume,
                snapshot.snapshot_id,
                subpath,
            )
            entries = []
            for e in raw:
                rel = (subpath.rstrip("/") + "/" + e["name"]).lstrip("/")
                entries.append({
                    "name": e["name"],
                    "path": rel,
                    "is_dir": e["is_dir"],
                    "size": e["size"],
                    "size_human": human_size(e["size"]) if e["size"] else "",
                    "mtime": None,
                })
            return {
                "ok": True, "backend": "qnap", "local": False,
                "subpath": subpath, "entries": entries,
            }
        finally:
            client.close()

    # fs 分支
    root = snapshot.path or snapshot.mount_path
    if not root:
        raise StorageError("快照缺少可浏览的本地路径")
    full = os.path.normpath(os.path.join(root, subpath)) if subpath else root
    return {
        "ok": True, "backend": "fs", "local": True,
        "path": full, "subpath": subpath,
        "entries": _browse_local_dir(full),
    }


def restore_from_snapshot(snapshot: Snapshot, rel_path: str, dest: str) -> dict:
    """统一取回快照内单文件。绝不包含回滚操作。

    返回 {ok, restored_to, message}。
    """
    if ".." in rel_path.split("/"):
        raise StorageError("相对路径非法")

    if getattr(snapshot, "backend", "fs") == "qnap" or snapshot.fs_type == "qnap":
        from qnap import default_client
        client = default_client()
        try:
            restored_to = client.restore_file(
                snapshot.volume_id or snapshot.volume,
                snapshot.snapshot_id,
                rel_path,
                dest,
            )
        finally:
            client.close()
        return {
            "ok": True,
            "restored_to": restored_to,
            "message": f"已从快照取回：{restored_to}",
        }

    # fs 分支：本地直接复制，绝不覆盖已存在文件
    source = os.path.join(snapshot.path or "", rel_path.lstrip("/"))
    source_real = os.path.realpath(source)
    snap_real = os.path.realpath(snapshot.path or "")
    if not (source_real == snap_real or source_real.startswith(snap_real + os.sep)):
        raise StorageError("路径越权：源文件必须在快照目录内")
    if not os.path.exists(source_real):
        raise StorageError(f"快照中不存在该文件: {rel_path}")

    dest_path = os.path.join(dest, os.path.basename(source_real)) if os.path.isdir(dest) else dest
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(dest_path)
        dest_path = f"{base}.restored-{int(time.time())}{ext}"
    os.makedirs(dest, exist_ok=True)
    if os.path.isdir(source_real):
        shutil.copytree(source_real, dest_path)
    else:
        shutil.copy2(source_real, dest_path)
    return {
        "ok": True,
        "restored_to": dest_path,
        "message": f"已恢复到：{dest_path}",
    }

