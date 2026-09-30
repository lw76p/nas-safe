#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
远程激活 NAS Safe 厂商邮件中继（零配置通知）。

前置（用户提供，走环境变量，不落盘）：
  NASSAFE_HOST / NASSAFE_USER / NASSAFE_PASS    NAS SSH 凭证
  NASSAFE_RELAY_APIKEY                          Resend/Brevo API Key（必填）
  RELAY_RECIPIENTS                              接收邮箱，多个用逗号或换行（必填）
  NASSAFE_RELAY_PROVIDER                        resend（默认）| brevo
  NASSAFE_RELAY_FROM                            alerts@resend.tsetch.com

流程：
  1. 读现有 nassafe 容器配置（env/ports/binds/privileged/restart/image），避免重建丢参数
  2. 备份容器内 /app/state 到 NAS 主机
  3. docker rm -f + docker run 重建，原 env 全部保留并追加 relay 变量
  4. 等待健康检查
  5. 自动 POST /api/notify/config：启用 relay 通道 + 接收邮箱（完成 Web UI 那步）
  6. 验证 relay_available=true 且配置已保存
"""
import os
import sys
import json
import time
import urllib.request
import paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")
DOCKER = "/share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker"

RELAY_KEY = os.environ.get("NASSAFE_RELAY_APIKEY", "").strip()
RELAY_EMAIL = os.environ.get("RELAY_RECIPIENTS", "").strip()
PROVIDER = os.environ.get("NASSAFE_RELAY_PROVIDER", "resend").strip()
FROM = os.environ.get("NASSAFE_RELAY_FROM", "alerts@resend.tsetch.com").strip()

if not RELAY_KEY:
    print("❌ 缺少 NASSAFE_RELAY_APIKEY（Resend/Brevo API Key）")
    sys.exit(1)
if not RELAY_EMAIL:
    print("❌ 缺少 RELAY_RECIPIENTS（接收邮箱）")
    sys.exit(1)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)


def run(cmd):
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    return out, err


def inspect(fmt):
    out, _ = run(f"{DOCKER} inspect -f '{fmt}' nassafe 2>/dev/null")
    return out


print("=== 1. 读取现有容器配置 ===")
env_list = json.loads(inspect("{{json .Config.Env}}") or "[]") or []
image = inspect("{{.Config.Image}}")
priv = inspect("{{.HostConfig.Privileged}}")
ports = json.loads(inspect("{{json .HostConfig.PortBindings}}") or "{}") or {}
binds = json.loads(inspect("{{json .HostConfig.Binds}}") or "[]") or []
restart = inspect("{{.HostConfig.RestartPolicy.Name}}")
print(f"  镜像={image} privileged={priv} restart={restart}")
print(f"  端口={list(ports.keys())} 挂载={binds}")

port_args = " ".join(f"-p {p.split('/')[0]}:{b[0]['HostPort']}" for p, b in ports.items())
bind_args = " ".join(f"-v {b}" for b in binds)
priv_arg = "--privileged" if priv == "true" else ""
restart_arg = f"--restart {restart}" if restart else "--restart unless-stopped"

# 保留原 env，剔除旧的 relay 变量，追加新值
env_list = [e for e in env_list if not e.startswith("NASSAFE_RELAY_")]
env_list += [
    f'NASSAFE_RELAY_APIKEY={RELAY_KEY}',
    f'NASSAFE_RELAY_PROVIDER={PROVIDER}',
    f'NASSAFE_RELAY_FROM={FROM}',
]
env_args = " ".join(f'-e "{e}"' for e in env_list)

print("=== 2. 备份容器内 /app/state ===")
ts = int(time.time())
bak, _ = run(
    f"{DOCKER} cp nassafe:/app/state /share/Container/nassafe_state_bak_{ts} 2>&1; echo OK"
)
print(f"  备份: {bak}")

print("=== 3. 重建容器并注入 relay 变量 ===")
run(f"{DOCKER} rm -f nassafe 2>&1")
run_cmd = (
    f"{DOCKER} run -d --name nassafe {restart_arg} {priv_arg} "
    f"{port_args} {bind_args} {env_args} {image}"
)
out, err = run(run_cmd)
print(f"  run 输出: {out} {err}")

print("=== 4. 等待健康检查（最多 30s）===")
ok = False
for _ in range(30):
    try:
        urllib.request.urlopen("http://192.168.8.62:8848/api/health", timeout=3)
        ok = True
        break
    except Exception:
        time.sleep(1)
print("  健康:", "✅" if ok else "❌ 超时")

print("=== 5. 自动配置 relay 通道 + 接收邮箱 ===")
recipients = RELAY_EMAIL.replace("\n", ",").replace(";", ",")
cfg = {
    "enabled": True,
    "channels": [{"type": "relay", "recipients": recipients}],
}
req = urllib.request.Request(
    "http://192.168.8.62:8848/api/notify/config",
    data=json.dumps(cfg).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    resp = urllib.request.urlopen(req, timeout=8).read().decode("utf-8")
    print("  配置保存:", resp)
except Exception as e:
    print("  配置失败:", e)

print("=== 6. 验证 ===")
try:
    conf = json.load(urllib.request.urlopen("http://192.168.8.62:8848/api/notify/config", timeout=8))
    print("  relay_available:", conf.get("relay_available"))
    print("  channels:", conf.get("config", {}).get("channels"))
except Exception as e:
    print("  读取失败:", e)

ssh.close()
print("=== 完成 ===")
