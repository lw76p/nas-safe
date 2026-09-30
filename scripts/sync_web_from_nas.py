import os, sys, paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")
DOCKER = "/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker"
TARGET = "nassafe"
WEB_REMOTE = "/app/web"
OUT = os.environ.get("SYNC_OUT", r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nassafe\web_nas")
os.makedirs(OUT, exist_ok=True)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=22, username=USER, password=PASS, timeout=15)

def run(cmd):
    stdin, stdout, stderr = c.exec_command(cmd, timeout=60)
    out = b""
    while True:
        chunk = stdout.read(65536)
        if not chunk: break
        out += chunk
    err = stderr.read().decode("utf-8", "replace")
    return out.decode("utf-8", "replace"), err

# 列出 web 目录
o, e = run(f"HOME=homes/{USER} {DOCKER} exec {TARGET} sh -c 'find {WEB_REMOTE} -type f' 2>&1")
files = [x.strip() for x in o.split("\n") if x.strip() and not x.strip().endswith("/")]
print(f"web files: {len(files)}")
for f in files:
    rel = f[len(WEB_REMOTE):].lstrip("/")
    local = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    o2, e2 = run(f"HOME=homes/{USER} {DOCKER} exec {TARGET} sh -c 'cat {f}' 2>&1")
    if "No such file" in o2 or "cat:" in o2:
        print(f"[skip] {rel}: {o2.strip()[:60]}")
        continue
    with open(local, "w", encoding="utf-8") as fh:
        fh.write(o2)
    print(f"[ok] {rel} -> {len(o2)} bytes")

c.close()
print("DONE ->", OUT)
