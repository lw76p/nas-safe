"""macOS APFS 快照后端（骨架 / 待接入）。

统一接口，与 storage.py 里的 btrfs / zfs / qnap / aliyun 后端保持一致，
由 storage 的分派层调用。

接入路线（后续版本）：
  - 枚举保护目标：本地 APFS 宗卷（/ /System/Volumes/Data 等）
  - 创建快照：tmutil localsnapshot  或  apfs 工具（需提权）
  - 浏览：Time Machine 本地快照挂载点 / 按日期目录浏览
  - 取回：从快照目录 cp 单文件
  - 删除：tmutil deletelocalsnapshots
当前一律返回清晰的「待接入」错误，不抛裸异常、不破坏主流程。
"""
from __future__ import annotations

from storage import StorageError


def list_volumes() -> list:
    raise StorageError(
        "macOS APFS 快照后端待接入：将用 tmutil localsnapshot / apfs 工具实现"
    )


def list_snapshots(volume) -> list:
    raise StorageError("macOS APFS 快照后端待接入")


def create_snapshot(volume, name: str, vital: bool = True):
    raise StorageError(
        "macOS APFS 快照后端待接入：将用 tmutil localsnapshot 创建本地快照"
    )


def delete_snapshot(snapshot) -> None:
    raise StorageError("macOS APFS 快照后端待接入")


def browse_snapshot(snapshot, subpath: str = "") -> dict:
    raise StorageError("macOS APFS 快照后端待接入")


def restore_from_snapshot(snapshot, rel_path: str, dest: str) -> dict:
    raise StorageError("macOS APFS 快照后端待接入")
