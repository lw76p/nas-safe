# NAS Safe — 容器镜像
#
# 基础镜像：python:3.12-slim（Debian）。适用两类部署：
#   1) btrfs 宿主本地模式（飞牛/TrueNAS/OMV）：容器内带 btrfs-progs 直接调宿主机子卷
#   2) 威联通等远程管理模式：容器内通过 paramiko SSH 回连 NAS 调 qcli（不挂宿主根、不动数据）
# 注意：ZFS 需宿主内核模块，容器内无法使用，ZFS 用户请裸机运行本工具。

FROM python:3.12-slim

LABEL org.opencontainers.image.title="NAS Safe"
LABEL org.opencontainers.image.description="NAS 防勒索快照管理 —— 锁住快照，时间轴一键回滚"
LABEL org.opencontainers.image.version="1.0.0"

# 时区 + 基础工具
# btrfs-progs：飞牛/TrueNAS/OMV 等 btrfs 宿主的本地模式需要（btrfs 子卷命令）。
# zfs 不支持容器内部署（需宿主内核模块），ZFS 用户请在 ZFS 宿主机裸机运行。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tzdata \
      btrfs-progs \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# QNAP 远程管理模式需要 paramiko（SSH 到 QTS 调用 qcli_volumesnapshot）。
# 仅容器部署才需要；裸机直接跑 server/app.py 仍是零依赖。
RUN pip install --no-cache-dir paramiko

WORKDIR /app

COPY server/ /app/server/
COPY web/ /app/web/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NASSAFE_HOST=0.0.0.0 \
    NASSAFE_PORT=8848 \
    NASSAFE_WEB_DIR=/app/web \
    TZ=Asia/Shanghai

EXPOSE 8848

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8848/api/health',timeout=3)"

CMD ["python3", "/app/server/app.py"]
