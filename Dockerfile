# NAS Safe — 容器镜像
#
# 基础镜像选 python:3.12-slim（Debian），与飞牛/绿联/OMV 同为 Debian 系
# 镜像内不含 btrfs-progs 的实际二进制依赖 —— 我们调用的是宿主机命令

FROM python:3.12-slim

LABEL org.opencontainers.image.title="NAS Safe"
LABEL org.opencontainers.image.description="NAS 防勒索快照管理 —— 锁住快照，时间轴一键回滚"
LABEL org.opencontainers.image.version="1.0.0"

# 时区 + 基础工具（zfsutils 在部分 slim 源缺失，按可选装：容器实际调用的是宿主机命令）
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tzdata \
      btrfs-progs \
      ca-certificates \
 && (apt-get install -y --no-install-recommends zfsutils-linux || echo "zfsutils-linux skipped") \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 知识库文档解析依赖（pdf/docx）；csv/txt/md 走标准库
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

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
