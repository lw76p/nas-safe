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
    """一个可快照的存储单元（btrfs 子卷 / ZFS 数据集）。"""
    name: str                 # 展示名
    mountpoint: str           # 挂载点
    fs_type: str              # 'btrfs' | 'zfs'
    uuid: Optional[str] = None
    device: Optional[str] = None
    snapshot_dir: Optional[str] = None   # 快照存放目录
    snapshots: list = field(default_factory=list)

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
    """枚举所有可快照的存储单元（btrfs + zfs）。"""
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
    return volumes


def list_all_snapshots(volume: Volume) -> list[Snapshot]:
    """按单元类型分派到对应实现。"""
    if volume.fs_type == "btrfs":
        return list_btrfs_snapshots(volume.mountpoint)
    if volume.fs_type == "zfs":
        return list_zfs_snapshots(volume.name)
    raise StorageError(f"不支持的文件系统: {volume.fs_type}")
