"""把 web 改动 SFTP 到阿里云 /opt/nas-safe/web/

版本化文件（app-YYYYMMDDx.js / style-YYYYMMDDx.css）不再硬编码，
直接从 index.html 解析当前引用，避免漏传/传旧版。
"""
import os
import re
import paramiko

HOST = "47.108.213.178"
USER = "root"
SECRET = r"C:\Users\aa\.workbuddy\cloud-secret.txt"
LOCAL_DIR = r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nas-safe-clone\web"
REMOTE_DIR = "/opt/nas-safe/web"

# 始终传的权威源文件
BASE_FILES = ["app.js", "style.css", "index.html"]


def ref_files():
    """从 index.html 解析出版本化引用文件。"""
    html = open(os.path.join(LOCAL_DIR, "index.html"), encoding="utf-8").read()
    found = set(re.findall(r'(?:href|src)="/([\w.\-]+\.(?:js|css))"', html))
    return sorted(f for f in found if f not in BASE_FILES)


def main():
    files = BASE_FILES + ref_files()
    pwd = open(SECRET, encoding="utf-8").read().strip()
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=pwd, timeout=30)
    sftp = cli.open_sftp()
    try:
        for fn in files:
            local = os.path.join(LOCAL_DIR, fn)
            if not os.path.exists(local):
                print(f"SKIP (missing) {fn}")
                continue
            remote = f"{REMOTE_DIR}/{fn}"
            print(f"PUT {fn} -> {remote}")
            sftp.put(local, remote)
        pat = "|".join(re.escape(f.split(".")[0]) for f in files)
        _, out, _ = cli.exec_command(f"ls -la {REMOTE_DIR}/ | grep -E '{pat}'")
        print("--- remote ---")
        print(out.read().decode("utf-8", "replace").strip())
    finally:
        sftp.close()
        cli.close()


if __name__ == "__main__":
    main()
