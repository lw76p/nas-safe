"""把 web 改动 SFTP 到阿里云 /opt/nas-safe/web/"""
import os
import sys
import paramiko

HOST = "47.108.213.178"
USER = "root"
SECRET = r"C:\Users\aa\.workbuddy\cloud-secret.txt"
LOCAL_DIR = r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nas-safe-clone\web"
REMOTE_DIR = "/opt/nas-safe/web"
FILES = [
    "app.js",
    "style.css",
    "index.html",
    "app-20261002h.js",
    "style-20261002h.css",
]

def main():
    pwd = open(SECRET, encoding="utf-8").read().strip()
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=pwd, timeout=30)
    sftp = cli.open_sftp()
    try:
        for fn in FILES:
            local = os.path.join(LOCAL_DIR, fn)
            remote = f"{REMOTE_DIR}/{fn}"
            print(f"PUT {fn} -> {remote}")
            sftp.put(local, remote)
        # 确认
        _, out, _ = cli.exec_command(f"ls -la {REMOTE_DIR}/ | grep -E 'app-20261002h|style-20261002h|index.html'")
        print("--- remote ---")
        print(out.read().decode("utf-8", "replace").strip())
    finally:
        sftp.close()
        cli.close()

if __name__ == "__main__":
    main()
