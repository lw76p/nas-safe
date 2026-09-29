"""只读探测 volumeID=2 快照的目录结构：创建一次快照，列出顶层及若干子目录一层，打印哪些目录含文件；最后删除并轮询清除。"""
import os
import sys
import time
import paramiko

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
from qnap import parse_create_id, parse_snapshots  # noqa: E402

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PASS, timeout=15, look_for_keys=False, allow_agent=False)

    def run(cmd, timeout=40):
        i, o, e = c.exec_command(cmd, timeout=timeout)
        return o.read().decode(errors="replace"), e.read().decode(errors="replace")

    run(f"qcli -l user={USER} pw='{PASS}' saveauthsid=yes")
    vid = "2"
    name = "nassafe_probe_%s" % time.strftime("%Y%m%d%H%M%S")
    print("== 创建快照 ==")
    o, e = run(f"qcli_volumesnapshot -t volumeID={vid} snapshot_name={name} vital=1")
    print(o.rstrip())
    # 拿 sid（用与产品一致的解析）
    sid = parse_create_id(o)
    print("SID=", sid)
    if not sid:
        c.close(); return

    base = f"/mnt/snapshot/{vid}/{sid}"
    print("\n== 顶层目录 ==")
    o, _ = run(f"ls -la '{base}'")
    print(o.rstrip())
    # 顶层目录名
    dirs = [p for p in o.splitlines() if p.startswith('d')]
    print("\n顶层目录名:", [d.split()[-1] for d in dirs][:15])

    # 列前 6 个顶层目录各一层，看是否含文件
    for d in [d.split()[-1] for d in dirs][:6]:
        print(f"\n== 子目录 {d} 一层 ==")
        o, _ = run(f"ls -la '{base}/{d}' 2>/dev/null | head -15")
        lines = [l for l in o.splitlines() if l and not l.startswith('total')]
        files = [l for l in lines if l.startswith('-')]
        print(f"  ({len(files)} 个文件, {len(lines)-len(files)} 个目录) 前几项:")
        print("\n".join(lines[:8]))

    # 清理
    print("\n== 删除快照 ==")
    o, e = run(f"qcli_volumesnapshot -d snapshotID={sid}")
    print(o.rstrip(), e.rstrip())
    removed = False
    for _ in range(20):
        o2, _ = run(f"qcli_volumesnapshot -l volumeID={vid}")
        if name not in o2:
            removed = True
            break
        time.sleep(5)
    print("清除:", "✅" if removed else "⚠️仍在回收")
    c.close()


if __name__ == "__main__":
    main()
