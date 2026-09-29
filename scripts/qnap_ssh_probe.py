#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QNAP SSH 只读探测脚本（不落地密码：凭据通过环境变量传入）
用法（在 bash 中，凭据不写文件）:
  NASSAFE_HOST=192.168.8.62 NASSAFE_USER=lw76p NASSAFE_PASS='lWh19&^55' \
    python scripts/qnap_ssh_probe.py

只做只读命令，绝不创建/删除快照。用于摸清威联通 QTS 的快照底层接口，
为 QNAP B 类适配层提供实证依据。
"""
import os
import paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")


def run(client, cmd, timeout=25):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    return out, err


def main():
    if not PASS:
        print("ERROR: 请通过环境变量 NASSAFE_PASS 传入密码")
        return
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"==> 连接 {USER}@{HOST}:22 ...")
    client.connect(
        HOST, port=22, username=USER, password=PASS,
        timeout=15, look_for_keys=False, allow_agent=False,
    )
    print("==> 连接成功\n")

    commands = [
        # 系统版本
        "uname -a; echo '---'; cat /etc/version 2>/dev/null; getcfg System Version 2>/dev/null",
        # 查找任何 snapshot 相关二进制
        "echo '== which snapshot =='; which snapshot 2>/dev/null; "
        "echo '== /sbin snap* =='; ls /sbin/ 2>/dev/null | grep -i snap; "
        "echo '== /usr/local/sbin snap* =='; ls /usr/local/sbin/ 2>/dev/null | grep -i snap; "
        "echo '== /usr/bin snap* =='; ls /usr/bin/ 2>/dev/null | grep -i snap",
        # 关键：QTS 自带 snapshot 命令帮助
        "echo '== /sbin/snapshot --help =='; /sbin/snapshot --help 2>&1 | head -50",
        # qcli 通用帮助（QNAP 配置工具）
        "echo '== qcli --help =='; qcli --help 2>&1 | head -30",
        # storage_util / 存储管理
        "echo '== storage_util =='; command -v storage_util; ls -la /sbin/storage_util 2>/dev/null",
        # 文件系统挂载情况（ext4/btrfs/卷）
        "echo '== mount grep =='; mount 2>/dev/null | grep -iE 'ext4|btrfs| /share|/dev/md|/dev/vg'",
        # 磁盘/卷概览
        "echo '== df -h =='; df -h 2>/dev/null | head -40",
        # 是否支持 LVM 瘦快照
        "echo '== lvm =='; command -v lvcreate; command -v lvs; ls /sbin/lvcreate 2>/dev/null",
        # 现有快照配置文件
        "echo '== snapshot.conf =='; cat /etc/config/snapshot.conf 2>/dev/null | head -40; "
        "echo '== /etc/config snapshot* =='; ls -la /etc/config/ 2>/dev/null | grep -i snap",
        # 共享文件夹（/share 结构）
        "echo '== /share =='; ls -la /share/ 2>/dev/null | head -30",
    ]

    for c in commands:
        print("\n==================== $ " + c[:60] + " ====================")
        try:
            o, e = run(client, c)
            if o.strip():
                print(o.rstrip())
            if e.strip():
                print("--- stderr ---\n" + e.rstrip())
        except Exception as ex:
            print("ERR:", repr(ex))
    client.close()
    print("\n==> 探测完成（只读，未做任何修改）")


if __name__ == "__main__":
    main()
