#!/usr/bin/env bash
# 西部数码/Ubuntu 22.04 云主机初始化脚本
# 用途：把 nassafe.tsetch.com 反代到 NAS 192.168.8.62:8848，并自动申请 HTTPS 证书
# 前置条件：① 已购买大陆云主机 ② 已把 nassafe.tsetch.com 的 A 记录指向本机公网 IP（CF 面板，DNS only，灰云）

set -euo pipefail

DOMAIN="nassafe.tsetch.com"
NAS_IP="192.168.8.62"
NAS_PORT="8848"
EMAIL="admin@tsetch.com"          # 用于 Let's Encrypt 提醒，按需修改

if [ "$EUID" -ne 0 ]; then
  echo "请用 root 执行：sudo bash init_gateway.sh"
  exit 1
fi

echo "=== 1/5 更新系统并安装必要软件 ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y nginx curl ufw software-properties-common
apt-get install -y certbot python3-certbot-nginx

echo "=== 2/5 配置防火墙 ==="
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "=== 3/5 写入 nginx 反代配置 ==="
cat > /etc/nginx/sites-available/nassafe <<EOF
server {
    listen 80;
    server_name ${DOMAIN};
    location / {
        proxy_pass http://${NAS_IP}:${NAS_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 30s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/nassafe /etc/nginx/sites-enabled/nassafe
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx
systemctl enable nginx

echo "=== 4/5 申请/续期 HTTPS 证书 ==="
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos --email "${EMAIL}" --redirect

echo "=== 5/5 设置证书自动续期 ==="
systemctl enable certbot.timer || true

echo ""
echo "✅ 网关初始化完成：${DOMAIN} -> ${NAS_IP}:${NAS_PORT}"
echo "可用命令验证：curl -I https://${DOMAIN}/api/health"
