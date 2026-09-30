#!/usr/bin/env bash
# 本地或服务器上快速验证 nassafe 反代是否通
set -e
DOMAIN="nassafe.tsetch.com"
echo "检查本地 DNS 解析："
nslookup "${DOMAIN}" || echo "解析失败，请检查 CF A 记录"
echo ""
echo "检查 HTTPS + 反代连通："
curl -sS -o /dev/null -w "HTTP %{http_code} / TLS %{time_total}s\n" "https://${DOMAIN}/api/health"
echo ""
echo "检查证书有效期："
echo | openssl s_client -servername "${DOMAIN}" -connect "${DOMAIN}:443" 2>/dev/null | openssl x509 -noout -dates -subject
