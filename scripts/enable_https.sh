#!/usr/bin/env bash
# NAS Safe 云网关：备案通过后一键开启 HTTPS
# ---------------------------------------------------------------
# 作用：给 nassafe.tsetch.com 申请 Let's Encrypt 证书、开启 443、
#       并把所有 http 访问自动跳转到 https。
#
# 为什么要等 ICP 备案：
#   大陆云主机在未备案时会拦截 80/443 的 HTTP 访问，certbot 的
#   HTTP-01 校验文件放出去也取不到 → 必定申请失败。备案通过后再跑。
#
# 前置条件（缺一不可）：
#   ① ICP 备案已通过（阿里云控制台显示「已备案」）
#   ② Cloudflare 已加 nassafe A 记录 → 47.108.213.178，且是灰云（DNS only）
#   ③ 云主机 80/443 端口已放行（阿里云安全组 + 本机 ufw）
#
# 用法：在云主机上以 root 执行  bash enable_https.sh
# ---------------------------------------------------------------

set -euo pipefail

DOMAIN="nassafe.tsetch.com"
EMAIL="admin@tsetch.com"
CONF="/etc/nginx/sites-available/nassafe"
SERVER_IP="47.108.213.178"

if [ "$EUID" -ne 0 ]; then
  echo "请用 root 执行：sudo bash enable_https.sh"
  exit 1
fi

echo "=== 0/5 前置检查：域名解析是否指向本机 ==="
RESOLVED=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)
if [ -z "$RESOLVED" ]; then
  echo "❌ 解析不到 $DOMAIN。请先在 Cloudflare 加 A 记录（灰云）指向 $SERVER_IP，等生效后再跑。"
  exit 1
fi
echo "解析结果：$DOMAIN -> $RESOLVED"
if [ "$RESOLVED" != "$SERVER_IP" ]; then
  echo "⚠️  当前解析到 $RESOLVED，与本机 $SERVER_IP 不一致。"
  echo "    如果走的是 Cloudflare 代理（橙云），证书会签在 CF 上，本脚本的 HTTP-01 会失败。"
  echo "    请到 CF 把该记录改成「仅 DNS / 灰云」后重试。"
  read -r -p "仍要继续？(y/N) " ans
  [ "$ans" = "y" ] || exit 1
fi

echo "=== 1/5 备份现有 nginx 配置 ==="
cp -n "$CONF" "${CONF}.bak.$(date +%Y%m%d%H%M%S)" || true
ls -1 /etc/nginx/sites-available/ | grep -c nassafe

echo "=== 2/5 安装 certbot（如已装会跳过） ==="
export DEBIAN_FRONTEND=noninteractive
command -v certbot >/dev/null 2>&1 || apt-get install -y certbot python3-certbot-nginx
nginx -t

echo "=== 3/5 申请证书并自动改写 nginx（同时开启 http→https 跳转） ==="
certbot --nginx -d "$DOMAIN" \
  --non-interactive --agree-tos --email "$EMAIL" \
  --redirect --keep-until-expiring --no-eff-email

echo "=== 4/5 校验并重载 nginx ==="
nginx -t
systemctl reload nginx

echo "=== 5/5 开启证书自动续期 ==="
systemctl enable certbot.timer 2>/dev/null || true
systemctl status certbot.timer --no-pager 2>/dev/null | head -3 || true

echo ""
echo "=== 验收 ==="
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://${DOMAIN}/api/health" || echo "000")
HTTPS_BODY=$(curl -s "https://${DOMAIN}/api/health" | head -c 120 || echo "")
echo "http  跳转码：$HTTP_CODE（301/302 为正常）"
echo "https 健康检查结果：$HTTPS_BODY"
echo ""
echo "✅ 完成。请把访问地址改成：https://${DOMAIN}"
echo ""
echo "对浏览器通知授权的影响（这是上 HTTPS 的主要收益之一）："
echo "  · https 属于「安全来源」，Chrome/Edge 会正常弹出「允许此网站发送通知吗？」询问框，"
echo "    用户点一次「允许」即可，不用再去地址栏站点设置里手动翻。"
echo "  · 局域网直连的 http://192.168.8.62:8848 仍是非安全来源，永远不会有原生询问框；"
echo "    那种场景请用桌面小助手（本机常驻，走 Windows 原生通知）。"
