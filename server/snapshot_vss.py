"""Windows 卷影复制 (VSS) 快照后端——真实实现。

原理：VSS 是块级写时复制（COW），创建快照零拷贝、瞬间完成；源文件被
勒索软件原地加密时，影子副本里的旧内容不受影响（真防勒索）。

实现路线：
  - 枚举保护目标：固定磁盘（GetDriveTypeW == DRIVE_FIXED）作为可保护卷
  - 创建快照：`vssadmin create shadow /for=<盘符>`，正则解析输出里的
    Shadow Copy ID 与 GLOBALROOT 设备名（中英文系统输出均兼容）
  - 元数据：存 state/vssrepo/<盘符slug>/<时间戳>.json（块级快照零文件拷贝）
  - 浏览：直接用 Windows 文件 API 走 \\\\?\\GLOBALROOT\\Device\\...ShadowCopyN\\
  - 取回：shutil 复制（失败回退 robocopy）
  - 删除：`vssadmin delete shadows /shadow=<ID> /quiet`

已知限制（如实告知用户）：
  - 需要管理员权限运行；无权限时本模块安全降级（list_volumes 返回空）
  - vssadmin 创建的是非持久快照：机器重启后消失（系统管理的还原点除外）。
    防勒索价值在「实时可取回」，持久化保留是后续版本课题。
  - VSS 存储区默认卷容量 10%，塞满后系统自动淘汰最老快照（与我们 meta
    里记录的 ID 失配 → list 时自动清理孤儿元数据）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import string
import subprocess
import sys
from datetime import datetime

from storage import StorageError

# ---------------------------------------------------------------------------

_MAX_SNAPSHOTS_PER_VOL = 30   # 每个卷最多保留的元数据条数（VSS 本身也会自动淘汰）
_TIMEOUT = 120                # vssadmin 单命令超时（秒）


def _which(cmd: str) -> bool:
    try:
        subprocess.run([cmd, "/?"], capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _state_root() -> str:
    from storage import state_dir
    return state_dir()


def _slug(text: str, limit: int = 16) -> str:
    return re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", text).strip("-").lower()[:limit] or "vol"


def is_admin() -> bool:
    """当前进程是否有管理员权限。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _need_windows() -> None:
    if not sys.platform.startswith("win"):
        raise StorageError("VSS 快照只能在 Windows 上使用")


def _need_admin() -> None:
    if not is_admin():
        raise StorageError(
            "创建/删除卷影副本需要管理员权限：请以管理员身份重新启动 NAS Safe"
        )


def _run_vssadmin(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["vssadmin", *args], capture_output=True, timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        raise StorageError("系统里没有 vssadmin 工具（Windows 精简版可能被移除）")
    except subprocess.TimeoutExpired:
        raise StorageError("vssadmin 命令超时")


def _decode(proc: subprocess.CompletedProcess) -> str:
    """vssadmin 输出编码：中文系统 GBK、英文系统可能 cp437/utf-8，逐级尝试。"""
    raw = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    for enc in ("utf-8", "gbk", "cp936", "cp437", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 保护目标枚举
# ---------------------------------------------------------------------------

def _fixed_drives() -> list[str]:
    """枚举固定磁盘盘符（排除光驱/U盘/网络盘/软盘）。"""
    drives: list[str] = []
    try:
        import ctypes
        GetDriveTypeW = ctypes.windll.kernel32.GetDriveTypeW
    except Exception:  # noqa: BLE001
        return drives
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            # 3 = DRIVE_FIXED
            if GetDriveTypeW(root) == 3:
                drives.append(root)
        except Exception:  # noqa: BLE001
            continue
    return drives


def _drive_label(root: str) -> str:
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
        if ok and buf.value:
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    return ""


def list_volumes() -> list:
    """枚举可保护的固定磁盘卷。

    无管理员权限时也列出（用户能看到保护目标），真正的权限校验
    放在 create/delete 时给出明确提示。
    """
    _need_windows()

    from storage import Volume

    repo_root = os.path.abspath(_state_root())
    volumes: list = []
    for root in _fixed_drives():
        letter = root[0]
        # 快照仓库所在盘也允许保护（meta 很小），但不保护 RAM 盘/虚拟盘
        label = _drive_label(root)
        name = f"本地磁盘 {letter}:" + (f" {label}" if label else "")
        slug = _slug(f"vss-{letter}")
        volumes.append(Volume(
            name=name,
            mountpoint=root,
            fs_type="vss",
            snapshot_dir=os.path.join(repo_root, slug),
            backend="fs",
        ))
    return volumes


# ---------------------------------------------------------------------------
# 影子副本操作
# ---------------------------------------------------------------------------

_RE_SHADOW_ID = re.compile(r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}")
_RE_DEVICE = re.compile(r"HarddiskVolumeShadowCopy\d+")


def _create_shadow(drive_letter: str) -> dict:
    r"""创建影子副本，返回 {"id": "{guid}", "device": r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN"}。"""
    proc = _run_vssadmin(["create", "shadow", f"/for={drive_letter}:"])
    out = _decode(proc)
    m_id = _RE_SHADOW_ID.search(out)
    m_dev = _RE_DEVICE.search(out)
    if not m_dev:
        err = out.strip().splitlines()
        detail = err[-1] if err else "未知错误"
        raise StorageError(f"创建卷影副本失败：{detail[:120]}")
    return {
        "id": m_id.group(0) if m_id else "",
        "device": f"\\\\?\\GLOBALROOT\\Device\\{m_dev.group(0)}",
    }


def _shadow_alive(device: str) -> bool:
    """影子副本是否仍存在（非持久快照重启后会消失）。"""
    if not device:
        return False
    return os.path.isdir(device.rstrip("\\/") + "\\")


def _delete_shadow(shadow_id: str) -> bool:
    if not shadow_id:
        return False
    proc = _run_vssadmin(["delete", "shadows", f"/shadow={shadow_id}", "/quiet"])
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# 快照元数据（块级快照零拷贝，只存索引）
# ---------------------------------------------------------------------------

def _repo_for(volume) -> str:
    base = getattr(volume, "snapshot_dir", "") or os.path.join(
        _state_root(), _slug("vss-" + getattr(volume, "mountpoint", "vol")))
    os.makedirs(base, exist_ok=True)
    return base


def _meta_path(repo: str, stamp: str) -> str:
    return os.path.join(repo, f"{stamp}.json")


def _read_meta(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_meta(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _prune_repo(repo: str) -> None:
    """超出保留数时删最老的元数据（并尝试删对应影子副本）。"""
    files = sorted(f for f in os.listdir(repo) if f.endswith(".json"))
    extra = files[:-_MAX_SNAPSHOTS_PER_VOL] if len(files) > _MAX_SNAPSHOTS_PER_VOL else []
    for f in extra:
        meta = _read_meta(os.path.join(repo, f))
        _delete_shadow(meta.get("shadow_id", ""))
        try:
            os.remove(os.path.join(repo, f))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 统一接口
# ---------------------------------------------------------------------------

def list_snapshots(volume) -> list:
    from storage import Snapshot

    repo = _repo_for(volume)
    out: list = []
    for fname in sorted(os.listdir(repo), reverse=True):
        if not fname.endswith(".json"):
            continue
        meta = _read_meta(os.path.join(repo, fname))
        device = meta.get("device", "")
        alive = _shadow_alive(device)
        if not alive:
            # 影子副本已消失（重启/系统淘汰）：清孤儿元数据
            try:
                os.remove(os.path.join(repo, fname))
            except OSError:
                pass
            continue
        out.append(Snapshot(
            name=meta.get("name") or fname[:-5],
            volume=getattr(volume, "name", meta.get("volume", "")),
            created_at=meta.get("created_at"),
            path=device,               # 影子副本设备路径，即"实体路径"
            size_bytes=0,              # 块级 COW 零拷贝，无独立体积
            readonly=True,
            description=meta.get("description") or "卷影副本 · 零拷贝 · 源盘被加密也不影响",
            fs_type="vss",
            vital=bool(meta.get("vital", False)),
            backend="fs",
        ))
    return out


def create_snapshot(volume, name: str, vital: bool = True):
    from storage import Snapshot

    _need_windows()
    _need_admin()

    mount = getattr(volume, "mountpoint", "")
    letter = (mount or "C")[0]
    if not os.path.isdir(f"{letter}:\\"):
        raise StorageError(f"保护卷不存在: {mount}")

    shadow = _create_shadow(letter)
    created = datetime.now().isoformat(timespec="seconds")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    repo = _repo_for(volume)
    _write_meta(_meta_path(repo, stamp), {
        "name": name,
        "volume": getattr(volume, "name", ""),
        "drive": f"{letter}:",
        "shadow_id": shadow["id"],
        "device": shadow["device"],
        "created_at": created,
        "vital": bool(vital),
        "description": f"卷影副本 · 零拷贝 · {letter} 盘",
    })
    _prune_repo(repo)

    return Snapshot(
        name=name,
        volume=getattr(volume, "name", ""),
        created_at=created,
        path=shadow["device"],
        size_bytes=0,
        readonly=True,
        description=f"卷影副本 · 零拷贝 · {letter} 盘",
        fs_type="vss",
        vital=bool(vital),
        backend="fs",
    )


def delete_snapshot(snapshot) -> None:
    _need_windows()
    _need_admin()

    path = getattr(snapshot, "path", "")
    shadow_id = ""
    # 从元数据里找 shadow_id（path 是设备名，vssadmin 删除要 GUID）
    repo_root = os.path.abspath(_state_root())
    for root_dir, _d, files in os.walk(repo_root):
        for f in files:
            if not f.endswith(".json"):
                continue
            meta = _read_meta(os.path.join(root_dir, f))
            if meta.get("device") == path:
                shadow_id = meta.get("shadow_id", "")
                try:
                    os.remove(os.path.join(root_dir, f))
                except OSError:
                    pass
                break
        if shadow_id or meta.get("device") == path:
            break
    if not shadow_id:
        # 允许直接传 shadow_id 或设备路径删除
        if _RE_SHADOW_ID.fullmatch(path or ""):
            shadow_id = path
    if not shadow_id:
        raise StorageError("找不到该快照的卷影 ID，可能已被系统清理")
    if not _delete_shadow(shadow_id):
        raise StorageError("删除卷影副本失败（可能已被 VSS 自动淘汰）")


# ---------------------------------------------------------------------------
# 浏览 / 取回
# ---------------------------------------------------------------------------

def _safe_join(root: str, subpath: str) -> str:
    """拼影子副本内的路径并防越权（挡 .. 与盘符切换）。"""
    base = root.rstrip("\\/") 
    sub = (subpath or "").replace("/", "\\").strip("\\")
    if any(p in ("..",) for p in sub.split("\\")):
        raise StorageError("路径越权：不能包含 ..")
    full = f"{base}\\{sub}" if sub else base
    if not full.lower().startswith(base.lower()):
        raise StorageError("路径越权")
    return full


def browse_snapshot(snapshot, subpath: str = "") -> dict:
    from storage import _browse_local_dir

    root = getattr(snapshot, "path", "")
    if not _shadow_alive(root):
        raise StorageError("卷影副本已失效（机器重启或被系统淘汰），请重新创建快照")
    full = _safe_join(root, subpath)
    if not os.path.isdir(full):
        raise StorageError(f"不是目录: {subpath or '/'}")
    return {
        "ok": True,
        "backend": "vss",
        "local": True,
        "path": full,
        "subpath": subpath,
        "entries": _browse_local_dir(full),
    }


def restore_from_snapshot(snapshot, rel_path: str, dest: str) -> dict:
    """从卷影副本取回文件/目录，绝不覆盖已存在的文件。"""
    root = getattr(snapshot, "path", "")
    if not _shadow_alive(root):
        raise StorageError("卷影副本已失效（机器重启或被系统淘汰），请重新创建快照")
    source = _safe_join(root, rel_path)
    if not os.path.exists(source):
        raise StorageError(f"快照中不存在：{rel_path}")

    os.makedirs(dest, exist_ok=True)
    dest_path = os.path.join(dest, os.path.basename(source)) if os.path.isdir(dest) else dest
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(dest_path)
        dest_path = f"{base}.restored-{int(datetime.now().timestamp())}{ext}"

    if os.path.isdir(source):
        # 影子副本目录：优先 robocopy（Windows 原生，处理长路径/权限更稳）
        if _which("robocopy"):
            # robocopy 返回码 >= 8 才是错误，1-7 都是成功态
            proc = subprocess.run(
                ["robocopy", source, dest_path, "/E", "/COPY:DT", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH"],
                capture_output=True, timeout=3600)
            if proc.returncode >= 8:
                raise StorageError(f"取回目录失败（robocopy 代码 {proc.returncode}）")
        else:
            shutil.copytree(source, dest_path)
    else:
        try:
            shutil.copy2(source, dest_path)
        except (OSError, shutil.Error):
            if _which("robocopy"):
                proc = subprocess.run(
                    ["robocopy", os.path.dirname(source), os.path.dirname(dest_path),
                     os.path.basename(source), "/COPY:DT", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH"],
                    capture_output=True, timeout=3600)
                if proc.returncode >= 8:
                    raise StorageError(f"取回文件失败（robocopy 代码 {proc.returncode}）")
                dest_path = os.path.join(os.path.dirname(dest_path), os.path.basename(source))
            else:
                raise
    return {
        "ok": True,
        "restored_to": dest_path,
        "message": f"已从卷影副本取回：{dest_path}",
    }
