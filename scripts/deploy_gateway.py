"""
把 nassafe 网关一键部署到新的西部数码/Ubuntu 云主机。
用法：
  python deploy_gateway.py --host <云主机IP> --user root --password <密码>
运行前请确保：
  1) 云主机已购买并拿到公网 IP
  2) nassafe.tsetch.com 已在 CF 设为 A 记录 -> 该 IP（DNS only，灰云）
"""
import argparse
import os
import paramiko
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REMOTE_SCRIPTS = "/root/nassafe-gateway"


def ssh_exec(client: paramiko.SSHClient, cmd: str, desc: str):
    print(f"\n=== {desc} ===")
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"{desc} 失败，退出码 {rc}")


def main():
    parser = argparse.ArgumentParser(description="Deploy nassafe gateway to new cloud host")
    parser.add_argument("--host", required=True, help="云主机公网 IP")
    parser.add_argument("--user", default="root", help="SSH 用户名")
    parser.add_argument("--password", required=True, help="SSH 密码")
    args = parser.parse_args()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=args.host, username=args.user, password=args.password, timeout=30)

    try:
        sftp = client.open_sftp()
        ssh_exec(client, f"mkdir -p {REMOTE_SCRIPTS}", "创建远程目录")
        for name in ("init_gateway.sh", "gateway_check.sh"):
            local = ROOT / name
            remote = f"{REMOTE_SCRIPTS}/{name}"
            sftp.put(str(local), remote)
            ssh_exec(client, f"chmod +x {remote}", f"设置 {name} 可执行")
        sftp.close()

        ssh_exec(
            client,
            f"cd {REMOTE_SCRIPTS} && bash init_gateway.sh",
            "执行网关初始化脚本（安装 nginx + 申请证书 + 反代到 NAS）",
        )
        ssh_exec(
            client,
            f"cd {REMOTE_SCRIPTS} && bash gateway_check.sh",
            "验证反代与证书",
        )
        print("\n✅ 网关部署完成。请把 https://nassafe.tsetch.com 加到微信公众平台 request 合法域名。")
    finally:
        client.close()


if __name__ == "__main__":
    main()
