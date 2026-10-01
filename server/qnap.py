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
import posixpath
import re
import shlex
import subprocess
import threading
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


def _parse_ls_entry(line: str) -> Optional[dict]:
    """解析 `ls -la` 单行输出。

    标准 GNU ls 行示例：
      drwxr-xr-x 28 admin administrators 4096 Sep 29 18:30 CACHEDEV2_DATA
      -rw-r--r--  1 admin administrators  123 Sep 29 18:30 note.txt

    用 split(None, 8) 把前 8 段按空白切开，剩余（含空格的文件名）整体为第 9 段。
    第 5 段是字节大小（数字），最后一段是名称。
    """
    line = line.rstrip("\n")
    if not line:
        return None
    perms = line[:10]
    ftype = perms[0] if perms else "?"
    parts = line.split(None, 8)
    if len(parts) < 9:
        return None
    name = parts[8]
    if name in (".", ".."):
        return None
    is_dir = (ftype == "d")
    try:
        size = int(parts[4])
    except ValueError:
        size = None
    # 解析时间：半年内 ls 显示 "Sep 29 18:30"（当年），更早显示 "Sep 29  2024"
    mtime = None
    try:
        import datetime as _dt
        mon, day, tail = parts[5], parts[6], parts[7]
        if ":" in tail:                      # 当年：月 日 时:分
            now = _dt.datetime.now()
            dt = _dt.datetime.strptime(
                f"{now.year} {mon} {int(day):02d} {tail}", "%Y %b %d %H:%M")
            if dt > now:                     # 未来时间说明是去年的（12月底跨年边界）
                dt = dt.replace(year=now.year - 1)
            mtime = dt.strftime("%Y-%m-%d %H:%M")
        else:                                # 超半年：月 日 年
            dt = _dt.datetime.strptime(f"{mon} {int(day):02d} {tail}", "%b %d %Y")
            mtime = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        mtime = None
    return {"name": name, "is_dir": is_dir, "size": size,
            "is_symlink": ftype == "l", "mtime": mtime}


# ---------------------------------------------------------------------------
# 客户端（本地 / SSH 两种执行方式）
# ---------------------------------------------------------------------------

_LOCAL_QCLI = "qcli"

# QNAP 快照创建后由系统自动以只读方式挂载在此根目录下：
#   /mnt/snapshot/<卷ID>/<快照ID>/
# 这正是浏览快照文件树的入口（无需额外 -m 挂载命令）。
SNAP_MOUNT_ROOT = "/mnt/snapshot"


# ---------------------------------------------------------------------------
# 常驻 SSH 连接池
# ---------------------------------------------------------------------------
# 容器远程模式下，过去每个 API 请求各建一条 SSH 连接、用完即断：
#   - 每次握手+认证 0.5~2s，是「点目录浏览都要转圈」的卡顿根因；
#   - 前端 15s 指标轮询 + 30s 告警轮询 + 后台扫描线程叠加，形成对 QTS
#     sshd 的连接风暴，触发 MaxStartups 丢连接 -> [Errno 104] Connection
#     reset by peer（垃圾清理失败就是撞上了这个）。
# 现按 (host, user, password) 常驻复用一条连接（paramiko Transport 支持
# 多线程各开 channel），keepalive 30s；单条命令失败（被对端 reset/断开）
# 时自动重建连接并重试一次。

_POOL: dict = {}
_POOL_LOCK = threading.Lock()
_LOGGED_IN: set = set()


def _pool_get(key, new_cli):
    """从池里取共享连接；没有则放入 new_cli。若其他线程已抢先重建，返回已有的并让调用方关掉 new_cli。"""
    with _POOL_LOCK:
        cli = _POOL.get(key)
        if cli is None:
            _POOL[key] = new_cli
            return new_cli
        return cli


def _pool_drop(key, cli) -> None:
    """把故障连接移出池并真正关闭。"""
    with _POOL_LOCK:
        if _POOL.get(key) is cli:
            _POOL.pop(key, None)
    try:
        cli.close()
    except Exception:
        pass


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
        self._sftp_client = None
        self.mode = "local" if not host else "ssh"

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

    # -- 执行 ----------------------------------------------------------

    def _get_ssh(self):
        """取常驻 SSH 连接（优先复用连接池；池里没有才新建并开 keepalive）。"""
        import paramiko

        if self._ssh is not None:
            return self._ssh
        key = (self.host, self.user, self.password)
        fresh = paramiko.SSHClient()
        fresh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        fresh.connect(
            self.host, port=22, username=self.user, password=self.password,
            timeout=15, look_for_keys=False, allow_agent=False,
        )
        try:
            tr = fresh.get_transport()
            if tr is not None:
                tr.set_keepalive(30)
        except Exception:
            pass
        cli = _pool_get(key, fresh)
        if cli is not fresh:
            # 其他线程已抢先重建了连接，关掉多余的这条
            try:
                fresh.close()
            except Exception:
                pass
        self._ssh = cli
        return self._ssh

    def _exec_ssh(self, make_cmd) -> str:
        """经常驻连接执行命令并返回文本；连接被对端 reset/断开时重建并重试一次。"""
        import paramiko

        last_exc: Exception | None = None
        for _ in range(2):
            cli = self._get_ssh()
            try:
                _stdin, stdout, stderr = make_cmd(cli)
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")
                return out + err
            except (OSError, EOFError, paramiko.SSHException) as exc:
                last_exc = exc
                _pool_drop((self.host, self.user, self.password), cli)
                self._ssh = None
        raise QnapError(f"SSH 命令执行失败（已自动重试一次）: {last_exc}")

    def _run_ssh(self, args: list[str]) -> str:
        cmd = " ".join(shlex.quote(a) for a in args)
        return self._exec_ssh(lambda cli: cli.exec_command(cmd, timeout=self.timeout))

    def close(self) -> None:
        """解除本客户端对常驻连接的引用（不断开共享 SSH）。

        连接池中的 SSH 供所有请求复用；真正断开由 _pool_drop 在连接
        故障时处理。本地模式无 SSH 连接，无影响。"""
        self._ssh = None

    def run_shell(self, script: str) -> str:
        """执行一段原始 shell 脚本（只读探测，如系统指标采集）。

        SSH 模式经远端 shell；本地模式直接 /bin/sh -c。"""
        if self.host in (None, "", "127.0.0.1", "localhost"):
            import subprocess

            proc = subprocess.run(
                ["/bin/sh", "-c", script],
                capture_output=True, text=True, timeout=self.timeout,
            )
            return (proc.stdout or "") + (proc.stderr or "")
        import paramiko  # noqa: F401  懒加载依赖标记

        return self._exec_ssh(lambda cli: cli.exec_command(script, timeout=self.timeout))

    # -- 登录 ----------------------------------------------------------

    def login(self) -> None:
        """登录并保存会话。无凭据（user/password）时跳过，依赖已保存的 sid。

        saveauthsid 在设备侧持久会话，本进程登录一次即可；重复登录
        每次多耗一个 SSH 来回，是接口变慢的隐形开销之一。"""
        if not (self.user and self.password):
            return
        key = (self.host, self.user, self.password)
        if key in _LOGGED_IN:
            return
        self._run([
            _LOCAL_QCLI, "-l",
            f"user={self.user}", f"pw={self.password}", "saveauthsid=yes",
        ])
        _LOGGED_IN.add(key)

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

    def revert_snapshot(self, volume_id: str, snapshot_id: str) -> None:
        """整卷回滚到指定快照（破坏性操作）。

        仅限调用方已完成强确认（前端需手输「回滚」二字）后调用。
        QTS 执行期间卷会短暂不可用；回滚后该快照之后新增/修改的数据将丢失。
        """
        if not str(volume_id).strip() or not str(snapshot_id).strip():
            raise QnapError("整卷回滚缺少 volume_id / snapshot_id")
        self.login()
        self._run([
            _LOCAL_QCLI + "_volumesnapshot", "-r",
            f"volumeID={volume_id}", f"snapshotID={snapshot_id}",
        ])
        # 注意：QTS 的删除是异步后台回收，命令返回 ok 后快照会短暂处于
        # "Removing..." 状态，数秒到数十秒后才彻底消失。调用方需轮询确认。

    # -- 浏览 / 取回 ----------------------------------------------------
    # QNAP 快照创建后由系统自动只读挂载在 /mnt/snapshot/<卷>/<快照ID>/，
    # 无需额外的 -m 挂载命令即可浏览文件树。

    def snapshot_mount_path(self, volume_id: str, snapshot_id: str) -> str:
        return f"{SNAP_MOUNT_ROOT}/{volume_id}/{snapshot_id}"

    def list_dir(self, volume_id: str, snapshot_id: str, subpath: str = "") -> list[dict]:
        """列出快照内某子目录的内容。

        返回 entries 列表，每项 {name, is_dir, size}。本地模式直接 os.listdir，
        SSH 模式经 `ls -la` 解析（文件名可含空格）。
        """
        root = self.snapshot_mount_path(volume_id, snapshot_id)
        if self.mode == "local":
            return self._list_dir_local(root, subpath)
        return self._list_dir_ssh(root, subpath)

    def _list_dir_local(self, root: str, subpath: str) -> list[dict]:
        full = posixpath.normpath(posixpath.join(root, subpath)) if subpath else root
        entries: list[dict] = []
        for name in sorted(os.listdir(full)):
            if name.startswith("."):
                continue
            p = os.path.join(full, name)
            # 跳过符号链接：跟随 QTS 的 symlink 路径会乱码报错，
            # 且真实目标文件会作为独立条目列出，不丢内容。
            if os.path.islink(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            is_dir = os.path.isdir(p)
            entries.append({
                "name": name,
                "is_dir": is_dir,
                "size": None if is_dir else st.st_size,
                "is_symlink": False,
            })
        return entries

    def _list_dir_ssh(self, root: str, subpath: str) -> list[dict]:
        full = posixpath.normpath(posixpath.join(root, subpath)) if subpath else root
        out = self._run(["ls", "-la", full])
        entries: list[dict] = []
        for line in out.splitlines():
            parsed = _parse_ls_entry(line)
            if parsed is None:
                continue
            # 跳过隐藏/系统虚拟目录（如 .@wfm、.qpkg、.@msdfs_root），
            # 这些在 QTS 上并非真实可读路径，且对用户无意义。
            # 也跳过符号链接（跟随会乱码报错，真实目标会单独列出）。
            if parsed["name"].startswith(".") or parsed["is_symlink"]:
                continue
            entries.append(parsed)
        return entries

    def read_file(
        self,
        volume_id: str,
        snapshot_id: str,
        rel_path: str,
        max_bytes: Optional[int] = 10 * 1024 * 1024,
    ) -> bytes:
        """读取快照内单个文件的字节内容。

        max_bytes 限制读取上限，防止超大文件撑爆内存/带宽（默认 10MB）。
        SSH 模式经 `head -c` 读取原始字节（二进制安全）；之所以不用 SFTP，
        是因为 QTS 的 SFTP 子系统对中文/特殊文件名路径编码处理有坑，
        改用 shell 命令 + UTF-8 locale 更可靠。
        """
        if ".." in rel_path.split("/"):
            raise QnapError("非法相对路径")
        root = self.snapshot_mount_path(volume_id, snapshot_id)
        full = posixpath.normpath(posixpath.join(root, rel_path)) if rel_path else root
        if self.mode == "local":
            with open(full, "rb") as fh:
                return fh.read(max_bytes) if max_bytes else fh.read()
        out, err = self._run_ssh_raw([
            "head", "-c", str(max_bytes if max_bytes else 10 * 1024 * 1024), full,
        ])
        if not out and err.strip():
            raise QnapError(f"读取文件失败: {err.decode(errors='replace').strip()}")
        return out

    def restore_file(
        self,
        volume_id: str,
        snapshot_id: str,
        rel_path: str,
        dest: str,
    ) -> str:
        """将快照内单个文件取回到服务器本地 dest（dest 为文件或目录路径）。

        绝不覆盖已存在文件：若目标已存在，自动加 .restored-<时间戳> 后缀。
        SSH 模式经 `cat` 流式写入，二进制安全。
        """
        if ".." in rel_path.split("/"):
            raise QnapError("非法相对路径")
        root = self.snapshot_mount_path(volume_id, snapshot_id)
        full = posixpath.normpath(posixpath.join(root, rel_path))
        # destination 一律视为「恢复目录」，文件落到 dest/<原文件名>。
        # 注意：dest/root/full 都是远程（或宿主本地 Linux）路径，一律用
        # posixpath 处理，避免开发机为 Windows 时 os.path 把 / 翻成 \\。
        dest_path = posixpath.join(dest, posixpath.basename(full))
        # 预建目标目录：小白取回时目标目录多半还不存在，必须自动创建。
        # destination 始终是「运行 server 的这台机器」的本地路径 —— 本地
        # 模式是 NAS 宿主自身；SSH 模式是把 NAS 文件取回到管理机，目录在本地预建。
        os.makedirs(dest, exist_ok=True)
        # 是否已存在：destination 永远是 server 本机路径，统一用本地判断即可
        # （切勿在远端 test -e，远端没有这个路径会恒判不存在而静默覆盖）。
        if os.path.exists(dest_path):
            import time as _time
            base, ext = os.path.splitext(dest_path)
            dest_path = f"{base}.restored-{int(_time.time())}{ext}"
        if self.mode == "local":
            import shutil as _shutil
            _shutil.copy2(full, dest_path)
        else:
            cli = self._get_ssh()
            _stdin, stdout_i, stderr_i = cli.exec_command(
                "cat " + shlex.quote(full), timeout=self.timeout)
            with open(dest_path, "wb") as fh:
                while True:
                    chunk = stdout_i.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
            err = stderr_i.read().decode(errors="replace").strip()
            if err:
                raise QnapError(f"取回文件失败: {err}")
        return dest_path

    def _run_ssh_raw(self, args: list[str]) -> tuple[bytes, bytes]:
        """经 SSH 执行命令并返回原始字节（用于读取文件内容，二进制安全，复用连接池+失败重试）。"""
        import paramiko

        cmd = " ".join(shlex.quote(a) for a in args)
        last_exc: Exception | None = None
        for _ in range(2):
            cli = self._get_ssh()
            try:
                _stdin, stdout, stderr = cli.exec_command(cmd, timeout=self.timeout)
                return stdout.read(), stderr.read()
            except (OSError, EOFError, paramiko.SSHException) as exc:
                last_exc = exc
                _pool_drop((self.host, self.user, self.password), cli)
                self._ssh = None
        raise QnapError(f"SSH 读取失败（已自动重试一次）: {last_exc}")



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


def list_dir(
    volume_id: str, snapshot_id: str, subpath: str = "",
    client: Optional[QnapClient] = None,
) -> list[dict]:
    client = client or default_client()
    try:
        return client.list_dir(volume_id, snapshot_id, subpath)
    finally:
        client.close()


def read_file(
    volume_id: str, snapshot_id: str, rel_path: str,
    max_bytes: Optional[int] = 10 * 1024 * 1024,
    client: Optional[QnapClient] = None,
) -> bytes:
    client = client or default_client()
    try:
        return client.read_file(volume_id, snapshot_id, rel_path, max_bytes=max_bytes)
    finally:
        client.close()


def restore_file(
    volume_id: str, snapshot_id: str, rel_path: str, dest: str,
    client: Optional[QnapClient] = None,
) -> str:
    client = client or default_client()
    try:
        return client.restore_file(volume_id, snapshot_id, rel_path, dest)
    finally:
        client.close()
