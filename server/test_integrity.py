#!/usr/bin/env python3
"""v2 内容完整性校验单元测试（不依赖真实 NAS）。

覆盖：
  - build_manifest / build_manifest_for_snapshot 基本行为
  - compare_manifest 的「相同 / 内容变化」判定
  - register_protected 自动附带完整性基线（及无可本地路径时优雅跳过）
  - scan_integrity（经 scan_tamper(include_integrity=True)）检测出内容被改
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage
import integrity


PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[通过] {name}")
    else:
        FAIL += 1
        print(f"[失败] {name} {extra}")


def _make_tree(root, files):
    for rel, content in files.items():
        p = os.path.join(root, rel)
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)


def make_snap(name="snap-x", volume="2", fs_type="btrfs", sid="100",
             vital=True, readonly=True, path=None):
    return storage.Snapshot(
        name=name, volume=volume, fs_type=fs_type,
        snapshot_id=sid, vital=vital, readonly=readonly, path=path,
    )


def fresh_state():
    d = tempfile.mkdtemp(prefix="nassafe_integrity_")
    os.environ["NASSAFE_STATE_DIR"] = d
    p = os.path.join(d, "protected.json")
    if os.path.exists(p):
        os.remove(p)
    return d


def test_build_manifest_nonexistent():
    assert integrity.build_manifest("/no/such/dir") is None
    check("不存在目录返回 None", True)


def test_build_manifest_basic():
    d = tempfile.mkdtemp(prefix="nas_int_")
    try:
        _make_tree(d, {"a.txt": b"hello", "sub/b.bin": b"world" * 10})
        m = integrity.build_manifest(d)
        check("文件数统计正确", m["file_count"] == 2, str(m))
        check("总字节统计正确", m["total_bytes"] == len(b"hello") + len(b"world" * 10))
        check("签名是 64 位 hex", len(m["signature"]) == 64)
        check("抽样非空", m["sampled_count"] > 0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_compare_identical_and_changed():
    d = tempfile.mkdtemp(prefix="nas_int_")
    try:
        vs = {"a.txt": b"hello", "sub/b.bin": b"world" * 10}
        _make_tree(d, vs)
        m1 = integrity.build_manifest(d)
        m2 = integrity.build_manifest(d)
        cmp_same = integrity.compare_manifest(m1, m2)
        check("相同清单判定未变化", cmp_same["changed"] is False, str(cmp_same))
        # 仅改内容、大小不变 → 签名仍变（按路径+大小+mtime，mtime 会变），且抽样头部哈希变化
        _make_tree(d, {"a.txt": b"HELLO", "sub/b.bin": b"world" * 10})
        m3 = integrity.build_manifest(d)
        cmp_chg = integrity.compare_manifest(m1, m3)
        check("内容/时间戳变化判定为变化", cmp_chg["changed"] is True, str(cmp_chg))
        check("抽样内容变化计数 >=1", cmp_chg["content_changed_count"] >= 1, str(cmp_chg))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_manifest_for_snapshot():
    d = tempfile.mkdtemp(prefix="nas_int_")
    try:
        _make_tree(d, {"x.txt": b"abc"})
        snap = make_snap(path=d)
        m = integrity.build_manifest_for_snapshot(snap)
        check("带本地路径的快照可生成清单", m is not None and m["file_count"] == 1)
        snap_no_path = make_snap()  # 无 path / mount_path
        check("无本地路径返回 None", integrity.build_manifest_for_snapshot(snap_no_path) is None)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_register_attaches_baseline():
    d = fresh_state()
    snap = make_snap(path=d)
    storage.register_protected(snap)
    entries = storage.load_protected().get("entries", [])
    check("register 后基线存在", len(entries) == 1)
    check("基线附带 integrity 清单", entries[0].get("integrity") is not None, str(entries))
    shutil.rmtree(d)


def test_register_no_local_path_skips_baseline():
    d = fresh_state()
    snap = make_snap()  # 无 path
    storage.register_protected(snap)
    entries = storage.load_protected().get("entries", [])
    check("无本地路径时 integrity 为 None（优雅跳过）",
          entries and entries[0].get("integrity") is None)
    shutil.rmtree(d)


def test_scan_integrity_detects_change():
    d = fresh_state()
    _make_tree(d, {"keep.txt": b"orig-data", "keep2.txt": b"more-data"})
    snap = make_snap(path=d)
    storage.register_protected(snap)  # 此时登记基线
    storage.list_all_volumes = lambda: [storage.Volume(
        name="vol2", mountpoint="2", fs_type="btrfs", volume_id="2")]
    storage.list_all_snapshots = lambda vol: [snap]
    # 默认巡检不并入完整性（较重），应无 integrity_changed
    alerts_default = storage.scan_tamper(include_integrity=False)
    check("默认巡检不含完整性告警", not any(
        a["type"] == "integrity_changed" for a in alerts_default))
    # 改动内容后再做深度校验
    _make_tree(d, {"keep.txt": b"TAMPERED!!", "keep2.txt": b"more-data"})
    alerts_deep = storage.scan_tamper(include_integrity=True)
    chg = [a for a in alerts_deep if a["type"] == "integrity_changed"]
    check("深度校验发现内容被篡改", len(chg) == 1, str(alerts_deep))
    shutil.rmtree(d)


if __name__ == "__main__":
    test_build_manifest_nonexistent()
    test_build_manifest_basic()
    test_compare_identical_and_changed()
    test_build_manifest_for_snapshot()
    test_register_attaches_baseline()
    test_register_no_local_path_skips_baseline()
    test_scan_integrity_detects_change()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)
