import os, sys, paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")
OUT = os.environ.get("SYNC_OUT", r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nassafe\server_nas")
DOCKER = "/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker"
TARGET = "nassafe"
APPDIR = "/app/server"
os.makedirs(OUT, exist_ok=True)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=22, username=USER, password=PASS, timeout=15)

def run(cmd):
    stdin, stdout, stderr = c.exec_command(cmd, timeout=60)
    # 循环读取，避免大文件截断
    out = b""
    while True:
        chunk = stdout.read(65536)
        if not chunk: break
        out += chunk
    err = stderr.read().decode("utf-8", "replace")
    return out.decode("utf-8", "replace"), err

files = ["app.py","notify.py","ai.py","integrity.py","behavior.py","storage.py","qnap.py"]
for f in files:
    o, e = run(f"HOME=homes/{USER} {DOCKER} exec {TARGET} sh -c 'cat {APPDIR}/{f}' 2>&1")
    if not o.strip() or "No such file" in o or "cat:" in o:
        print(f"[skip] {f}: out={o.strip()[:60]!r} err={e.strip()[:60]!r}")
        continue
    with open(os.path.join(OUT, f), "w", encoding="utf-8") as fh:
        fh.write(o)
    print(f"[ok] {f} -> {len(o)} bytes")

c.close()
print("DONE ->", OUT)
