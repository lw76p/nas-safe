#!/usr/bin/env bash
# ============================================================
#  TS Safe 一键安装（NAS：威联通 / 群晖 / 绿联 / 极空间 / 其他 x86_64 Linux）
#
#  拉镜像 + 自动选最大存储卷 + 启动，一条命令装完：
#      bash install_nas_safe.sh
#  想指定数据目录（不选则自动挑剩余空间最大的卷）：
#      bash install_nas_safe.sh /volume2
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

# 内部：回显并执行（DRY_RUN=1 时只回显）；失败时打印输出尾部，不再吞报错
run() {
  echo "  \$ $1"
  if [ "$DRY" != "1" ]; then
    _out=$(eval "$1" 2>&1) || {
      echo "$_out" | tail -8 | sed 's/^/    /'
      echo "[错误] 命令失败：$1"
      exit 1
    }
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

# ---- 1.5) docker 守护进程 + 仓库连通性预检 ----
if ! "$DOCKER" info >/dev/null 2>&1; then
  echo "[错误] docker 守护进程没有运行。请先启动 Docker/Container Station 套件再重试。"
  exit 1
fi
REG_HOST="${IMAGE%%/*}"
curl_ok=""
if command -v curl >/dev/null 2>&1; then
  curl_ok=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$REG_HOST/v2/" 2>/dev/null)
fi
if [ -n "$curl_ok" ]; then
  echo "[1/5] 仓库连通性: https://$REG_HOST 可达（HTTP $curl_ok，401 为正常鉴权响应）"
else
  echo "[1/5] 仓库连通性: 跳过检测（无 curl），由 docker pull 直接验证"
fi

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

# ---- 3) 拉镜像（显示原始报错 + 自动重试一次）----
echo "[3/5] 拉取镜像（首次约 200MB，稍等）..."
pull_out=""
pull_once() {
  echo "  \$ $DOCKER pull $IMAGE"
  if [ "$DRY" != "1" ]; then
    pull_out=$("$DOCKER" pull "$IMAGE" 2>&1)
    return $?
  fi
  return 0
}
if pull_once || pull_once; then
  echo "  镜像就绪: $IMAGE"
else
  echo ""
  echo "  ---- docker pull 原始报错（最后 15 行）----"
  echo "$pull_out" | tail -15 | sed 's/^/    /'
  echo "  ----------------------------------------"
  echo "[错误] docker pull 失败。常见原因对照："
  echo "  · 报错含 no such host / i/o timeout / TLS  → NAS 网络/DNS 出不去，换 DNS（223.5.5.5）或检查防火墙"
  echo "  · 报错含 unauthorized / denied           → 联系镜像提供方确认仓库状态"
  echo "  · 报错含 no space left                   → 磁盘满，df -h 查看"
  echo "  · 报错含 manifest unknown / not found    → docker 版本过旧，$DOCKER version 查看（需 20.10+）"
  exit 1
fi

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
