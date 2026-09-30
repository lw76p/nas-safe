import os, sys, paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")
DOCKER = "/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker"
TARGET = "nassafe"
BASE = r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nassafe"
HOST_TMP = "/share/CE_CACHEDEV1_DATA/nassafe_deploy"

FILES = [
    ("server/ai.py", "/app/server/ai.py"),
    ("server/integrity.py", "/app/server/integrity.py"),
    ("server/behavior.py", "/app/server/behavior.py"),
    ("server/notify.py", "/app/server/notify.py"),
    ("server/app.py", "/app/server/app.py"),
    ("web/app.js", "/app/web/app.js"),
    ("web/index.html", "/app/web/index.html"),
    ("web/style.css", "/app/web/style.css"),
]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=22, username=USER, password=PASS, timeout=15)
sftp = c.open_sftp()

def run(cmd):
    stdin, stdout, stderr = c.exec_command(cmd, timeout=60)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return out, err

# 准备主机临时目录
run(f"mkdir -p {HOST_TMP}")

# 备份线上 app.py（回滚用）
run(f"HOME=homes/{USER} {DOCKER} cp {TARGET}:/app/server/app.py {HOST_TMP}/app.py.bak 2>&1")
print("backup app.py:", run(f"ls -l {HOST_TMP}/app.py.bak")[0].strip())

# 上传 + docker cp
for local_rel, remote_in_container in FILES:
    local = os.path.join(BASE, local_rel)
    host_path = HOST_TMP + "/" + os.path.basename(local_rel)
    sftp.put(local, host_path)
    o, e = run(f"HOME=homes/{USER} {DOCKER} cp {host_path} {TARGET}:{remote_in_container} 2>&1")
    print(f"cp {os.path.basename(local_rel)} -> {remote_in_container}: out={o.strip()!r} err={e.strip()!r}")

# 重启容器使 app.py 生效
o, e = run(f"HOME=homes/{USER} {DOCKER} restart {TARGET} 2>&1")
print("restart:", o.strip(), e.strip())

sftp.close()
c.close()
print("DEPLOY DONE")
