#!/usr/bin/env python3
"""NAS Safe 桌面提醒小助手（Windows）

用途：即使**完全关闭网页**，只要这个小助手在运行，NAS 出现异常就会在
电脑上弹出系统通知。

工作方式：
  1. 定时轮询 NAS Safe（http://NAS的IP:8848）的状态接口（只读，不改动任何数据）
  2. 发现异常（磁盘过热 / CPU 高温高负载 / 卷快满 / 存满趋势 / 快照告警）→
       · AI 配置 = 本地模型（Ollama 等，运行在本机）→ 用本机模型把异常写成一句人话提醒（数据不出本机）
       · AI 配置 = 云端 / 未启用 → 交由 NAS 端按最快通道推送（微信服务号 > 手机推送 > 邮件）
  3. 弹出 Windows 系统通知（右下角，可进通知中心）

运行：
    pythonw desktop_agent.py --nas http://192.168.8.62:8848
（用 pythonw 启动无黑窗；也可加 --interval 120 改轮询间隔秒数）

首次使用建议双击运行一次，确认能弹出测试通知。
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NAS-Safe-Agent"}


def http_json(url, timeout=15, method="GET", body=None):
    data = None
    headers = dict(UA)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------
# Windows 通知：优先 Toast（进通知中心），失败回退气泡提示
# --------------------------------------------------------------------------
def notify(title, text):
    ps_toast = f"""
$ErrorActionPreference='Stop'
try {{
  [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{text}</text></binding></visual></toast>')
  $t = New-Object Windows.UI.Notifications.ToastNotification $xml
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{title}').Show($t)
  exit 0
}} catch {{ exit 1 }}
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_toast],
            capture_output=True, timeout=25,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass
    # 回退：通知区气泡（不进通知中心，但一定能看到）
    ps_balloon = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = '{title}'
$n.BalloonTipText = '{text}'
$n.Visible = $true
$n.ShowBalloonTip(15000)
Start-Sleep -Seconds 8
$n.Dispose()
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_balloon],
            capture_output=True, timeout=30,
        )
        return True
    except Exception as e:
        print("通知失败：", e, file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# 异常判定（与网页端一致）
# --------------------------------------------------------------------------
def collect(m):
    out = []
    cap = m.get("capabilities") or {}
    cpu = m.get("cpu") or {}
    if cap.get("cpu_temp") and cpu.get("temp_c") is not None:
        t = cpu["temp_c"]
        if t >= 90:
            out.append(("cpu-temp", 2, f"CPU 温度过高（{t}°C）"))
        elif t >= 80:
            out.append(("cpu-temp", 1, f"CPU 温度偏高（{t}°C）"))
    if cpu.get("load1") is not None and cpu["load1"] >= 8:
        out.append(("cpu-load", 2 if cpu["load1"] >= 16 else 1, f"系统负载过高（{cpu['load1']}）"))
    for arr, cn in (
        ([d for d in (m.get("disks") or []) if str(d.get("name", "")).startswith("nvme")], "固态"),
        ([d for d in (m.get("disks") or []) if not str(d.get("name", "")).startswith("nvme")], "硬盘"),
    ):
        for i, d in enumerate(arr):
            t = d.get("temp_c")
            if t is None:
                continue
            if t >= 60:
                out.append((f"disk-{d.get('name')}", 2, f"{cn} {i + 1} 过热（{t}°C）"))
            elif t >= 50:
                out.append((f"disk-{d.get('name')}", 1, f"{cn} {i + 1} 温度偏高（{t}°C）"))
    for v in m.get("volumes") or []:
        if not v or v.get("total_kb", 0) <= 0:
            continue
        p = v.get("percent", 0)
        name = str(v.get("mount", "")).split("/")[-1] or v.get("mount")
        if p >= 90:
            out.append((f"vol-{v.get('mount')}", 2, f"「{name}」空间即将用尽（已用 {p}%）"))
        elif p >= 75:
            out.append((f"vol-{v.get('mount')}", 1, f"「{name}」空间偏紧（已用 {p}%）"))
    for t in m.get("trends") or []:
        if t.get("days_to_full"):
            name = str(t.get("mount", "")).split("/")[-1] or t.get("mount")
            out.append((f"trend-{t.get('mount')}", 1, f"「{name}」预计 {t['days_to_full']} 天后存满"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nas", default="http://192.168.8.62:8848", help="NAS Safe 地址")
    ap.add_argument("--interval", type=int, default=120, help="轮询间隔（秒）")
    ap.add_argument("--once", action="store_true", help="只检测一次（调试用）")
    args = ap.parse_args()
    base = args.nas.rstrip("/")

    seen = set()
    print(f"NAS Safe 桌面提醒已启动：{base}（每 {args.interval}s 检查一次，Ctrl+C 退出）")
    notify("NAS Safe 小助手已启动", "关闭网页也会继续守护，异常会在这里提醒你")

    while True:
        try:
            m = http_json(f"{base}/api/system/metrics").get("metrics") or {}
            alerts = (http_json(f"{base}/api/alerts") or {}).get("alerts") or []
            cur = list(collect(m))
            for a in alerts:
                if a.get("level") in ("critical", "warn"):
                    cur.append((f"alert-{a.get('title')}", 2 if a.get("level") == "critical" else 1, a.get("title") or "快照保护异常"))
            keys = {c[0] for c in cur}
            for k in list(seen):
                if k not in keys:
                    seen.discard(k)  # 异常恢复，允许复发再提醒
            fresh = [c for c in cur if c[0] not in seen]
            if fresh:
                for f in fresh:
                    seen.add(f[0])
                summary = "；".join(f[2] for f in fresh)
                level = "critical" if any(f[1] >= 2 for f in fresh) else "warn"
                # 远端通道（微信/邮件）由 NAS 端按最快通道自动优选
                try:
                    http_json(f"{base}/api/notify/alert", method="POST", timeout=15,
                              body={"title": "NAS Safe 异常提醒", "detail": summary, "level": level})
                except Exception as e:
                    print("远端推送跳过：", e)
                # 本机弹窗：本地模型先翻译成一句人话
                text = summary
                try:
                    cfg = http_json(f"{base}/api/ai/config") or {}
                    if cfg.get("enabled") and str(cfg.get("provider", "")).startswith("ollama"):
                        q = f"请用一句通俗中文（30 字以内）提醒电脑前的用户：{summary}。只输出提醒文案。"
                        r = http_json(f"{base}/api/ai/ask", method="POST", timeout=120, body={"question": q})
                        if r.get("text"):
                            text = r["text"].strip()[:60]
                except Exception:
                    pass
                notify("NAS Safe 异常提醒", text)
                print("[提醒]", text)
        except Exception as e:
            print("轮询失败（下次重试）：", e)

        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
