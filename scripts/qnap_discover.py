#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QNAP 登录 + 只读列举（登录存档会话，便于后续适配层测试）。不创建/删除快照。"""
import os
import paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, PASS, timeout=15, look_for_keys=False, allow_agent=False)

    def run(cmd, timeout=30):
        i, o, e = client.exec_command(cmd, timeout=timeout)
        return o.read().decode(errors="replace"), e.read().decode(errors="replace")

    # 1) 登录并存会话
    o, e = run(f"qcli -l user={USER} pw='{PASS}' saveauthsid=yes")
    print("== qcli login ==\n" + o.rstrip())
    if e.strip():
        print("err:", e.rstrip())

    # 2) 卷列表（拿可读卷名）
    print("\n== qcli_volume -l ==")
    o, e = run("qcli_volume -l")
    print(o.rstrip())
    if e.strip():
        print("err:", e.rstrip())

    # 3) 数据卷(volumeID=2)现有快照
    print("\n== qcli_volumesnapshot -l volumeID=2 ==")
    o, e = run("qcli_volumesnapshot -l volumeID=2")
    print(o.rstrip())
    if e.strip():
        print("err:", e.rstrip())

    # 4) 计数
    print("\n== qcli_volumesnapshot -C volumeID=2 ==")
    o, e = run("qcli_volumesnapshot -C volumeID=2")
    print(o.rstrip())
    if e.strip():
        print("err:", e.rstrip())

    # 5) 取快照语法（故意少参数，看必填项）
    print("\n== qcli_volumesnapshot -t (看必填参数) ==")
    o, e = run("qcli_volumesnapshot -t")
    print(o.rstrip())
    if e.strip():
        print("err:", e.rstrip())

    # 6) 保留/锁定语法
    print("\n== qcli_volumesnapshot -T (看必填参数) ==")
    o, e = run("qcli_volumesnapshot -T")
    print(o.rstrip())
    if e.strip():
        print("err:", e.rstrip())

    client.close()
    print("\n==> 只读发现完成（未创建/删除任何快照）")


if __name__ == "__main__":
    main()
