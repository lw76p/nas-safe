#!/usr/bin/env bash
# ============================================================
#  TS Safe 一键安装（NAS：威联通 / 群晖 / 其他 x86_64 Linux）
#
#  拉镜像 + 自动选最大存储卷 + 启动，一条命令装完：
#      bash install_nas_safe.sh
#  想指定数据目录（不选则自动挑剩余空间最大的卷）：
#      bash install_nas_safe.sh /share/我的目录
#
#  可选环境变量（一般不用管）：
#      NASSAFE_IMAGE   镜像地址（默认阿里云 lw76p/nas-safe:latest）
#      NASSAFE_NAME    容器名（默认 nassafe）
#      NASSAFE_PORT    端口（默认 8848）
#      NASSAFE_DRY_RUN=1   只打印将执行的命令，不真装
# ============================================================
set -u

IMAGE="${NASSAFE_IMAGE:-crpi-aoopmmoq9zfqyus5.cn-hangzhou.personal.cr.aliyuncs.com/lw76p/nas-safe:latest}"
NAME="${NASSAFE_NAME:-nassafe}"
PORT="${NASSAFE_PORT:-8848}"
DRY="${NASSAFE_DRY_RUN:-0}"

# 内部：回显并执行（DRY_RUN=1 时只回显）
run() {
  echo "  \$ $1"
  if [ "$DRY" != "1" ]; then
    eval "$1" >/dev/null 2>&1 || { echo "[错误] 命令失败：$1"; exit 1; }
  fi
}

echo "=============================================="
echo "  TS Safe 一键安装"
echo "=============================================="

# ---- 1) 找 docker（QNAP 不在 PATH 时用全路径）----
DOCKER=""
for cand in docker \
  /share/CE_CACHEDEV1_DATA/.qpkg/container-station/bin/docker \
  /share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker \
  /usr/local/bin/docker; do
  if command -v "$cand" >/dev/null 2>&1; then DOCKER="$cand"; break; fi
done
if [ -z "$DOCKER" ]; then
  echo "[错误] 没找到 docker。请确认已安装 Container Station / Docker 套件。"
  exit 1
fi
echo "[1/5] docker: $DOCKER"

# ---- 2) 定数据目录：用户指定 > 自动选剩余空间最大的存储卷 ----
BEST_MB=0
if [ "${1:-}" != "" ]; then
  DATA_ROOT="$1"
  echo "[2/5] 数据目录（用户指定）: $DATA_ROOT"
else
  DATA_ROOT=""
  for base in /share/CACHEDEV*_DATA /share/CE_CACHEDEV*_DATA /volume*; do
    [ -d "$base" ] || continue
    mb=$(df -Pm "$base" 2>/dev/null | awk 'NR==2{print $4}')
    [ -n "$mb" ] || continue
    if [ "$mb" -gt "$BEST_MB" ]; then BEST_MB="$mb"; DATA_ROOT="$base"; fi
  done
  [ -z "$DATA_ROOT" ] && DATA_ROOT="/share"
  echo "[2/5] 数据目录（自动选剩余空间最大的卷）: $DATA_ROOT（剩 ${BEST_MB}MB）"
fi
STATE_DIR="$DATA_ROOT/nassafe_state"
mkdir -p "$STATE_DIR" 2>/dev/null || { echo "[错误] 无法创建 $STATE_DIR（需要 root？请用 sudo 重试）"; exit 1; }

# ---- 3) 拉镜像 ----
echo "[3/5] 拉取镜像（首次约 200MB，稍等）..."
run "$DOCKER pull $IMAGE"

# ---- 4) 启动（已有同名容器先移除；数据在 STATE_DIR，不丢）----
OLD=$("$DOCKER" ps -a --filter "name=^/$NAME$" --format '{{.Names}}' 2>/dev/null)
if [ "$OLD" = "$NAME" ]; then
  echo "[4/5] 检测到已有容器 $NAME，先移除（数据目录保留，不影响数据）..."
else
  echo "[4/5] 启动容器..."
fi
run "$DOCKER rm -f $NAME 2>/dev/null || true"
run "$DOCKER run -d --name $NAME --network host -v $STATE_DIR:/app/state --restart unless-stopped $IMAGE"

# ---- 5) 健康检查 + 打印地址 ----
echo "[5/5] 等待服务就绪..."
ok=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 5
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)
  [ "$code" = "200" ] && ok=1 && break
done

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$IP" ] && IP="<NAS的IP>"

echo ""
if [ "$ok" = "1" ]; then
  echo "=============================================="
  echo "  ✅ 安装成功！"
  echo "  控制台:   http://$IP:$PORT"
  echo "  使用指南: http://$IP:$PORT/guide.html"
  echo "  数据目录: $STATE_DIR（升级 / 换容器都不丢）"
  echo "=============================================="
  echo "  下一步：打开控制台注册管理员账号，再按指南接入电脑 / 摄像头等设备。"
else
  echo "  ⚠ 服务未就绪，查看日志：$DOCKER logs $NAME"
  exit 1
fi
