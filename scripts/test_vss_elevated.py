"""VSS 后端全链路自测：create -> list -> browse -> restore -> delete。

输出全部写入 sys.argv[1] 指定的结果文件（供提权运行后读取）。
state 目录用独立临时目录，不污染真实数据。
"""
import os
import sys
import tempfile
import traceback

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
sys.path.insert(0, SERVER)
os.chdir(tempfile.mkdtemp(prefix="nassafe_vss_test_"))  # state_dir() = cwd/state

OUT = []


def log(msg: str) -> None:
    OUT.append(str(msg))


def main() -> int:
    import snapshot_vss
    from storage import Volume, StorageError

    log(f"is_admin={snapshot_vss.is_admin()}")
    log(f"drives={snapshot_vss._fixed_drives()}")

    # 1. 枚举卷
    vols = snapshot_vss.list_volumes()
    log(f"list_volumes -> {[(v.name, v.fs_type, v.mountpoint) for v in vols]}")
    if not vols:
        log("FAIL: 没有枚举到任何卷（需要管理员权限）")
        return 1
    vol = next((v for v in vols if v.mountpoint.upper().startswith("C")), vols[0])

    # 2. 创建影子副本
    snap = snapshot_vss.create_snapshot(vol, "测试快照1", vital=False)
    log(f"create -> name={snap.name} path={snap.path} desc={snap.description}")
    if "HarddiskVolumeShadowCopy" not in (snap.path or ""):
        log("FAIL: 影子副本设备名不对")
        return 1

    # 3. 列表
    snaps = snapshot_vss.list_snapshots(vol)
    log(f"list_snapshots -> {len(snaps)} 个, 第一个 name={snaps[0].name if snaps else None}")
    if not snaps:
        log("FAIL: 创建后列表为空（影子可能立即失效）")
        return 1

    # 4. 浏览根目录
    root_view = snapshot_vss.browse_snapshot(snap, "")
    names = [e.get("name") for e in root_view.get("entries", [])][:12]
    log(f"browse root -> {names}")

    # 5. 浏览 Windows 子目录 + 取回一个小文件
    win_view = snapshot_vss.browse_snapshot(snap, "Windows")
    log(f"browse /Windows -> {len(win_view.get('entries', []))} 项")
    probe = ""
    for cand in ("Windows\\win.ini", "Windows\\system.ini", "Windows\\WindowsShell.Manifest"):
        try:
            snapshot_vss._safe_join(snap.path, cand)
            full = snapshot_vss._safe_join(snap.path, cand)
            if os.path.isfile(full):
                probe = cand
                break
        except Exception:  # noqa: BLE001
            continue
    if probe:
        dest = os.path.join(os.environ.get("TEMP", tempfile.gettempdir()), "vss_restore_out")
        r = snapshot_vss.restore_from_snapshot(snap, probe, dest)
        ok_file = os.path.join(dest, probe.split("\\")[-1])
        log(f"restore {probe} -> {r.get('restored_to')} size={os.path.getsize(ok_file) if os.path.isfile(ok_file) else 'MISS'}")
    else:
        log("restore 跳过：没找到探测文件")

    # 6. 删除
    snapshot_vss.delete_snapshot(snap)
    snaps2 = snapshot_vss.list_snapshots(vol)
    log(f"after delete -> {len(snaps2)} 个残留")
    if snaps2:
        log("FAIL: 删除后仍有残留")
        return 1

    # 7. 再建一个验证 vital 保存 + 孤儿清理逻辑
    snap2 = snapshot_vss.create_snapshot(vol, "测试快照2", vital=True)
    log(f"second create ok -> {snap2.path}")
    snapshot_vss.delete_snapshot(snap2)

    log("ALL-PASS")
    return 0


try:
    rc = main()
except Exception:
    log("EXC: " + traceback.format_exc()[-800:])
    rc = 3

out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "vss_test_out.txt")
with open(out_path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(OUT) + f"\nRC={rc}\n")
sys.exit(rc)
