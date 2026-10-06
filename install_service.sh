#!/usr/bin/env bash
# ============================================================
#  TS Safe 完整版 — Linux / macOS 一键安装 / 卸载
#
#  把完整 TS Safe 引擎注册为「开机自启」的后台服务，
#  Web 控制台默认 http://localhost:8848。
#  所有功能（快照 / 重复文件 / 磁盘清理 / 迁移 / 日报 / 告警）
#  都在这台机器上原生可用，无需再依赖别的设备来「代管」。
#
#  安装：  Linux 用  sudo bash install_service.sh
#          macOS 用  bash install_service.sh
#  卸载：  同上，末尾加  uninstall
# ============================================================
set -u

# 安装根目录 = 本脚本所在目录（应含 server/ 与 web/）
INSTALL_ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$INSTALL_ROOT/server"
WEB_DIR="$INSTALL_ROOT/web"
PORT="${NASSAFE_PORT:-8848}"

if [ ! -f "$SERVER_DIR/app.py" ]; then
  echo "[错误] 未找到 $SERVER_DIR/app.py"
  echo "        请把本脚本放在仓库根目录（与 server/、web/ 同级）后再运行。"
  exit 1
fi

# ---- 卸载分支 ----
if [ "${1:-}" = "uninstall" ]; then
  echo "[步骤] 卸载 TS Safe 服务..."
  if [ "$(uname)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/com.tssafe.server.plist"
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
  else
    systemctl stop tssafe 2>/dev/null || true
    systemctl disable tssafe 2>/dev/null || true
    rm -f /etc/systemd/system/tssafe.service
    systemctl daemon-reload 2>/dev/null || true
  fi
  echo "============================================================"
  echo "  已卸载 TS Safe 服务（venv 与状态目录已保留，方便重装；"
  echo "  如需彻底清理，删除 $INSTALL_ROOT/venv 与状态目录即可）。"
  echo "============================================================"
  exit 0
fi

# Linux 需要 root 才能写 systemd 单元；Mac 用用户 launchd 不需要
if [ "$(uname)" = "Linux" ] && [ "$(id -u)" -ne 0 ]; then
  echo "[需要 root 权限] 正在以 sudo 重新运行本脚本..."
  exec sudo bash "$0" "$@"
fi

# ---- 选择 Python（>=3.10）----
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
    maj=$(echo "$ver" | cut -d. -f1); min=$(echo "$ver" | cut -d. -f2)
    if [ "$maj" -ge 3 ] && { [ "$maj" -gt 3 ] || [ "${min:-0}" -ge 10 ]; }; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "[错误] 未检测到 Python 3.10+。请先安装："
  echo "        Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
  echo "        macOS:          brew install python@3.12"
  exit 1
fi
echo "[信息] 使用 Python：$PY ($("$PY" --version 2>&1))"

# ---- 创建 venv 并装依赖 ----
if [ ! -x "$INSTALL_ROOT/venv/bin/python" ]; then
  echo "[步骤] 创建虚拟环境 venv ..."
  "$PY" -m venv "$INSTALL_ROOT/venv" || { echo "[错误] 创建虚拟环境失败"; exit 1; }
fi
echo "[步骤] 安装依赖（requirements.txt）..."
"$INSTALL_ROOT/venv/bin/pip" install -q -r "$INSTALL_ROOT/requirements.txt" || {
  echo "[错误] 依赖安装失败，请检查网络后重试"; exit 1;
}

# ---- 状态目录 ----
if [ "$(uname)" = "Darwin" ]; then
  STATE_DIR="$HOME/Library/Application Support/NAS Safe/state"
else
  STATE_DIR="/opt/nas-safe/state"
  mkdir -p "$(dirname "$STATE_DIR")" 2>/dev/null || true
fi
mkdir -p "$STATE_DIR"

# ---- 写服务单元并启用 ----
if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.tssafe.server.plist"
  VPY="$INSTALL_ROOT/venv/bin/python"
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.tssafe.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VPY</string>
    <string>$SERVER_DIR/app.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NASSAFE_STATE_DIR</key><string>$STATE_DIR</string>
    <key>NASSAFE_WEB_DIR</key><string>$WEB_DIR</string>
    <key>NASSAFE_PORT</key><string>$PORT</string>
    <key>NASSAFE_BIND_HOST</key><string>0.0.0.0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$STATE_DIR/logs/app.log</string>
  <key>StandardErrorPath</key><string>$STATE_DIR/logs/app.log</string>
  <key>WorkingDirectory</key><string>$SERVER_DIR</string>
</dict>
</plist>
PLIST_EOF
  mkdir -p "$STATE_DIR/logs"
  launchctl load "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
  echo "[步骤] 已通过 launchd 启动（开机自启）。"
else
  cat > /etc/systemd/system/tssafe.service <<UNIT_EOF
[Unit]
Description=TS Safe Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$SERVER_DIR
ExecStart=$INSTALL_ROOT/venv/bin/python $SERVER_DIR/app.py
Environment=NASSAFE_STATE_DIR=$STATE_DIR
Environment=NASSAFE_WEB_DIR=$WEB_DIR
Environment=NASSAFE_PORT=$PORT
Environment=NASSAFE_BIND_HOST=0.0.0.0
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT_EOF
  systemctl daemon-reload
  systemctl enable --now tssafe
  echo "[步骤] 已通过 systemd 启动（开机自启）。"
fi

# ---- 完成 + 引导 ----
echo ""
echo "============================================================"
echo "  TS Safe 已安装并启动（开机自动运行）！"
echo ""
echo "  ★ 第一步：打开控制台设管理员账号"
echo "      本机：   http://localhost:$PORT"
echo "      局域网： http://本机局域网IP:$PORT"
echo ""
CENTER=""
SRC_FILE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/install_source.json"
if [ -f "$SRC_FILE" ]; then
  CENTER=$(sed -n 's/.*"center"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SRC_FILE" 2>/dev/null | head -n 1)
fi
echo "  ★ 第二步（可选）：回到「总控台」接管这台设备"
echo "      本机已是独立主机，所有功能原生可用。"
echo "      若想在原来的总控台里也直接管理它：到总控台「＋ 添加设备 / 扫描」，"
echo "      会把它识别为 TS Safe 服务端（不再是只能监控的端点），迁移/快照等都可用。"
if [ -n "$CENTER" ]; then
  echo "      你的原总控台：$CENTER"
  echo "      这台机器会自动回去登记并保持在线，回到上面地址就能在「联机设备」里看到它。"
fi
echo ""
if [ "$(uname)" = "Darwin" ]; then
  echo "  状态目录： $STATE_DIR"
  echo "  卸载：     bash install_service.sh uninstall"
  echo "============================================================"
  echo "[信息] 正在为你打开控制台页面（首次请设置管理员账号）..."
  sleep 2
  open "http://localhost:$PORT" 2>/dev/null || true
else
  echo "  状态目录： $STATE_DIR"
  echo "  日志：     journalctl -u tssafe -f"
  echo "  卸载：     sudo bash install_service.sh uninstall"
  echo "============================================================"
fi
