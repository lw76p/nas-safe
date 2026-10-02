"""Windows 卷影复制 (VSS) 快照后端（骨架 / 待接入）。

统一接口，与 storage.py 里的 btrfs / zfs / qnap / aliyun 后端保持一致，
由 storage 的分派层（create_snapshot / list_all_snapshots / ...）调用。

接入路线（后续版本）：
  - 枚举保护目标：固定盘 C:/ D:/ ... 作为可保护卷
  - 创建快照：vssadmin create shadow /for=<drive>  或 diskshadow 脚本
  - 浏览：把影子盘符号链接暴露为只读挂载点，复用 _browse_local_dir
  - 取回：robocopy 从影子盘拷贝单文件到目标
  - 删除：vssadmin delete shadows /shadow=<id>
当前一律返回清晰的「待接入」错误，不抛裸异常、不破坏主流程。
"""
from __future__ import annotations

from storage import StorageError


def list_volumes() -> list:
    raise StorageError(
        "Windows VSS 快照后端待接入：将用 vssadmin / diskshadow 枚举固定盘并创建影子副本"
    )


def list_snapshots(volume) -> list:
    raise StorageError("Windows VSS 快照后端待接入")


def create_snapshot(volume, name: str, vital: bool = True):
    raise StorageError(
        "Windows VSS 快照后端待接入：将用 vssadmin create shadow /for=<盘符> 实现"
    )


def delete_snapshot(snapshot) -> None:
    raise StorageError("Windows VSS 快照后端待接入")


def browse_snapshot(snapshot, subpath: str = "") -> dict:
    raise StorageError("Windows VSS 快照后端待接入")


def restore_from_snapshot(snapshot, rel_path: str, dest: str) -> dict:
    raise StorageError("Windows VSS 快照后端待接入")
