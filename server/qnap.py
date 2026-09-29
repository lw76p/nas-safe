"""
NAS Safe — 威联通 QTS 官方快照适配层（B 类档位）

威联通 QTS 底层是 LVM + ext4，块级快照走官方 qcli_volumesnapshot CLI，
而不是 btrfs/zfs 命令。本模块封装其 创建 / 列出 / 删除 / 锁定 接口。

实证环境（TS-873A, QTS 5.2.9, 2026-09-29 真机验证通过）：
  - 创建并锁定 : qcli_volumesnapshot -t volumeID=<id> snapshot_name=<名> vital=1
  - 列出        : qcli_volumesnapshot -l volumeID=<id>
  - 删除        : qcli_volumesnapshot -d snapshotID=<id>     # 注意：不需要 volumeID
  - 卷列表      : qcli_volume -l
  - 登录        : qcli -l user=<u> pw='<p>' saveauthsid=yes

设计：
  - 自包含，不 import storage（避免循环依赖）；storage 以懒加载方式调用本模块。
  - 本地模式：直接 subprocess 调用 qcli（适用于把 NAS Safe 装在 QTS 主机上的场景）。
  - SSH 模式：经 paramiko 连接 QTS（适用于容器部署），参数用 shlex 转义，密码安全传递。
  - 零强制第三方依赖：paramiko 仅在 SSH 模式被懒加载。

安全红线：
  - 绝不使用 -r (revert/回滚) —— 那是把整个卷回退到快照点的破坏性操作。
  - 创建快照默认 vital=1（永久保留），勒索软件无法催删，这正是产品核心卖点。
  - 命令以列表参数 + shell=False 调用（本地）；SSH 模式对整条命令做 shlex 转义。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Optional


class QnapError(Exception):
    """QNAP 快照操作失败。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class QnapVolume:
    volume_id: str           # 数字 ID，如 "2"
    alias: str              # 展示名，如 "我的文件" / "系统盘"
    volume_type: str = ""
    encrypt: str = ""
    static: str = ""
    fsrvp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QnapSnapshot:
    snapshot_id: str         # 数字 ID，如 "10001"
    name: str
    created_at: Optional[str] = None
    vital: bool = False       # True = 永久锁定，不可被保留策略删除
    snap_type: str = ""       # Crash consistent / App consistent ...
    status: Optional[str] = None   # Ready / Removing...

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------

def parse_volumes(text: str) -> list[QnapVolume]:
    """解析 `qcli_volume -l` 输出。"""
    lines = [l.rstrip() for l in text.splitlines()]
    header_idx = None
    for i, line in enumerate(lines):
        if "volumeID" in line and "Alias" in line:
            header_idx = i
            break
    if header_idx is None:
        return []

    vols: list[QnapVolume] = []
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("volume count"):
            continue
        tokens = stripped.split()
        if len(tokens) < 4 or not tokens[0].isdigit():
            continue
        vid = tokens[0]
        vtype = tokens[1] if len(tokens) > 1 else ""
        encrypt = tokens[2] if len(tokens) > 2 else ""
        # alias 位于末两位（Staticvolume / FSRVP）之前
        if len(tokens) >= 6:
            alias = " ".join(tokens[3:-2])
            static = tokens[-2]
            fsrvp = tokens[-1]
        else:
            alias = " ".join(tokens[3:])
            static = ""
            fsrvp = ""
        vols.append(QnapVolume(
            volume_id=vid, alias=alias, volume_type=vtype,
            encrypt=encrypt, static=static, fsrvp=fsrvp,
        ))
    return vols


_SNAP_RE = re.compile(r"^(\d+)\s+(.+?)\s+(\S+)\s+([01])\s+(\S+)\s+(.+?)\s*$")


def parse_snapshots(text: str) -> list[QnapSnapshot]:
    """解析 `qcli_volumesnapshot -l volumeID=<id>` 输出。

    快照行示例（注意日期含空格，需非贪婪拆分）：
      10001  Tue Sep 29 18:17:59 2026  nassafe_test_xxx  1  Crash consistent Ready
    """
    lines = [l.rstrip() for l in text.splitlines()]
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("snapshotid"):
            header_idx = i
            break
    if header_idx is None:
        return []

    snaps: list[QnapSnapshot] = []
    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("snapshot count"):
            continue
        m = _SNAP_RE.match(stripped)
        if not m:
            continue
        sid, dt, name, vital, stype, status = m.groups()
        snaps.append(QnapSnapshot(
            snapshot_id=sid,
            name=name,
            created_at=dt.strip(),
            vital=(vital == "1"),
            snap_type=stype,
            status=status.strip(),
        ))
    return snaps


_CREATE_RE = re.compile(r"create snapshot\s+(\d+)\s+ok", re.IGNORECASE)


def parse_create_id(text: str) -> str:
    m = _CREATE_RE.search(text)
    if not m:
        low = text.lower()
        if "not defined" in low or "error" in low or "fail" in low:
            raise QnapError(f"创建快照失败: {text.strip()}")
        raise QnapError(f"无法解析创建结果: {text.strip()}")
    return m.group(1)


def parse_delete_ok(text: str) -> bool:
    low = text.lower()
    if "delete snapshot" in low and "ok" in low:
        return True
    if "not defined" in low or "error" in low or "fail" in low:
        raise QnapError(f"删除快照失败: {text.strip()}")
    raise QnapError(f"无法解析删除结果: {text.strip()}")


# ---------------------------------------------------------------------------
# 客户端（本地 / SSH 两种执行方式）
# ---------------------------------------------------------------------------

_LOCAL_QCLI = "qcli"


class QnapClient:
    """执行 qcli 命令的客户端。

    host 为 None / 127.0.0.1 / localhost 时走本地 subprocess；
    否则走 SSH（懒加载 paramiko）。
    """

    def __init__(
        self,
        host: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 60,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.timeout = timeout
        self._ssh = None
        self.mode = "local" if not host else "ssh"

    # -- 执行 ----------------------------------------------------------

    def _run(self, args: list[str]) -> str:
        if self.mode == "local":
            return self._run_local(args)
        return self._run_ssh(args)

    def _run_local(self, args: list[str]) -> str:
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True,
                timeout=self.timeout, shell=False,
            )
        except FileNotFoundError as exc:
            raise QnapError(
                "未找到 qcli 命令，确认 NAS Safe 运行在 QNAP QTS 主机上"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise QnapError(f"qcli 命令超时: {' '.join(args)}") from exc

        out = proc.stdout or ""
        err = proc.stderr or ""
        if proc.returncode != 0 and err.strip():
            raise QnapError(f"qcli 命令失败 ({proc.returncode}): {' '.join(args)}\n{err.strip()}")
        return out + err

    def _run_ssh(self, args: list[str]) -> str:
        import paramiko  # 懒加载，仅 SSH 模式需要

        if self._ssh is None:
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._ssh.connect(
                self.host, port=22, username=self.user, password=self.password,
                timeout=15, look_for_keys=False, allow_agent=False,
            )
        cmd = " ".join(shlex.quote(a) for a in args)
        stdin, stdout, stderr = self._ssh.exec_command(cmd, timeout=self.timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        return out + err

    def close(self) -> None:
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

    # -- 登录 ----------------------------------------------------------

    def login(self) -> None:
        """登录并保存会话。无凭据（user/password）时跳过，依赖已保存的 sid。"""
        if not (self.user and self.password):
            return
        self._run([
            _LOCAL_QCLI, "-l",
            f"user={self.user}", f"pw={self.password}", "saveauthsid=yes",
        ])

    # -- 业务方法 ------------------------------------------------------

    def list_volumes(self) -> list[QnapVolume]:
        self.login()
        out = self._run([_LOCAL_QCLI + "_volume", "-l"])
        return parse_volumes(out)

    def list_snapshots(self, volume_id: str) -> list[QnapSnapshot]:
        self.login()
        out = self._run([_LOCAL_QCLI + "_volumesnapshot", "-l", f"volumeID={volume_id}"])
        return parse_snapshots(out)

    def create_snapshot(
        self, volume_id: str, name: str, vital: bool = True
    ) -> QnapSnapshot:
        if not re.match(r"^[A-Za-z0-9_\-]+$", name):
            raise QnapError("快照名只允许字母数字下划线横线")
        self.login()
        out = self._run([
            _LOCAL_QCLI + "_volumesnapshot", "-t",
            f"volumeID={volume_id}", f"snapshot_name={name}",
            f"vital={1 if vital else 0}",
        ])
        sid = parse_create_id(out)
        # 列出确认（QTS 偶发异步，列一次保证拿到完整记录）
        for s in self.list_snapshots(volume_id):
            if s.snapshot_id == sid:
                return s
        return QnapSnapshot(
            snapshot_id=sid, name=name, vital=vital, status="", snap_type="",
        )

    def delete_snapshot(self, snapshot_id: str) -> None:
        self.login()
        out = self._run([
            _LOCAL_QCLI + "_volumesnapshot", "-d", f"snapshotID={snapshot_id}",
        ])
        parse_delete_ok(out)
        # 注意：QTS 的删除是异步后台回收，命令返回 ok 后快照会短暂处于
        # "Removing..." 状态，数秒到数十秒后才彻底消失。调用方需轮询确认。


# ---------------------------------------------------------------------------
# 高层封装（storage 统一入口会调用）
# ---------------------------------------------------------------------------

def default_client() -> QnapClient:
    """根据环境变量构造客户端。

    NASSAFE_QNAP_HOST / NASSAFE_HOST 指向远程 -> SSH 模式；
    否则本地模式（装在 QTS 主机上）。
    """
    host = os.environ.get("NASSAFE_QNAP_HOST") or os.environ.get("NASSAFE_HOST")
    user = os.environ.get("NASSAFE_QNAP_USER")
    password = os.environ.get("NASSAFE_QNAP_PASS") or os.environ.get("NASSAFE_PASS")
    if host and host not in ("127.0.0.1", "localhost", ""):
        return QnapClient(host=host, user=user, password=password)
    return QnapClient(host=None, user=user, password=password)


def list_volumes(client: Optional[QnapClient] = None) -> list[QnapVolume]:
    client = client or default_client()
    try:
        return client.list_volumes()
    finally:
        client.close()


def list_snapshots(volume_id: str, client: Optional[QnapClient] = None) -> list[QnapSnapshot]:
    client = client or default_client()
    try:
        return client.list_snapshots(volume_id)
    finally:
        client.close()


def create_snapshot(
    volume_id: str, name: str, vital: bool = True,
    client: Optional[QnapClient] = None,
) -> QnapSnapshot:
    client = client or default_client()
    try:
        return client.create_snapshot(volume_id, name, vital=vital)
    finally:
        client.close()


def delete_snapshot(snapshot_id: str, client: Optional[QnapClient] = None) -> None:
    client = client or default_client()
    try:
        client.delete_snapshot(snapshot_id)
    finally:
        client.close()
