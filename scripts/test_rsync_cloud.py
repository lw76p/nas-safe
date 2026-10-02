"""在阿里云（真实 Linux）上端到端验证 rsync/硬链接快照后端。

流程：SFTP 上传 server 文件 -> 重启服务 -> 远程跑一段 e2e 测试
（枚举保护目标 / 建快照 / 篡改 / 再建 / 浏览 / 取回还原 / 纯 Python 兜底 / 删除）。
"""
from __future__ import annotations

import os
import paramiko

HOST = "47.108.213.178"
USER = "root"
SECRET = r"C:\Users\aa\.workbuddy\cloud-secret.txt"
LOCAL = r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nas-safe-clone\server"
REMOTE_DIR = "/opt/nas-safe/server"
FILES = ["storage.py", "snapshot_rsync.py", "brands.py"]

REMOTE_TEST = r'''
import os, sys, shutil, tempfile
os.chdir("/opt/nas-safe")
sys.path.insert(0, "/opt/nas-safe/server")
import storage
from storage import Volume
import snapshot_rsync as rs

ok = lambda m: print("  PASS:", m)

print("=== A. 真实枚举保护目标 (Linux) ===")
vols = storage.list_all_volumes()
for v in vols:
    print("   -", v.name, "| fs:", v.fs_type, "| src:", v.mountpoint)
n_rsync = len([v for v in vols if v.fs_type == "rsync"])
print("  rsync 卷数量:", n_rsync)
assert n_rsync > 0, "Linux 上应枚举到 rsync 保护目标"

print("=== B. 端到端 CRUD ===")
src = tempfile.mkdtemp(prefix="e2e_src_")
open(os.path.join(src, "hello.txt"), "w").write("v1")
os.makedirs(os.path.join(src, "sub"))
open(os.path.join(src, "sub", "deep.txt"), "w").write("deep-v1")

vol = Volume(name="E2E测试目录", mountpoint=src, fs_type="rsync",
             snapshot_dir=os.path.join(rs._state_root(), "e2e_test"), backend="fs")
shutil.rmtree(vol.snapshot_dir, ignore_errors=True)   # 清掉上一轮残留

s1 = storage.create_snapshot(vol, "第一次", vital=True)
m1 = rs._read_meta(s1.path)
print("  s1 engine=%s copied=%s linked=%s" % (m1.get("engine"), m1.get("copied"), m1.get("linked")))
assert m1.get("copied") == 2, "首份应复制 2 个文件"
ok("首份快照（全量复制）")

open(os.path.join(src, "hello.txt"), "w").write("v2-被加密")
s2 = storage.create_snapshot(vol, "第二次", vital=True)
m2 = rs._read_meta(s2.path)
print("  s2 engine=%s copied=%s linked=%s" % (m2.get("engine"), m2.get("copied"), m2.get("linked")))
ok("篡改后建第二份快照")

snaps = storage.list_all_snapshots(vol)
print("  快照列表:", [x.name for x in snaps])
assert len(snaps) == 2, "应有 2 份快照"
ok("快照列举")

b = storage.browse_snapshot(snaps[0], "")
print("  浏览根:", [e["name"] for e in b["entries"]])
assert any(e["name"] == "hello.txt" for e in b["entries"])
ok("快照浏览")

b2 = storage.browse_snapshot(snaps[0], "sub")
print("  浏览 sub:", [e["name"] for e in b2["entries"]])
ok("子目录浏览")

oldest = snaps[-1]
dest = tempfile.mkdtemp(prefix="e2e_dest_")
r = storage.restore_from_snapshot(oldest, "hello.txt", dest)
got = open(r["restored_to"]).read()
print("  从「%s」取回内容 = %r" % (oldest.name, got))
assert got == "v1", "应从篡改前的快照取回 v1，实际 %r" % got
ok("防勒索还原：取回的是篡改前的内容")

print("=== C. 纯 Python 兜底（禁用 rsync 二进制）===")
rs._which = lambda c: False
s3 = storage.create_snapshot(vol, "纯Python兜底", vital=True)
m3 = rs._read_meta(s3.path)
print("  s3 engine=%s copied=%s linked=%s" % (m3.get("engine"), m3.get("copied"), m3.get("linked")))
assert m3.get("engine") == "hardlink", "应走纯 Python 硬链接兜底"
assert m3.get("linked") >= 1, "未变更文件应被硬链接复用"
ok("无 rsync 时纯 Python 硬链接兜底可用")

print("=== D. 删除 ===")
for s in storage.list_all_snapshots(vol):
    storage.delete_snapshot(s)
left = len(storage.list_all_snapshots(vol))
print("  删除后剩余:", left)
assert left == 0, "快照应被全部删除"
ok("快照删除")

for d in (src, dest):
    shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(os.path.dirname(vol.snapshot_dir) + "/e2e_test", ignore_errors=True)
print("\nALL CLOUD E2E TESTS PASSED")
'''


def main() -> None:
    pwd = open(SECRET, encoding="utf-8").read().strip()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=pwd, timeout=30)
    try:
        sftp = ssh.open_sftp()
        for f in FILES:
            sftp.put(os.path.join(LOCAL, f), f"{REMOTE_DIR}/{f}")
            print("put", f)
        sftp.close()

        # 写入远程测试脚本
        sftp = ssh.open_sftp()
        with sftp.open("/tmp/rsync_e2e_remote.py", "w") as fh:
            fh.write(REMOTE_TEST)
        sftp.close()

        ssh.exec_command("systemctl restart nas-safe")
        _, out, _ = ssh.exec_command("sleep 2; systemctl is-active nas-safe")
        print("SERVICE:", out.read().decode().strip())

        _, out, err = ssh.exec_command("python3 /tmp/rsync_e2e_remote.py 2>&1")
        print("--- REMOTE E2E ---")
        print(out.read().decode("utf-8", "replace"))
        e = err.read().decode("utf-8", "replace").strip()
        if e:
            print("STDERR:", e)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
