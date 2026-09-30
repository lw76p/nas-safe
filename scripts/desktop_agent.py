#!/usr/bin/env python3
"""NAS Safe 桌面小助手（Windows）

定位：**电脑端的提醒全部由它负责**，网页端不再需要弹窗授权。
平时缩在屏幕右下角（一个小盾牌图标，几乎不占资源），只在 NAS 出现异常
或保护状态变动时主动弹一次 Windows 通知；通知会自动消失。

阅读规则（按用户要求）：
  · 同一条异常**只弹一次**，异常恢复后才可能再弹
  · 弹过但你**没看**的，不会重复弹，只在右下角图标上显示一个红色感叹号
  · 你点开图标读完，感叹号消失，该项不再提醒

安装 / 使用：
    pythonw desktop_agent.py --install      # 一键安装：探测 NAS + 开机自启 + 注册协议
    pythonw desktop_agent.py                # 直接运行（有配置）
    pythonw desktop_agent.py --nas http://192.168.8.62:8848
    pythonw desktop_agent.py --interval 180 # 改轮询间隔（默认 120 秒，资源占用极低）

其它参数：
    --protocol nassafe-agent://start|stop   # 网页设置页一键启停（安装时自动注册）
    --no-ui                                 # 纯后台模式，不显示右下角图标
    --once                                  # 只检测一次（调试）
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NAS-Safe-Agent"}
DEFAULT_INTERVAL = 120
CTRL_PORT = 18765          # 本机控制端口（只监听 127.0.0.1，不外泄）
PROTOCOL = "nassafe-agent"  # 浏览器拉起本机小助手的自定义协议
STOP_EVENT = threading.Event()
LOCK = threading.Lock()


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
# Windows 通知（Toast，自动消失并进通知中心）；失败回退气泡
# --------------------------------------------------------------------------
def notify(title, text):
    safe_t = str(title).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_x = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ps_toast = f"""
$ErrorActionPreference='Stop'
try {{
  [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml('<toast scenario="reminder"><visual><binding template="ToastGeneric"><text>{safe_t}</text><text>{safe_x}</text></binding></visual></toast>')
  $t = New-Object Windows.UI.Notifications.ToastNotification $xml
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('NAS Safe').Show($t)
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
    ps_balloon = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = '{safe_t}'
$n.BalloonTipText = '{safe_x}'
$n.Visible = $true
$n.ShowBalloonTip(10000)
Start-Sleep -Seconds 6
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
# 配置与未读消息持久化：%APPDATA%\NASSafeAgent\
# --------------------------------------------------------------------------
def config_dir():
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    return os.path.join(base or os.path.expanduser("~"), "NASSafeAgent")


def _config_path():
    return os.path.join(config_dir(), "config.json")


def load_config():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(config_dir(), exist_ok=True)
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存配置失败：", e, file=sys.stderr)


def load_unread():
    return load_config().get("unread") or []


def save_unread(items):
    cfg = load_config()
    cfg["unread"] = items[-50:]  # 最多保留 50 条
    save_config(cfg)


# --------------------------------------------------------------------------
# 本机控制服务（网页设置页：/ping 检测在线，/stop 请求退出）
# --------------------------------------------------------------------------
class _CtrlHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ping":
            body = json.dumps({"ok": True, "agent": "nassafe", "ver": 3,
                               "unread": len(load_unread())}).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/stop":
            body = b'{"ok":true,"stopping":true}'
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            STOP_EVENT.set()
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def log_message(self, *a):
        pass


def start_control_server():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", CTRL_PORT), _CtrlHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
    except OSError:
        return None


def agent_online(timeout=1.5):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CTRL_PORT}/ping", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("agent") == "nassafe"
    except Exception:
        return False


def agent_stop_remote(timeout=3):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CTRL_PORT}/stop", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


# --------------------------------------------------------------------------
# NAS 自动发现
# --------------------------------------------------------------------------
def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _probe_host(ip, port=8848, timeout=0.5):
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
    except Exception:
        return None
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"http://{ip}:{port}/api/health", headers=UA), timeout=3
        ) as r:
            if json.loads(r.read().decode("utf-8")).get("ok"):
                return f"http://{ip}:{port}"
    except Exception:
        pass
    return None


def discover_nas(extra=None):
    found = []
    for u in extra or []:
        u = u.rstrip("/")
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{u}/api/health", headers=UA), timeout=3
            ) as r:
                if json.loads(r.read().decode("utf-8")).get("ok"):
                    found.append(u)
        except Exception:
            pass
    ip = _local_ip()
    if ip:
        prefix = ".".join(ip.split(".")[:3])
        with ThreadPoolExecutor(max_workers=64) as ex:
            futs = [ex.submit(_probe_host, f"{prefix}.{i}") for i in range(1, 255)]
            for f in as_completed(futs):
                r = f.result()
                if r and r not in found:
                    found.append(r)
    return found


# --------------------------------------------------------------------------
# 首次安装：确认窗口 + 开机自启 + 协议注册
# --------------------------------------------------------------------------
def pick_dialog(candidates, manual_default=""):
    try:
        import tkinter as tk
    except Exception:
        return None
    chosen = {"url": None}
    root = tk.Tk()
    root.title("NAS Safe 小助手 · 安装")
    root.geometry("470x300")
    root.attributes("-topmost", True)
    tk.Label(root, text="检测到以下 NAS Safe 服务，选择要守护的地址：",
             anchor="w", justify="left").pack(fill="x", padx=14, pady=(14, 6))
    var = tk.StringVar(value=candidates[0] if candidates else "")
    for c in candidates[:5]:
        tk.Radiobutton(root, text=c, variable=var, value=c).pack(anchor="w", padx=18)
    row = tk.Frame(root)
    row.pack(fill="x", padx=14, pady=(8, 2))
    tk.Label(row, text="或手动填写：").pack(side="left")
    ent = tk.Entry(row)
    ent.pack(side="left", fill="x", expand=True)
    if manual_default:
        ent.insert(0, manual_default)

    def on_ok():
        u = (ent.get().strip() or var.get()).strip()
        if u:
            if not u.startswith("http"):
                u = "http://" + u
            chosen["url"] = u.rstrip("/")
        root.destroy()

    tk.Button(root, text="安装并开机自启", command=on_ok, width=22,
              bg="#2563eb", fg="white").pack(pady=(6, 2))
    tk.Button(root, text="取消", command=root.destroy, width=10).pack()
    tk.Label(root, text="安装后缩在右下角，只在异常时提醒，几乎不占资源。", fg="#666").pack(side="bottom", pady=8)
    root.mainloop()
    return chosen["url"]


def _self_cmd():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def ensure_autostart(base, interval):
    if os.name != "nt":
        return False
    try:
        import winreg
        cmd = f'{_self_cmd()} --nas "{base}" --interval {interval}'
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "NASSafeAgent", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print("写开机自启失败：", e, file=sys.stderr)
        return False


def remove_autostart():
    if os.name != "nt":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, "NASSafeAgent")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print("移除开机自启失败：", e, file=sys.stderr)
        return False


def register_protocol():
    if os.name != "nt":
        return False
    try:
        import winreg
        inner = f'{_self_cmd()} --protocol "%1"'
        root = rf"Software\Classes\{PROTOCOL}"
        for sub, name, val in (
            ("", None, "URL:NAS Safe Agent Protocol"),
            ("", "URL Protocol", ""),
            (r"\shell\open\command", None, inner),
        ):
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + sub)
            if name is None:
                winreg.SetValue(key, "", winreg.REG_SZ, val)
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, val)
            winreg.CloseKey(key)
        return True
    except Exception as e:
        print("注册协议失败：", e, file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# 异常判定（与网页端规则一致）
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


# --------------------------------------------------------------------------
# 右下角小图标 + 未读消息中心（tkinter，无第三方依赖）
# --------------------------------------------------------------------------
class AgentUI:
    def __init__(self, base, on_quit=None):
        self.base = base
        self.on_quit = on_quit
        self.root = None
        self.badge = None
        self.inbox_win = None
        self.items = load_unread()
        try:
            import tkinter as tk
            self.tk = tk
        except Exception:
            self.tk = None

    def available(self):
        return self.tk is not None

    def build(self):
        tk = self.tk
        self.root = tk.Tk()
        self.root.withdraw()  # 主窗口隐藏，只显示右下角小图标
        self.badge = tk.Toplevel(self.root)
        self.badge.overrideredirect(True)
        self.badge.attributes("-topmost", True)
        self.badge.configure(bg="#111827")
        w, h = 88, 34
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.badge.geometry(f"{w}x{h}+{sw - w - 24}+{sh - h - 58}")
        self.label = tk.Label(self.badge, text="", bg="#111827", fg="#7dd3fc",
                              font=("Microsoft YaHei UI", 10), cursor="hand2")
        self.label.pack(expand=True, fill="both")
        self.label.bind("<Button-1>", lambda e: self.open_inbox())
        self.badge.bind("<Button-3>", lambda e: self._menu(e))
        self.label.bind("<Button-3>", lambda e: self._menu(e))
        self.refresh()
        self.badge.after(1000, self._watch_stop)

    def _menu(self, ev):
        tk = self.tk
        m = tk.Menu(self.badge, tearoff=0)
        m.add_command(label="查看未读消息", command=self.open_inbox)
        m.add_command(label="全部标记已读", command=lambda: self.mark_all_read())
        m.add_separator()
        m.add_command(label="退出小助手", command=self.quit)
        m.tk_popup(ev.x_root, ev.y_root)

    def refresh(self):
        if not self.badge:
            return
        n = len(self.items)
        if n:
            self.label.configure(text=f"!  {n}", fg="#ff6b6b")
        else:
            self.label.configure(text="🛡 保护中", fg="#7dd3fc")

    def add_unread(self, key, text):
        with LOCK:
            if any(i.get("key") == key for i in self.items):
                return
            self.items.append({"key": key, "text": text,
                               "ts": time.strftime("%m-%d %H:%M")})
            save_unread(self.items)
        if self.badge:
            self.badge.after(0, self.refresh)

    def open_inbox(self):
        if self.inbox_win and self.inbox_win.winfo_exists():
            self.inbox_win.lift()
            return
        tk = self.tk
        win = tk.Toplevel(self.root)
        self.inbox_win = win
        win.title("NAS Safe 未读提醒")
        win.geometry("520x330")
        win.attributes("-topmost", True)
        tk.Label(win, text="未读提醒（点一条即标记已读）", anchor="w").pack(fill="x", padx=12, pady=(12, 6))
        frame = tk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12)
        lb = tk.Listbox(frame, activestyle="none")
        sb = tk.Scrollbar(frame, command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def refill():
            lb.delete(0, "end")
            for it in self.items:
                lb.insert("end", f"[{it.get('ts','')}] {it.get('text','')}")

        def on_pick(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            idx = sel[0]
            with LOCK:
                if idx < len(self.items):
                    self.items.pop(idx)
                    save_unread(self.items)
            refill()
            self.refresh()

        lb.bind("<<ListboxSelect>>", on_pick)
        lb.bind("<Double-Button-1>", on_pick)
        refill()

        row = tk.Frame(win)
        row.pack(fill="x", padx=12, pady=8)
        tk.Button(row, text="全部标记已读", command=lambda: (self.mark_all_read(), refill())).pack(side="left")
        tk.Button(row, text="关闭", command=win.destroy).pack(side="right")

    def mark_all_read(self):
        with LOCK:
            self.items = []
            save_unread(self.items)
        self.refresh()

    def _watch_stop(self):
        if STOP_EVENT.is_set():
            self.quit()
            return
        self.badge.after(1000, self._watch_stop)

    def quit(self):
        try:
            if self.inbox_win:
                self.inbox_win.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.build()
        self.root.mainloop()


# --------------------------------------------------------------------------
# 守护轮询（后台线程）
# --------------------------------------------------------------------------
def poll_loop(base, interval, ui, seen):
    while not STOP_EVENT.is_set():
        try:
            m = http_json(f"{base}/api/system/metrics").get("metrics") or {}
            alerts = (http_json(f"{base}/api/alerts") or {}).get("alerts") or []
            cur = list(collect(m))
            for a in alerts:
                if a.get("level") in ("critical", "warn"):
                    cur.append((f"alert-{a.get('title')}", 2 if a.get("level") == "critical" else 1,
                                a.get("title") or "快照保护异常"))
            keys = {c[0] for c in cur}
            for k in list(seen):
                if k not in keys:
                    seen.discard(k)  # 异常恢复后才允许复发提醒
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
                # 本机弹一次（之后只留右下角感叹号，不重复弹）
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
                ui.add_unread(fresh[0][0], text)
                print("[提醒]", text)
        except Exception as e:
            print("轮询失败（下次重试）：", e)
        # 分片睡眠：停止指令秒级生效
        for _ in range(max(1, interval // 2)):
            if STOP_EVENT.is_set():
                break
            time.sleep(2)


# --------------------------------------------------------------------------
# 协议入口：网页设置页一键启停
# --------------------------------------------------------------------------
def handle_protocol(raw):
    action = (raw or "").split("://", 1)[-1].strip("/").lower()
    if action == "stop":
        if agent_stop_remote():
            remove_autostart()
            notify("NAS Safe 小助手已停止", "不再后台守护；可在网页设置里重新开启")
        else:
            remove_autostart()
        return
    if agent_online():
        notify("NAS Safe 小助手已在运行", "无需重复启动")
        return
    base = load_config().get("nas") or ""
    if not base:
        cands = discover_nas()
        base = pick_dialog(cands) or ""
    if not base:
        return
    interval = int(load_config().get("interval") or DEFAULT_INTERVAL)
    save_config({"nas": base, "interval": interval})
    ensure_autostart(base, interval)
    register_protocol()
    run_agent(base, interval, first=True)


def run_agent(base, interval, first=False, once=False, no_ui=False):
    ui = AgentUI(base)
    use_ui = (not no_ui) and ui.available()
    start_control_server()
    seen = set()
    print(f"NAS Safe 小助手已启动：{base}（每 {interval}s 检查一次）")
    if once:
        poll_once(base, ui, seen)
        return
    if first and not use_ui:
        notify("NAS Safe 小助手已启动", f"正在守护 {base}，异常会在这里提醒你")
    if not use_ui:
        poll_loop(base, interval, ui, seen)
        return
    # 有 UI：轮询放后台线程，主线程跑右下角图标
    threading.Thread(target=poll_loop, args=(base, interval, ui, seen), daemon=True).start()
    ui.run()


def poll_once(base, ui, seen):
    try:
        m = http_json(f"{base}/api/system/metrics").get("metrics") or {}
        cur = collect(m)
        print("检测到异常：", cur if cur else "无")
    except Exception as e:
        print("检测失败：", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nas", default="", help="NAS Safe 地址（留空则自动探测）")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="轮询间隔（秒）")
    ap.add_argument("--install", action="store_true", help="一键安装：探测 NAS + 弹窗确认 + 开机自启")
    ap.add_argument("--protocol", default="", help=argparse.SUPPRESS)
    ap.add_argument("--no-ui", action="store_true", help="纯后台模式，不显示右下角图标")
    ap.add_argument("--once", action="store_true", help="只检测一次（调试用）")
    args = ap.parse_args()

    if args.protocol:
        handle_protocol(args.protocol)
        return

    if agent_online():
        print("已有小助手在运行（如需重启，请在网页设置里先关闭再开启）。")
        return

    cfg = load_config()
    base = args.nas.rstrip("/") or (cfg.get("nas") or "")
    first_run = not base
    if not base:
        print("正在探测局域网内的 NAS Safe…")
        cands = discover_nas(extra=[args.nas] if args.nas else None)
        base = (cands[0] if len(cands) == 1 else pick_dialog(cands)) or ""
    if not base:
        print("未选择 NAS 地址，退出。")
        return

    interval = args.interval or int(cfg.get("interval") or DEFAULT_INTERVAL)
    if args.install or first_run:
        save_config({"nas": base, "interval": interval})
        ensure_autostart(base, interval)
        register_protocol()
    run_agent(base, interval, first=args.install or first_run,
              once=args.once, no_ui=args.no_ui)


if __name__ == "__main__":
    main()
