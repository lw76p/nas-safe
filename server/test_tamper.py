#!/usr/bin/env python3
"""篡改检测（tamper detection）单元测试。

不依赖任何真实 NAS：通过 monkeypatch storage.list_all_volumes /
list_all_snapshots 注入模拟数据，验证「受保护快照基线」对比逻辑。
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import storage


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


def make_vol(fs_type="qnap", vid="2"):
    return storage.Volume(
        name=f"vol{vid}", mountpoint=vid, fs_type=fs_type, volume_id=vid
    )


def make_snap(name="snap-x", volume="2", fs_type="qnap", sid="100",
              vital=True, readonly=True):
    return storage.Snapshot(
        name=name, volume=volume, fs_type=fs_type,
        snapshot_id=sid, vital=vital, readonly=readonly,
    )


def fresh_state():
    d = tempfile.mkdtemp(prefix="nassafe_tamper_")
    os.environ["NASSAFE_STATE_DIR"] = d
    p = os.path.join(d, "protected.json")
    if os.path.exists(p):
        os.remove(p)
    return d


def test_normal_no_alert():
    d = fresh_state()
    snap = make_snap()
    storage.register_protected(snap)
    storage.list_all_volumes = lambda: [make_vol()]
    storage.list_all_snapshots = lambda vol: [snap]
    alerts = storage.scan_tamper()
    check("基线快照存在时无误报", alerts == [], str(alerts))
    shutil.rmtree(d)


def test_deleted_alert():
    d = fresh_state()
    snap = make_snap()
    storage.register_protected(snap)
    storage.list_all_volumes = lambda: [make_vol()]
    storage.list_all_snapshots = lambda vol: []  # 快照消失
    alerts = storage.scan_tamper()
    crit = [a for a in alerts if a["type"] == "deleted" and a["level"] == "critical"]
    check("快照消失触发 critical deleted 告警", len(crit) == 1, str(alerts))
    shutil.rmtree(d)


def test_unlocked_alert():
    d = fresh_state()
    snap = make_snap(vital=True)
    storage.register_protected(snap)
    current = make_snap(vital=False)  # 当前快照锁被解除
    storage.list_all_volumes = lambda: [make_vol()]
    storage.list_all_snapshots = lambda vol: [current]
    alerts = storage.scan_tamper()
    unl = [a for a in alerts if a["type"] == "unlocked"]
    check("锁被解除触发 warn unlocked 告警", len(unl) == 1, str(alerts))
    shutil.rmtree(d)


def test_writable_alert():
    d = fresh_state()
    snap = make_snap(vital=False, readonly=True)
    storage.register_protected(snap)
    current = make_snap(vital=False, readonly=False)  # 变可写
    storage.list_all_volumes = lambda: [make_vol()]
    storage.list_all_snapshots = lambda vol: [current]
    alerts = storage.scan_tamper()
    wr = [a for a in alerts if a["type"] == "writable"]
    check("变可写触发 warn writable 告警", len(wr) == 1, str(alerts))
    shutil.rmtree(d)


def test_unregister_no_alert():
    d = fresh_state()
    snap = make_snap()
    storage.register_protected(snap)
    storage.unregister_protected(storage.snapshot_key(snap))
    storage.list_all_volumes = lambda: [make_vol()]
    # 即便快照后来消失，因已主动取消保护，不应告警
    storage.list_all_snapshots = lambda vol: []
    alerts = storage.scan_tamper()
    check("主动 unregister 后不告警", alerts == [], str(alerts))
    shutil.rmtree(d)


def test_state_dir_unwritable_no_raise():
    # 把 NASSAFE_STATE_DIR 指向一个普通文件（非目录），
    # save_protected 应静默跳过，register / load 都不抛异常。
    f = tempfile.NamedTemporaryFile(delete=False, prefix="nassafe_block_")
    f.close()
    os.environ["NASSAFE_STATE_DIR"] = f.name
    try:
        snap = make_snap()
        storage.register_protected(snap)  # 不应抛异常
        check("状态目录不可写时 register 不抛异常", True)
        check("状态目录不可写时 load 返回空基线",
              storage.load_protected().get("entries") == [])
    finally:
        os.remove(f.name)


if __name__ == "__main__":
    test_normal_no_alert()
    test_deleted_alert()
    test_unlocked_alert()
    test_writable_alert()
    test_unregister_no_alert()
    test_state_dir_unwritable_no_raise()
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    sys.exit(1 if FAIL else 0)
