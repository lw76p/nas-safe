# TS Safe — 全功能开源免费版
# 构建：docker build -t nas-safe:latest .
# 运行：docker run -d --name nassafe --network host \
#         -v /share/CE_CACHEDEV1_DATA/nassafe_state:/app/state \
#         --restart unless-stopped nas-safe:latest
# 基础镜像走 DaoCloud 中转（国内可拉；与官方 python:3.12-slim 同源）
FROM docker.m.daocloud.io/library/python:3.12-slim

WORKDIR /app

# 依赖：仅知识库文档解析用（pdf/docx）；基础服务全走标准库
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 程序与前端（含 /guide.html 使用指南）
COPY server/ ./server/
COPY web/ ./web/
COPY docs/ ./docs/
COPY 首次使用指南.txt ./

ENV NASSAFE_PORT=8848 \
    NASSAFE_BIND_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# 账号/设备/配置/快照记录全在这里，挂出来升级不丢
VOLUME ["/app/state"]

EXPOSE 8848
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8848/api/health',timeout=4)" || exit 1

CMD ["python3", "server/app.py"]
