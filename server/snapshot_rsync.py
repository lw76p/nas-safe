"""通用 Linux（无 btrfs/ZFS）rsync + 硬链接快照后端（骨架 / 待接入）。

统一接口，与 storage.py 里的 btrfs / zfs / qnap / aliyun 后端保持一致，
由 storage 的分派层调用。面向裸机 / 云服务器 ext4/xfs 等文件系统，
用「rsync + cp -al 硬链接」做 TimeMachine 式增量快照（仅占用变更块）。

接入路线（后续版本）：
  - 枚举保护目标：用户目录 / 数据盘（参考 brands.BRAND_SHARE_HINTS）
  - 创建快照：rsync -a --delete --link-dest=<上一份> <源> <快照库>/<时间戳>/
  - 浏览：直接读 <快照库>/<时间戳>/ 目录（复用 _browse_local_dir）
  - 取回：cp 单文件
  - 删除：rm -rf <快照库>/<时间戳>/
当前一律返回清晰的「待接入」错误，不抛裸异常、不破坏主流程。
"""
from __future__ import annotations

from storage import StorageError


def list_volumes() -> list:
    raise StorageError(
        "通用 Linux rsync 快照后端待接入：将用 rsync + cp -al 硬链接做增量快照"
    )


def list_snapshots(volume) -> list:
    raise StorageError("通用 Linux rsync 快照后端待接入")


def create_snapshot(volume, name: str, vital: bool = True):
    raise StorageError(
        "通用 Linux rsync 快照后端待接入：将用 rsync -a --link-dest 实现"
    )


def delete_snapshot(snapshot) -> None:
    raise StorageError("通用 Linux rsync 快照后端待接入")


def browse_snapshot(snapshot, subpath: str = "") -> dict:
    raise StorageError("通用 Linux rsync 快照后端待接入")


def restore_from_snapshot(snapshot, rel_path: str, dest: str) -> dict:
    raise StorageError("通用 Linux rsync 快照后端待接入")
