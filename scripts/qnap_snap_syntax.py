#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QNAP 快照命令语法探测（只读，除 --help 外不动任何东西）"""
import os
import paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")


def run(client, cmd, timeout=25):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=22, username=USER, password=PASS,
                   timeout=15, look_for_keys=False, allow_agent=False)

    commands = [
        # 官方卷快照 CLI 帮助（最关键）
        "echo '== qcli_volumesnapshot --help =='; qcli_volumesnapshot --help 2>&1 | head -80",
        # snapshot_util 帮助
        "echo '== snapshot_util --help =='; snapshot_util --help 2>&1 | head -80",
        # qsnaputil 帮助
        "echo '== qsnaputil --help =='; qsnaputil --help 2>&1 | head -60",
        # 现有快照配置目录内容
        "echo '== /etc/config/qsnapshot =='; ls -la /etc/config/qsnapshot/ 2>/dev/null",
        # LVM 现有卷与瘦池/快照（看结构）
        "echo '== lvs -a =='; lvs -a --units g 2>/dev/null | head -60",
        # 用官方工具列出现有快照（理解输出格式/命名）
        "echo '== qcli_volumesnapshot list =='; qcli_volumesnapshot --list 2>&1 | head -40",
        "echo '== qsnaputil -l =='; qsnaputil -l 2>&1 | head -40",
        # 卷名映射（QTS 怎么叫这些卷）
        "echo '== qcli_volume --help =='; qcli_volume --help 2>&1 | head -40",
    ]

    for c in commands:
        print("\n==================== $ " + c[:55] + " ====================")
        try:
            o, e = run(client, c)
            if o.strip():
                print(o.rstrip())
            if e.strip():
                print("--- stderr ---\n" + e.rstrip())
        except Exception as ex:
            print("ERR:", repr(ex))
    client.close()
    print("\n==> 语法探测完成（只读）")


if __name__ == "__main__":
    main()
