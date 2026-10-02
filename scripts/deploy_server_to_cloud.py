"""把 server 端改动同步到阿里云轻量（47.108.213.178）。

绕开偶发卡死的 git pull：直接 SFTP 传文件，再远程验证 import 并重启 nas-safe.service。
用法（用带 paramiko 的 venv 运行）：
    python deploy_server_to_cloud.py
"""
from __future__ import annotations

import os
import paramiko

HOST = "47.108.213.178"
PORT = 22
USER = "root"
PASS = "Lwp19&^55"

LOCAL = "C:/Users/aa/WorkBuddy/2026-09-29-16-08-29/nas-safe-clone/server"
REMOTE_DIR = "/opt/nas-safe/server"
FILES = [
    "brands.py",
    "storage.py",
    "metrics.py",
    "anomalies.py",
    "daily_report.py",
    "snapshot_vss.py",
    "snapshot_apfs.py",
    "snapshot_rsync.py",
    "app.py",
    "devices.py",
    "netscan.py",
    "auth.py",
]


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=30)
    try:
        sftp = ssh.open_sftp()
        for f in FILES:
            local = os.path.join(LOCAL, f)
            remote = REMOTE_DIR + "/" + f
            sftp.put(local, remote)
            print("put", remote)
        sftp.close()

        # 远程验证 import（确保新代码不破坏 Linux 运行环境）
        _, stdout, stderr = ssh.exec_command(
            "cd /opt/nas-safe/server && python3 -c 'import storage, brands; "
            "print(\"import ok\")'"
        )
        print("IMPORT OUT:", stdout.read().decode().strip())
        print("IMPORT ERR:", stderr.read().decode().strip())

        # 重启服务
        _, stdout, stderr = ssh.exec_command(
            "systemctl restart nas-safe && sleep 2 && systemctl is-active nas-safe"
        )
        print("RESTART:", stdout.read().decode().strip(),
              stderr.read().decode().strip())
    finally:
        ssh.close()


if __name__ == "__main__":
    main()
