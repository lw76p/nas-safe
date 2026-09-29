"""威联通快照浏览/取回 真机验证（创建->挂载->浏览->读文件->卸载->删除->确认清除）。
仅用于一次性实证；密码仅经环境变量传入，不落盘。"""
import os
import re
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

    name = "nassafe_browse_test"
    vid = "1"

    # 1) 创建锁快照
    print("== 1) 创建锁快照 ==")
    o, e = run(f"qcli_volumesnapshot -t volumeID={vid} snapshot_name={name} vital=1")
    print(o.rstrip(), e.rstrip())

    # 2) 解析 snapshotID
    o, _ = run(f"qcli_volumesnapshot -l volumeID={vid}")
    m = re.search(r"(\d+)\s+\S+\s+" + re.escape(name) + r"\b", o)
    sid = m.group(1) if m else None
    print("\n解析 snapshotID =", sid)
    if not sid:
        print("⚠️ 未解析到 SID，中止")
        c.close()
        return

    # 3) 挂载（-m）。若提示未启用共享文件夹，先启用再挂
    print("\n== 2) 挂载快照为共享文件夹 (-m) ==")
    o, e = run(f"qcli_volumesnapshot -m snapshotID={sid}")
    if "enable" in (o + e).lower() or "not" in (o + e).lower():
        print("需要先启用快照共享文件夹导出，执行 snapshot_util --snapshot_share_export_enable 1")
        run("snapshot_util --snapshot_share_export_enable 1")
        time.sleep(2)
        o, e = run(f"qcli_volumesnapshot -m snapshotID={sid}")
    print(o.rstrip(), e.rstrip())

    time.sleep(5)

    # 4) 查共享文件夹信息 + 找挂载路径
    print("\n== 3) 共享文件夹信息 (-S) ==")
    o, e = run(f"qcli_volumesnapshot -S snapshotID={sid}")
    print(o.rstrip(), e.rstrip())

    print("\n== 4) /share 下出现的快照挂载 ==")
    o, _ = run("ls -la /share/ | grep -i -E 'browse_test|snapshot|@'")
    print(o.rstrip() or "(none)")

    # 5) 用 mount 看真实挂载点
    print("\n== 5) mount 中快照相关 ==")
    o, _ = run("mount | grep -i -E 'snapshot|browse_test'")
    print(o.rstrip() or "(none)")

    # 6) 列出挂载点顶层 + 读一个文件（只读 head）
    print("\n== 6) 浏览快照文件 ==")
    # 优先用 -S 解析出的路径；否则扫 /share 下带 @ 或 browse_test 的目录
    cand = []
    o, _ = run("ls -d /share/*/ 2>/dev/null")
    for line in o.split():
        line = line.strip().rstrip("/")
        if "browse_test" in line or "snapshot" in line.lower():
            cand.append(line)
    if not cand:
        # 兜底：列全部 /share 顶层
        o, _ = run("ls -la /share/")
        print(o.rstrip())
    for d in cand[:1]:
        print("挂载目录:", d)
        top, _ = run(f"ls -la '{d}/' | head -20")
        print(top.rstrip())
        # 找一个可 head 的文件
        f, _ = run(f"find '{d}' -maxdepth 2 -type f 2>/dev/null | head -3")
        first = f.splitlines()[0].strip() if f.strip() else ""
        if first:
            print("读文件:", first)
            r, _ = run(f"head -c 300 '{first}'")
            print("--- content head ---")
            print(r.rstrip())

    # 7) 卸载：再用 -m 调一次（toggle），并删除快照清理共享
    print("\n== 7) 清理：删除快照 (-d) ==")
    o, e = run(f"qcli_volumesnapshot -d snapshotID={sid}")
    print(o.rstrip(), e.rstrip())

    # 8) 轮询确认清除
    removed = False
    for _ in range(20):  # 最多 ~60s
        o, _ = run(f"qcli_volumesnapshot -l volumeID={vid}")
        if name not in o:
            removed = True
            break
        time.sleep(3)
    print("\n== 8) 清除确认 ==", "✅ 已清除" if removed else "⚠️ 仍在回收(稍后自动消失)")

    c.close()


if __name__ == "__main__":
    main()
