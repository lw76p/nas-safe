#!/bin/bash
# NAS Safe — 一键启动脚本
#
# 自动检测运行环境，选择最合适的启动方式。
# 用法：sudo bash scripts/start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PORT="${NASSAFE_PORT:-8848}"

echo "=============================================================="
echo "  NAS Safe — 启动"
echo "=============================================================="
echo ""

# ------------------------------------------------------------------
# 环境检测
# ------------------------------------------------------------------
echo "【环境检测】"

OS_NAME="未知"
if [ -f /etc/os-release ]; then
  . /etc/os-release 2>/dev/null || true
  OS_NAME="${PRETTY_NAME:-${NAME:-未知}}"
fi
echo "  系统      : $OS_NAME"
echo "  内核      : $(uname -r)"

HAS_BTRFS="否"
HAS_ZFS="否"
command -v btrfs >/dev/null 2>&1 && HAS_BTRFS="是"
command -v zfs  >/dev/null 2>&1 && HAS_ZFS="是"
echo "  btrfs     : $HAS_BTRFS"
echo "  zfs       : $HAS_ZFS"

HAS_DOCKER="否"
command -v docker >/dev/null 2>&1 && HAS_DOCKER="是"
echo "  docker    : $HAS_DOCKER"

HAS_PYTHON="否"
if command -v python3 >/dev/null 2>&1; then
  HAS_PYTHON="是 ($(python3 --version 2>&1 | head -1))"
fi
echo "  python3   : $HAS_PYTHON"

if [ "$HAS_BTRFS" = "否" ] && [ "$HAS_ZFS" = "否" ]; then
  echo ""
  echo "  [警告] 未检测到 btrfs 或 zfs 命令。"
  echo "         快照功能需要底层文件系统支持，程序仍可启动但无法创建快照。"
fi

echo ""

# ------------------------------------------------------------------
# 权限检查
# ------------------------------------------------------------------
if [ "$(id -u)" != "0" ]; then
  echo "【提示】当前不是 root。快照操作可能需要 root 权限。"
  echo "       如果后续操作失败，请用 sudo 重新运行：sudo bash $0"
  echo ""
fi

# ------------------------------------------------------------------
# 启动
# ------------------------------------------------------------------
if [ "$HAS_DOCKER" = "是" ] && [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
  echo "【启动方式】Docker Compose"
  echo ""
  cd "$PROJECT_DIR"
  docker compose up -d
  echo ""
  echo "  容器已启动。"
  echo "  界面地址：http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):$PORT"
  echo ""
  echo "  查看日志：docker compose logs -f"
  echo "  停止服务：docker compose down"

elif [ "$HAS_PYTHON" != "否" ]; then
  echo "【启动方式】直接运行 Python"
  echo ""
  export NASSAFE_WEB_DIR="$PROJECT_DIR/web"
  export NASSAFE_PORT="$PORT"
  echo "  界面地址：http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):$PORT"
  echo "  按 Ctrl+C 停止"
  echo ""
  exec python3 "$PROJECT_DIR/server/app.py"

else
  echo "[错误] 既没有 Docker 也没有 python3，无法启动。"
  echo "       请先安装 Docker 或 Python 3.9+"
  exit 1
fi
