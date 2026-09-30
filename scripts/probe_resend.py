#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 Resend 账号：Key 是否有效 + 域名验证状态。"""
import os, json, urllib.request

KEY = os.environ.get("NASSAFE_RELAY_APIKEY", "").strip()
if not KEY:
    print("❌ 缺少 NASSAFE_RELAY_APIKEY")
    raise SystemExit(1)

req = urllib.request.Request(
    "https://api.resend.com/domains",
    headers={"Authorization": f"Bearer {KEY}"},
)
try:
    data = json.load(urllib.request.urlopen(req, timeout=10))
except urllib.error.HTTPError as e:
    print("❌ Key 无效或请求失败:", e.code, e.read().decode("utf-8", "replace")[:300])
    raise SystemExit(1)

domains = data.get("data", [])
print(f"✅ Key 有效。账号下域名数: {len(domains)}")
for d in domains:
    name = d.get("name")
    status = d.get("status")  # verified / not_started / ...
    region = d.get("region", "")
    print(f"  - {name}  状态={status}  region={region}")
    if status != "verified":
        recs = d.get("records", [])
        for r in recs:
            print(f"      待验证记录: {r.get('type')} {r.get('name')} -> {r.get('value')[:60]}...")

# 也看下账户信息（确认免费层）
try:
    areq = urllib.request.Request(
        "https://api.resend.com/accounts",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    acc = json.load(urllib.request.urlopen(areq, timeout=10))
    print("账户:", acc.get("data", {}).get("email") or acc)
except Exception as e:
    print("(账户信息探测跳过:", e, ")")
