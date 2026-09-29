"""清理残留测试快照：打印 -l 真实格式 + 删除快照 10001 + 卸载挂载 + 轮询确认。"""
import os
import time
import paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PASS, timeout=15,
              look_for_keys=False, allow_agent=False)

    def run(cmd, timeout=40):
        i, o, e = c.exec_command(cmd, timeout=timeout)
        return o.read().decode(errors="replace"), e.read().decode(errors="replace")

    run(f"qcli -l user={USER} pw='{PASS}' saveauthsid=yes")

    print("== 当前 volume1 快照原始输出 ==")
    o, _ = run("qcli_volumesnapshot -l volumeID=1")
    print(repr(o))

    # 卸载挂载点（fuse）
    print("\n== 卸载 /mnt/snapshot/1/10001 ==")
    run("fusermount -u /mnt/snapshot/1/10001 2>/dev/null")
    run("umount /mnt/snapshot/1/10001 2>/dev/null")
    run("qcli_volumesnapshot -m snapshotID=10001 2>/dev/null")  # 尝试切换卸载

    # 删除快照（用正确 ID 10001）
    print("\n== 删除 snapshotID=10001 ==")
    o, e = run("qcli_volumesnapshot -d snapshotID=10001")
    print(repr(o), repr(e))

    removed = False
    for _ in range(25):  # 最多 ~75s
        o, _ = run("qcli_volumesnapshot -l volumeID=1")
        # 快照被删除后 -l 输出不再含 10001（表头不含数字）
        if "10001" not in o:
            removed = True
            break
        time.sleep(3)
    print("\n== 清除确认 ==")
    print("✅ 已清除" if removed else "⚠️ 仍在系统(稍后自动消失)")
    # 最后再看一次挂载
    o, _ = run("mount | grep 10001")
    print("挂载残留:", o.rstrip() or "(无)")

    c.close()


if __name__ == "__main__":
    main()
