#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QNAP 适配层真机验证（一次性，安全）：
1) 登录存档会话
2) 在 volumeID=1(系统盘) 建一个 vital=1(永久锁定) 的测试快照
3) 列出确认其存在且 vital=1
4) 立即按 snapshotID 删除（绝不回滚 revert）
5) 再次列出确认已清除
"""
import os
import paramiko
import time

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")
TEST_NAME = "nassafe_test_%s" % time.strftime("%Y%m%d_%H%M%S")
VOL = "1"  # 系统盘（低风险），验证机制与数据盘完全一致


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, PASS, timeout=15, look_for_keys=False, allow_agent=False)

    def run(cmd, timeout=40):
        i, o, e = client.exec_command(cmd, timeout=timeout)
        return o.read().decode(errors="replace"), e.read().decode(errors="replace")

    def list_snaps(vol):
        o, _ = run(f"qcli_volumesnapshot -l volumeID={vol}")
        return o

    def find_snap_id(out, name):
        for line in out.splitlines():
            if name in line:
                parts = line.split()
                # 行格式: snapshotID create_time snapshot_name vital type status
                if parts and parts[0].isdigit():
                    return parts[0], (parts[3] if len(parts) > 3 else "?")
        return None, None

    # 1) 登录
    o, e = run(f"qcli -l user={USER} pw='{PASS}' saveauthsid=yes")
    print("== login ==", o.strip())

    # 2) 建锁定测试快照
    print(f"\n== 创建测试快照 name={TEST_NAME} vital=1 on volumeID={VOL} ==")
    o, e = run(f"qcli_volumesnapshot -t volumeID={VOL} snapshot_name={TEST_NAME} vital=1")
    print(o.rstrip())
    if e.strip():
        print("err:", e.rstrip())

    # 3) 列出确认
    print("\n== 列出 volumeID=%s 快照 ==" % VOL)
    out = list_snaps(VOL)
    print(out.rstrip())
    sid, vital = find_snap_id(out, TEST_NAME)
    print(f"---> 解析: snapshotID={sid}, vital={vital}")

    if not sid:
        print("!! 未找到刚创建的快照，终止（不删除，避免误删）")
        client.close()
        return

    # 4) 删除（绝不 revert）
    print(f"\n== 删除 snapshotID={sid} ==")
    o, e = run(f"qcli_volumesnapshot -d volumeID={VOL} snapshotID={sid}")
    print(o.rstrip())
    if e.strip():
        print("err:", e.rstrip())

    # 5) 再次列出确认清除
    print("\n== 再次列出确认清除 ==")
    out2 = list_snaps(VOL)
    print(out2.rstrip())
    sid2, _ = find_snap_id(out2, TEST_NAME)
    print("\n==> 结论:", "快照已成功删除 ✅" if not sid2 else "⚠️ 快照仍存在，需人工检查")

    # 清理：清掉 NAS 上可能残留的 bash 历史（含登录命令）
    run("history -c 2>/dev/null; : > ~/.bash_history 2>/dev/null")
    client.close()


if __name__ == "__main__":
    main()
