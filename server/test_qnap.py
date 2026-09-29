"""
NAS Safe — 威联通 QNAP 适配层测试

  A. 纯单元测试（无需网络/真实 NAS）：
     1. qcli_volume -l 输出解析
     2. qcli_volumesnapshot -l 输出解析（含日期含空格、vital 字段）
     3. 创建/删除命令构造与返回值解析
     4. 错误输出正确抛 QnapError
     5. 用 FakeClient 验证 create 默认 vital=1、delete 用 snapshotID（无需 volumeID）

  B. 真机集成测试（仅当设置 NASSAFE_HOST + NASSAFE_PASS 时运行）：
     在 volumeID=1 创建 vital=1 快照 -> 列出确认 -> 删除 -> 确认清除。
     复用 2026-09-29 真机验证流程，自动化且自我清理。
"""

import os
import re
import sys
import time
import tempfile
import shutil as shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qnap  # noqa: E402
from qnap import (  # noqa: E402
    QnapClient, QnapError, parse_volumes, parse_snapshots,
    parse_create_id, parse_delete_ok,
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name} {detail}")


# 取自真实 QNAP TS-873A / QTS 5.2.9 的输出样本
VOLUME_LIST_TEXT = """
Volume Count
2
volumeID Type Encrypt Alias        Staticvolume FSRVP
2        Data --      我的文件 no           no
1        Data unlock  系统盘    no           no
"""

SNAP_LIST_TEXT = """
Snapshot Count
1
snapshotID create_time              snapshot_name                vital snapshot_type    status
10001      Tue Sep 29 18:17:59 2026 nassafe_test_20260929_181756 1     Crash consistent Ready
"""


def test_parse_volumes():
    print("\n【A1】qcli_volume -l 解析")
    vols = parse_volumes(VOLUME_LIST_TEXT)
    check("解析出 2 个卷", len(vols) == 2, f"实际 {len(vols)}")
    by_id = {v.volume_id: v for v in vols}
    check("volumeID=2 别名=我的文件", by_id.get("2") is not None and by_id["2"].alias == "我的文件")
    check("volumeID=1 别名=系统盘", by_id.get("1") is not None and by_id["1"].alias == "系统盘")
    check("volumeID=2 类型=Data", by_id.get("2") is not None and by_id["2"].volume_type == "Data")


def test_parse_snapshots():
    print("\n【A2】qcli_volumesnapshot -l 解析")
    snaps = parse_snapshots(SNAP_LIST_TEXT)
    check("解析出 1 个快照", len(snaps) == 1, f"实际 {len(snaps)}")
    s = snaps[0]
    check("snapshot_id=10001", s.snapshot_id == "10001")
    check("name 含 nassafe_test", s.name.startswith("nassafe_test"))
    check("vital=1 解析为 True", s.vital is True)
    check("status=consistent Ready（含空格）", s.status == "consistent Ready")
    check("created_at 保留完整日期", s.created_at.startswith("Tue Sep 29"))


def test_create_delete_parsing():
    print("\n【A3】创建/删除返回解析")
    sid = parse_create_id("create snapshot 10001 ok!")
    check("解析创建 ID=10001", sid == "10001")
    ok = parse_delete_ok("delete snapshot 10001 ok!")
    check("删除 ok 解析为真", ok is True)


def test_error_parsing():
    print("\n【A4】错误输出抛 QnapError")
    for bad in ["volumeID is not defined, please check command again!!",
                "Error: snapshot not found",
                "fail to create"]:
        try:
            parse_create_id(bad)
            check(f"对错误抛异常: {bad[:20]}", False, "竟然没抛")
        except QnapError:
            check(f"对错误抛异常: {bad[:20]}", True)


class FakeQnapClient(QnapClient):
    """记录调用并回放脚本化输出的测试替身。"""

    def __init__(self):
        super().__init__(host=None)
        self.calls = []
        self._created = {}
        self._counter = 10000

    def _kv(self, args):
        out = {}
        for a in args:
            if "=" in a:
                k, _, v = a.partition("=")
                out[k] = v
        return out

    def _run(self, args):
        self.calls.append(list(args))
        joined = " ".join(args)
        if "user=" in joined:           # 登录，忽略
            return ""
        if "-t" in args:                # 创建
            kv = self._kv(args)
            self._counter += 1
            sid = str(self._counter)
            self._created.setdefault(kv["volumeID"], []).append(
                (sid, kv["snapshot_name"], kv["vital"])
            )
            return f"create snapshot {sid} ok!"
        if "-d" in args:                # 删除
            kv = self._kv(args)
            return f"delete snapshot {kv['snapshotID']} ok!"
        if "volumeID=" in joined:       # 快照列表
            kv = self._kv(args)
            vid = kv["volumeID"]
            rows = ["Snapshot Count", str(len(self._created.get(vid, []))),
                    "snapshotID create_time snapshot_name vital snapshot_type status"]
            for (sid, name, vital) in self._created.get(vid, []):
                rows.append(f"{sid}  Tue Sep 29 18:17:59 2026  {name}  {vital}  Crash consistent Ready")
            return "\n".join(rows)
        if "qcli_volume" in args and "-l" in args:   # 卷列表
            return VOLUME_LIST_TEXT
        return ""


def test_fake_client_flow():
    print("\n【A5】FakeClient 验证 create/delete 命令与 vital 默认值")
    c = FakeQnapClient()

    # 创建（默认 vital=True）
    s = qnap.create_snapshot("1", "test_snap", client=c)
    check("创建返回 snapshot_id", s.snapshot_id == "10001")
    check("创建默认 vital=True", s.vital is True)

    # 校验 create 命令里带了 vital=1
    create_call = [a for a in c.calls if "-t" in a]
    check("存在 -t 创建命令", bool(create_call))
    if create_call:
        check("-t 命令含 vital=1", any(x == "vital=1" for x in create_call[0]))
        check("-t 命令含 volumeID=1", any(x == "volumeID=1" for x in create_call[0]))

    # 删除（用 snapshotID，无需 volumeID）
    qnap.delete_snapshot("10001", client=c)
    del_call = [a for a in c.calls if "-d" in a]
    check("存在 -d 删除命令", bool(del_call))
    if del_call:
        check("-d 命令含 snapshotID=10001", any(x == "snapshotID=10001" for x in del_call[0]))
        check("-d 命令不含 volumeID", not any(x.startswith("volumeID=") for x in del_call[0]))

    # vital=False 时应为 vital=0
    c2 = FakeQnapClient()
    qnap.create_snapshot("2", "x", vital=False, client=c2)
    tcall = [a for a in c2.calls if "-t" in a][0]
    check("vital=False -> vital=0", any(x == "vital=0" for x in tcall))


def test_local_mode_no_qcli():
    print("\n【A6】本地模式缺少 qcli 时抛 QnapError")
    # 默认客户端为本地模式；非 QNAP 环境无 qcli，应快速失败
    if not qnap.default_client().mode == "local":
        check("本地模式判断", True, "（跳过：当前在远程模式）")
        return
    try:
        qnap.create_snapshot("1", "x")
        check("无 qcli 抛异常", False, "竟然成功")
    except QnapError:
        check("无 qcli 抛 QnapError", True)


def test_parse_ls_entry():
    print("\n【A7】ls -la 行解析（快照浏览）")
    d = qnap._parse_ls_entry(
        "drwxr-xr-x 28 admin administrators 4096 Sep 29 18:30 CACHEDEV2_DATA")
    check("目录行 is_dir=True", d is not None and d["is_dir"] is True)
    check("目录行 size=4096", d is not None and d["size"] == 4096)
    f = qnap._parse_ls_entry(
        "-rw-r--r--  1 admin administrators  123 Sep 29 18:30 note.txt")
    check("文件行 is_dir=False", f is not None and f["is_dir"] is False)
    check("文件行 size=123", f is not None and f["size"] == 123)
    check("文件行 name=note.txt", f is not None and f["name"] == "note.txt")
    dot = qnap._parse_ls_entry(
        "drwxr-xr-x 35 admin administrators 1120 Sep 28 16:28 .")
    check("跳过 . 条目", dot is None)
    sp = qnap._parse_ls_entry(
        "-rw-r--r--  1 admin administrators  200 Sep 29 18:30 My Movie.mp4")
    check("文件名含空格解析正确", sp is not None and sp["name"] == "My Movie.mp4")


def test_local_browse_and_restore():
    print("\n【A8】本地模式 list_dir / read_file / restore_file")
    if qnap.default_client().mode != "local":
        check("本地模式判断", True, "（跳过：当前为远程模式）")
        return
    tmp = tempfile.mkdtemp()
    try:
        root = os.path.join(tmp, "vol1", "10001")
        os.makedirs(os.path.join(root, "subdir"))
        with open(os.path.join(root, "hello.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello world")
        old = qnap.SNAP_MOUNT_ROOT
        qnap.SNAP_MOUNT_ROOT = tmp
        try:
            c = QnapClient(host=None)
            entries = c.list_dir("vol1", "10001", "")
            names = {e["name"] for e in entries}
            check("列出含 hello.txt", "hello.txt" in names)
            check("列出含 subdir 且 is_dir",
                  any(e["name"] == "subdir" and e["is_dir"] for e in entries))
            data = c.read_file("vol1", "10001", "hello.txt", max_bytes=None)
            check("read_file 内容正确", data == b"hello world")
            dest = tempfile.mkdtemp()
            restored = c.restore_file("vol1", "10001", "hello.txt", dest)
            check("restore 文件存在", os.path.exists(restored))
            with open(restored, encoding="utf-8") as fh:
                check("restore 内容正确", fh.read() == "hello world")
        finally:
            qnap.SNAP_MOUNT_ROOT = old
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# B. 真机集成测试（需 NASSAFE_HOST + NASSAFE_PASS）
# ---------------------------------------------------------------------------

def test_live_integration():
    print("\n【B】真机集成测试（NAS 192.168.8.62）：创建->浏览->读取->取回->删除")
    host = os.environ.get("NASSAFE_HOST")
    password = os.environ.get("NASSAFE_PASS")
    if not (host and password):
        print("  [跳过] 未设置 NASSAFE_HOST / NASSAFE_PASS，跳过真机测试")
        return

    user = os.environ.get("NASSAFE_QNAP_USER") or os.environ.get("NASSAFE_USER") or "admin"
    client = QnapClient(host=host, user=user, password=password)
    created = []   # [(vid, sid, name)]

    try:
        vols = client.list_volumes()
        check("真机列出卷成功", len(vols) >= 1, f"得到 {len(vols)} 个")

        # 用数据盘(volumeID=2) 做浏览验证（含真实文件）
        vid = "2"
        name = "nassafe_ci_%s" % time.strftime("%Y%m%d%H%M%S")

        # 创建并锁定
        s = client.create_snapshot(vid, name, vital=True)
        check("真机创建快照成功", bool(s.snapshot_id))
        check("真机快照 vital=True（已锁定）", s.vital is True)
        created.append((vid, s.snapshot_id, name))

        snaps = client.list_snapshots(vid)
        check("真机列出包含新快照", any(x.name == name for x in snaps))

        # 浏览：列出顶层目录
        top = client.list_dir(vid, s.snapshot_id, "")
        check("真机浏览顶层非空", len(top) > 0)
        check("真机顶层含目录", any(e["is_dir"] for e in top))

        # 深度优先找一个含真实文件的路径（优先浅层），用于读取/取回验证。
        # 数据盘目录树极深，全局 BFS 会在跨顶层目录平铺展开时耗尽列举预算
        # 而仍未触及文件；改为 DFS：逐个顶层目录一路钻到底，命中第一个文件
        # 即返回，调用次数极少。再给全局 200 次列举 / 深度 8 的上限兜底。
        found = None   # (rel_path, size)
        top_dirs = [d["name"] for d in top if d["is_dir"]]
        calls = 0
        MAX_CALLS = 200
        MAX_DEPTH = 8

        def dfs(rel: str, depth: int) -> None:
            nonlocal calls, found
            if found or calls >= MAX_CALLS or depth > MAX_DEPTH:
                return
            try:
                sub = client.list_dir(vid, s.snapshot_id, rel)
                calls += 1
            except QnapError:
                return
            for e in sub:
                entry_rel = e["name"] if not rel else rel + "/" + e["name"]
                if e["is_dir"]:
                    if depth < MAX_DEPTH:
                        dfs(entry_rel, depth + 1)
                else:
                    found = (entry_rel, e["size"] or 0)
                    return
                if found:
                    return

        for d in top_dirs:
            dfs(d, 1)
            if found:
                break
        check("真机递归浏览找到可读文件", found is not None,
              f"（快照根下未找到非目录条目，已列举 {calls} 次）")
        if found:
            rel, fsize = found
            data = client.read_file(vid, s.snapshot_id, rel, max_bytes=200)
            check("真机读取文件(字节非空)", isinstance(data, bytes) and len(data) > 0)
            # 仅对小文件取回到沙箱临时目录，避免拉取超大视频占带宽
            if fsize <= 5 * 1024 * 1024:
                dest = tempfile.mkdtemp()
                restored = client.restore_file(vid, s.snapshot_id, rel, dest)
                check("真机取回文件成功(大小>0)",
                      os.path.exists(restored) and os.path.getsize(restored) > 0)
            else:
                check("真机取回跳过(大文件)", True, "（仅验证读取）")
    except QnapError as e:
        check("真机集成流程", False, str(e))
    finally:
        # 清理：删除所有创建过的快照并轮询确认清除
        for (vid, sid, name) in created:
            try:
                client.delete_snapshot(sid)
            except QnapError:
                pass
        for (vid, sid, name) in created:
            removed = False
            for _ in range(20):     # 最多约 100s（QTS 异步回收可能较慢）
                try:
                    after = client.list_snapshots(vid)
                except QnapError:
                    after = []
                if not any(x.name == name for x in after) and \
                   not any(x.snapshot_id == sid for x in after):
                    removed = True
                    break
                time.sleep(5)
            check(f"真机删除后清除 {sid}", removed)
        client.close()


if __name__ == "__main__":
    print("=" * 58)
    print("  NAS Safe — QNAP 适配层测试")
    print("=" * 58)

    test_parse_volumes()
    test_parse_snapshots()
    test_create_delete_parsing()
    test_error_parsing()
    test_fake_client_flow()
    test_local_mode_no_qcli()
    test_parse_ls_entry()
    test_local_browse_and_restore()
    test_live_integration()

    print("\n" + "=" * 58)
    print(f"  通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 58)
    sys.exit(1 if FAIL else 0)
