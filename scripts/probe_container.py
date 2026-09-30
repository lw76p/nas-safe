import os, paramiko, json

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")
DOCKER = "/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

def run(cmd):
    stdin, stdout, stderr = ssh.exec_command(cmd)
    return stdout.read().decode("utf-8", "replace").strip()

print("=== compose 项目标签（空=非compose起的）===")
print(run(f"{DOCKER} inspect -f '{{{{ index .Config.Labels \"com.docker.compose.project\" }}}}' nassafe 2>&1"))
print("=== 当前容器 env 中是否已有 RELAY ===")
print(run(f"{DOCKER} inspect -f '{{{{ json .Config.Env }}}}' nassafe 2>&1 | tr ',' '\\n' | grep -i relay || echo '无 RELAY 变量'"))
print("=== 镜像名 ===")
print(run(f"{DOCKER} inspect -f '{{{{.Config.Image}}}}' nassafe 2>&1"))
print("=== docker compose 是否在 PATH ===")
print(run("which docker-compose 2>/dev/null; which docker 2>/dev/null; ls /share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker-compose 2>/dev/null || echo '无独立 docker-compose'"))
ssh.close()
