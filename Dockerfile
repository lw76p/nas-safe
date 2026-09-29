# NAS Safe — 容器镜像
#
# 基础镜像选 python:3.12-slim（Debian），与飞牛/绿联/OMV 同为 Debian 系
# 镜像内不含 btrfs-progs 的实际二进制依赖 —— 我们调用的是宿主机命令

FROM python:3.12-slim

LABEL org.opencontainers.image.title="NAS Safe"
LABEL org.opencontainers.image.description="NAS 防勒索快照管理 —— 锁住快照，时间轴一键回滚"
LABEL org.opencontainers.image.version="1.0.0"

# 时区 + 基础工具
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tzdata \
      btrfs-progs \
      zfsutils-linux \
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
