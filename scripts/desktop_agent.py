#!/usr/bin/env python3
"""NAS Safe 桌面小助手（Windows）

定位：**电脑端的提醒全部由它负责**，网页端不再需要弹窗授权。
平时缩在屏幕右下角（一个小盾牌图标，几乎不占资源），只在 NAS 出现异常
或保护状态变动时主动弹一次 Windows 通知；通知会自动消失。

提醒策略（按用户要求）：
  · 桌面端与微信端**同步发**：小助手在线时，本机弹窗与微信/邮件同一时刻发出；
    不判断用户是否坐在电脑前（判断空闲既易误判、又要常驻检测，不划算）
  · 小助手被用户关闭：自动上报离线，改由 NAS 服务端看门狗继续发微信/邮件，提醒不丢
  · 常驻形态：系统托盘图标（右下角通知区域）—— 绿色盾牌（正常）/ 红色盾牌+白色感叹号（有未读），悬停显示「NAS Safe 桌面助手 · 快照保护中」；
    有未读告警时蓝色盾牌中央出现红色感叹号，左键看未读、右键菜单可全部已读或退出

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
import atexit
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NAS-Safe-Agent"}
APP_NAME = "NAS Safe 桌面助手"      # 显示名（托盘提示 / 通知 / 注册表）
APP_DIR = "NAS Safe 桌面助手"       # 安装目录名（%APPDATA% 下）
APP_EXE = "桌面助手.exe"            # 主程序文件名
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # 调 PowerShell 不闪黑框
DEFAULT_INTERVAL = 120
CTRL_PORT = 18765          # 本机控制端口（只监听 127.0.0.1，不外泄）
PROTOCOL = "nassafe-agent"  # 浏览器拉起本机小助手的自定义协议
# 旧版残留识别标记（用于清理自启项/目录时不限旧版名，避免"杀不净"）
_LEGACY_MARKERS = ["桌面助手.exe", "NASSafeAgent", "NAS Safe 桌面助手",
                   "nassafe-agent", "desktop_agent"]
STOP_EVENT = threading.Event()
LOCK = threading.Lock()
AGENT_VER = "1.0.7.22"

# 托盘单例（通知气球用）
_TRAY = None


def _res_path(name):
    """取资源文件路径：PyInstaller 单文件运行时从临时目录取，开发时从脚本目录取。"""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", ""), name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)



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
# Windows 通知：优先走托盘气球（不调用 PowerShell，避免安全软件拦截）
# --------------------------------------------------------------------------
def _console_url(path: str = "") -> str:
    """取总控台地址（配置里保存的 NAS / 总控台），没有就返回空串。"""
    try:
        cfg = load_config() or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    base = str(cfg.get("nas") or cfg.get("base") or "").strip().rstrip("/")
    return (base + path) if base else ""


def _open_inbox_window():
    """点击气泡（或托盘）时打开未读提醒。

    历史坑（务必不要再改回去）：这里原来先走 tkinter 本地弹窗，但 tkinter 在
    Windows 上**只能在该进程创建解释器的主线程**使用，而本函数运行在气泡的 Win32
    消息线程里 → 必然抛 RuntimeError；外面又是 except: pass，用户看到的现象是
    「点了气泡毫无反应」，日志里只会多出一条空的「未读提醒：」气泡
    （tkinter 失败后的兜底又弹了一个 toast）。代码注释里早就写明 tkinter 建窗不可
    靠，但当时只把「气泡」换成了 Win32 自绘，**未读窗口还留在 tkinter 上**。

    现在：优先用浏览器打开控制台的告警页（任何环境都有效）→ tkinter 次选 →
    都不行才用消息框兜底；每一步都写 agent.log，便于事后定位。
    """
    def _ref():
        if _TRAY is not None:
            _TRAY.update(len(load_unread()))

    url = _console_url("/#/alerts")
    if url:
        try:
            import webbrowser
            webbrowser.open(url)
            agent_log("open_inbox: 已用浏览器打开 %s" % url)
            return
        except Exception as exc:  # noqa: BLE001
            agent_log("open_inbox: 浏览器打开失败：%s" % exc)

    try:
        show_inbox_window(_ref)
        agent_log("open_inbox: 已请求本地未读窗口")
        return
    except Exception as exc:  # noqa: BLE001
        agent_log("open_inbox: 本地窗口打开失败：%s（改用消息框兜底）" % exc)

    items = load_unread()
    text = "\n".join("· " + str(i.get("text", "")) for i in items[:10]) or "目前没有未读提醒"
    try:
        ToastManager._fallback("未读提醒", text)
    except Exception:  # noqa: BLE001
        pass


def _fire_toast_callback(manager, toast_hwnd):
    """执行气泡点击回调；回调完成后让气泡线程自毁。

    必须先跑回调再关窗：浏览器标签页打开需要 1~2 秒，先关窗用户会以为
    「点了闪退、什么都没发生」。DestroyWindow 只能由拥有窗口的线程调用，
    回调在独立线程里跑，所以完成后用 PostMessage WM_TIMER 让气泡线程自己关。
    """
    cb = manager._click.pop(int(toast_hwnd), None)
    if not cb:
        try:
            ctypes.windll.user32.DestroyWindow(int(toast_hwnd))
        except Exception:
            pass
        return

    def _run():
        try:
            cb()
        except Exception:
            pass
        finally:
            try:
                ctypes.windll.user32.PostMessageW(int(toast_hwnd), 0x8002, 0, 0)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def notify(title, text, level="info"):
    """弹出提醒：使用 Win32 自绘 Toast 弹窗（必现、样式可控、不依赖系统通知权限），
    不再调用系统托盘气球通知——Windows 原生通知边框/字体不可控，用户反馈样式难看。

    返回 channel 字符串，供 HTTP 接口如实反馈：
      "toast"     = Win32 自定义弹窗已显示
      "fallback"  = 回退到系统消息框
      "error"     = 弹窗与兜底都失败
    """
    try:
        status = _TOAST.show(title, text, on_click=_open_inbox_window, level=level)
    except Exception:
        try:
            ToastManager._fallback(title, text)
        except Exception:
            pass
        status = "error"
    agent_log(f"notify: [{level}] {title} | {text} -> channel={status}")
    return status


# --------------------------------------------------------------------------
# 自定义 Toast 弹窗（右下角、必现、可点击；ctypes / Win32 自绘，与系统托盘同源）
#
# 为什么弃用 tkinter：实测 tkinter 在某些运行环境下子线程建窗弹窗不可见，
# 且原实现把整段窗口创建包在 except:pass 里，失败时既不报错也不兜底，表现就是
# "收到提醒但没气泡"。这里改用与系统托盘同一套 Win32 机制（托盘图标已验证可见），
# 由专用后台线程持有消息循环，其它线程（HTTP 控制线程 / 轮询线程）通过 PostMessage
# 线程安全地请求显示；弹窗初始化失败时回退系统 MessageBoxW，保证"至少有提醒"。
# --------------------------------------------------------------------------
_TOAST_BG = 0xFFFFFF       # 白色卡片底（BGR=RGB）
_TOAST_TITLE = 0xC78402    # #0284c7 标题深蓝 -> BGR
_TOAST_ACCENT = 0xF8BD38   # #38bdf8 -> BGR
_TOAST_FG = 0x554133       # #334155 正文深灰 -> BGR
_TOAST_HINT = 0xB8A394     # #94a3b8 灰色小字 CTA -> BGR
_TOAST_BAR_WARN = 0x0B9EF5  # #f59e0b 橙色强调条 -> BGR
_TOAST_BAR_ERR = 0x4444EF   # #ef4444 红色强调条 -> BGR


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", ctypes.c_void_p), ("fErase", ctypes.c_int32),
                ("rcPaint", _RECT), ("fRestore", ctypes.c_int32),
                ("fIncUpdate", ctypes.c_int32), ("rgbReserved", ctypes.c_ubyte * 32)]


class _LOGFONTW(ctypes.Structure):
    _fields_ = [("lfHeight", ctypes.c_long), ("lfWidth", ctypes.c_long),
                ("lfEscapement", ctypes.c_long), ("lfOrientation", ctypes.c_long),
                ("lfWeight", ctypes.c_long), ("lfItalic", ctypes.c_byte),
                ("lfUnderline", ctypes.c_byte), ("lfStrikeOut", ctypes.c_byte),
                ("lfCharSet", ctypes.c_byte), ("lfOutPrecision", ctypes.c_byte),
                ("lfClipPrecision", ctypes.c_byte), ("lfQuality", ctypes.c_byte),
                ("lfPitchAndFamily", ctypes.c_byte), ("lfFaceName", ctypes.c_wchar * 32)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint32), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int32), ("cbWndExtra", ctypes.c_int32),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("pt", ctypes.c_long * 2)]


class ToastManager:
    WM_TOAST_SHOW = 0x0400 + 10
    WS_POPUP = 0x80000000
    WS_EX_TOPMOST = 0x00000008
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000

    def __init__(self):
        self.host_hwnd = None
        self.thread = None
        self._failed = False
        self._pending = {}
        self._pid = 0
        self._lock = threading.Lock()
        self._live = []                 # 当前可见 toast 的 hwnd（右下角堆叠定位用）
        self._text = {}                 # hwnd -> (title, body)
        self._click = {}                 # hwnd -> on_click 回调
        self._wndproc = None
        self._hinst = None
        self._font = None
        self._font_title = None
        self._font_body = None
        self._font_hint = None
        self._bg_brush = None
        self._bar_brush = None
        self._colors = {}

    # ---- 公开：启动后台线程（持有 Win32 消息循环）----
    def start(self, timeout=6.0):
        if os.name != "nt":
            return False
        self.thread = threading.Thread(target=self._run, daemon=True, name="nassafe-toast")
        self.thread.start()
        waited = 0.0
        while waited < timeout and not self.host_hwnd and not self._failed:
            time.sleep(0.1)
            waited += 0.1
        return bool(self.host_hwnd)

    # ---- 公开：显示一条 toast；返回 "toast"（已显示）/ "fallback"（已回退系统弹窗）----
    def show(self, title, body, on_click=None, timeout_ms=6000, level="info"):
        if not self.host_hwnd:
            self._fallback(title, body)
            return "fallback"
        bar_cls_name = ("NASSafeToastBarWarn" if level == "warn"
                        else "NASSafeToastBarErr" if level in ("error", "critical")
                        else "NASSafeToastBar")
        with self._lock:
            self._pid += 1
            pid = self._pid
            self._pending[pid] = (title, body, on_click, timeout_ms, bar_cls_name)
        try:
            ctypes.windll.user32.PostMessageW(self.host_hwnd, self.WM_TOAST_SHOW, pid, 0)
            return "toast"
        except Exception:
            self._fallback(title, body)
            return "fallback"

    @staticmethod
    def _fallback(title, body):
        try:
            ctypes.windll.user32.MessageBoxW(None, str(body), str(title),
                                            0x00000040 | 0x00001000)
        except Exception:
            pass

    # ---- 后台线程：Win32 消息循环 ----
    def _run(self):
        try:
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            g32 = ctypes.windll.gdi32

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t)
            u32.RegisterClassW.argtypes = [ctypes.c_void_p]
            u32.RegisterClassW.restype = ctypes.c_uint16
            u32.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                            ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            u32.CreateWindowExW.restype = ctypes.c_void_p
            u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u32.ShowWindow.restype = ctypes.c_int
            u32.DestroyWindow.argtypes = [ctypes.c_void_p]
            u32.DestroyWindow.restype = ctypes.c_int
            u32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_size_t, ctypes.c_ssize_t]
            u32.DefWindowProcW.restype = ctypes.c_ssize_t
            u32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_uint32, ctypes.c_uint32]
            u32.GetMessageW.restype = ctypes.c_ssize_t
            u32.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            u32.SetWindowTextW.restype = ctypes.c_int
            u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_size_t, ctypes.c_ssize_t]
            u32.SendMessageW.restype = ctypes.c_ssize_t
            u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_uint32]
            u32.SetWindowPos.restype = ctypes.c_int
            u32.BeginPaint.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            u32.BeginPaint.restype = ctypes.c_void_p
            u32.EndPaint.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            u32.EndPaint.restype = ctypes.c_int
            u32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            u32.GetClientRect.restype = ctypes.c_int
            u32.GetSystemMetrics.argtypes = [ctypes.c_int]
            u32.GetSystemMetrics.restype = ctypes.c_int
            u32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
            u32.SetTimer.restype = ctypes.c_size_t
            u32.PostQuitMessage.argtypes = [ctypes.c_int]
            u32.PostQuitMessage.restype = None
            u32.GetParent.argtypes = [ctypes.c_void_p]
            u32.GetParent.restype = ctypes.c_void_p
            g32.CreateSolidBrush.argtypes = [ctypes.c_uint32]
            g32.CreateSolidBrush.restype = ctypes.c_void_p
            g32.DeleteObject.argtypes = [ctypes.c_void_p]
            g32.DeleteObject.restype = ctypes.c_int
            g32.CreateFontIndirectW.argtypes = [ctypes.c_void_p]
            g32.CreateFontIndirectW.restype = ctypes.c_void_p
            g32.SetTextColor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            g32.SetTextColor.restype = ctypes.c_uint32
            g32.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
            g32.SetBkMode.restype = ctypes.c_int

            # 微软雅黑字体：标题加粗、正文常规，保证中文清晰可见
            self._hinst = k32.GetModuleHandleW(None)

            lf_title = _LOGFONTW()
            lf_title.lfHeight = -15
            lf_title.lfWeight = 500
            lf_title.lfCharSet = 134          # GB2312_CHARSET
            lf_title.lfQuality = 5            # CLEARTYPE_QUALITY：ClearType 锐化
            lf_title.lfFaceName = "Microsoft YaHei UI"
            self._font_title = g32.CreateFontIndirectW(ctypes.byref(lf_title))

            lf_body = _LOGFONTW()
            lf_body.lfHeight = -13
            lf_body.lfWeight = 400
            lf_body.lfCharSet = 134
            lf_body.lfQuality = 5             # CLEARTYPE_QUALITY：ClearType 锐化
            lf_body.lfFaceName = "Microsoft YaHei UI"
            self._font_body = g32.CreateFontIndirectW(ctypes.byref(lf_body))

            lf_hint = _LOGFONTW()
            lf_hint.lfHeight = -11
            lf_hint.lfWeight = 400
            lf_hint.lfCharSet = 134
            lf_hint.lfQuality = 5             # CLEARTYPE_QUALITY
            lf_hint.lfFaceName = "Microsoft YaHei UI"
            self._font_hint = g32.CreateFontIndirectW(ctypes.byref(lf_hint))

            # 卡片底色 + 强调条底色：用窗口类背景刷，由系统自绘，
            # 完全绕开 FillRect/BeginPaint/DrawTextW 等受限 GDI 绘制函数。
            self._bg_brush = g32.CreateSolidBrush(_TOAST_BG)
            self._bar_brush = g32.CreateSolidBrush(_TOAST_ACCENT)
            self._colors = {}

            manager = self

            def _wndproc(hwnd, msg, wparam, lparam):
                try:
                    if hwnd == manager.host_hwnd:
                        if msg == manager.WM_TOAST_SHOW:
                            item = manager._pending.pop(int(wparam), None)
                            if item:
                                manager._create_popup(item)
                            return 0
                        if msg == 0x0002:   # WM_DESTROY
                            u32.PostQuitMessage(0)
                            return 0
                        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)
                    # 以下为 toast 弹窗
                    if msg == 0x0138:       # WM_CTLCOLORSTATIC：给子文本控件上色（0x0133 是 WM_CTLCOLOREDIT，Static 收不到！）
                        color = manager._colors.get(int(lparam), _TOAST_FG)
                        try:
                            g32.SetBkMode(int(wparam), 1)      # TRANSPARENT：去字形背景块
                            g32.SetTextColor(int(wparam), color)
                        except Exception:
                            pass
                        return int(manager._bg_brush)
                    if msg == 0x0111:       # WM_COMMAND：SS_NOTIFY 的 Static 子控件被点击（STN_CLICKED）
                        # 历史坑：卡片表面铺满 Static（图标/标题/正文/「点击查看详情」），
                        # 不带 SS_NOTIFY 时系统 Static 会吞掉鼠标消息，点击文字全无反应，
                        # 只有点到文字间空白才碰巧触发——用户体感「气泡点击详情无效」。
                        # lparam 是控件 hwnd（64 位），& 0xFFFFFFFF 会截断 → GetParent 拿错句柄
                        # 历史坑2：先 DestroyWindow 再开回调线程 → 气泡瞬间消失、浏览器
                        # 标签页 1~2 秒后才开（还常开在当前窗口后面），用户体感「点了闪退、
                        # 什么都没发生」。现在先执行回调，完成后由气泡线程自毁（PostMessage
                        # WM_TIMER；DestroyWindow 只能由拥有该窗口的线程调用）。
                        ctl = int(lparam) & 0xFFFFFFFFFFFFFFFF
                        if ctl:
                            parent = u32.GetParent(ctl)
                            target = int(parent) if parent and int(parent) in manager._click else None
                            if target is not None:
                                _fire_toast_callback(manager, target)
                        return 0
                    if msg == 0x0113:       # WM_TIMER：到时自动销毁
                        u32.DestroyWindow(hwnd)
                        return 0
                    if msg == 0x8002:       # WM_APP+2：回调执行完毕，气泡线程自毁
                        # 历史坑：用 PostMessage(WM_TIMER) 通知自毁不可靠——WM_TIMER
                        # 被 Windows 特殊对待（与 SetTimer 合成机制绑定），手工 post
                        # 的 WM_TIMER 不保证送进窗口过程。WM_APP 段是普通队列消息，
                        # DispatchMessageW 必达。
                        u32.DestroyWindow(hwnd)
                        return 0
                    if msg == 0x0201:       # WM_LBUTTONDOWN：点击触发回调（accent 条命中时映射到父卡片）
                        target = hwnd if hwnd in manager._click else u32.GetParent(hwnd)
                        target = int(target) if target else None
                        if target is not None and target in manager._click:
                            _fire_toast_callback(manager, target)
                        return 0
                    if msg == 0x0002:       # WM_DESTROY
                        manager._click.pop(hwnd, None)
                        if hwnd in manager._live:
                            manager._live.remove(hwnd)
                        return 0
                except Exception:
                    pass
                return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = WNDPROC(_wndproc)

            # 主卡片窗口类：背景刷（系统自动填充卡片）+ 投影
            cls = _WNDCLASSW()
            cls.style = 0x00020000          # CS_DROPSHADOW
            cls.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            cls.hInstance = self._hinst
            cls.hbrBackground = self._bg_brush
            cls.lpszClassName = "NASSafeToast"
            u32.RegisterClassW(ctypes.byref(cls))

            # 强调条窗口类：accent 背景刷
            bar_cls = _WNDCLASSW()
            bar_cls.style = 0
            bar_cls.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            bar_cls.hInstance = self._hinst
            bar_cls.hbrBackground = self._bar_brush
            bar_cls.lpszClassName = "NASSafeToastBar"
            u32.RegisterClassW(ctypes.byref(bar_cls))

            # warn/error 级别强调条窗口类（不同色，弹窗按级别选用）
            for cls_name, brush in (("NASSafeToastBarWarn",
                                     g32.CreateSolidBrush(_TOAST_BAR_WARN)),
                                    ("NASSafeToastBarErr",
                                     g32.CreateSolidBrush(_TOAST_BAR_ERR))):
                wc = _WNDCLASSW()
                wc.style = 0
                wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
                wc.hInstance = self._hinst
                wc.hbrBackground = brush
                wc.lpszClassName = cls_name
                u32.RegisterClassW(ctypes.byref(wc))

            host = u32.CreateWindowExW(0, "NASSafeToast", "NASSafeToastHost",
                                       self.WS_POPUP, -10000, -10000, 0, 0,
                                       None, None, self._hinst, None)
            if not host:
                self._failed = True
                return
            self.host_hwnd = host

            m = _MSG()
            while u32.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(m))
                u32.DispatchMessageW(ctypes.byref(m))
        except Exception as e:
            try:
                print("Toast 线程初始化失败：", e, file=sys.stderr)
            except Exception:
                pass
            self._failed = True

    def _create_popup(self, item):
        u32 = ctypes.windll.user32
        title, body, on_click, timeout_ms, bar_cls_name = item
        w, h = 380, 118
        try:
            sw = u32.GetSystemMetrics(0)   # SM_CXSCREEN
            sh = u32.GetSystemMetrics(1)   # SM_CYSCREEN
        except Exception:
            sw, sh = 1920, 1080
        with self._lock:
            seq = len(self._live)
        x = sw - w - 20
        y = sh - h - 20 - (seq % 4) * (h + 8)   # 右下角，底部往上堆叠，最多 4 条
        hwnd = u32.CreateWindowExW(
            self.WS_EX_TOPMOST | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE,
            "NASSafeToast", "", self.WS_POPUP,
            x, y, w, h, None, None, self._hinst, None)
        if not hwnd:
            self._fallback(title, body)
            return
        with self._lock:
            self._live.append(hwnd)
        self._click[hwnd] = on_click

        # 左侧强调条（accent 色块，系统自绘）
        try:
            bar = u32.CreateWindowExW(0, bar_cls_name, "",
                                      0x40000000,        # WS_CHILD
                                      0, 0, 4, h, hwnd, None, self._hinst, None)
            if bar:
                u32.ShowWindow(bar, 5)
        except Exception:
            bar = None

        # 产品图标（盾牌 ICO；加载失败则文字整体左移兜底）
        tx, tw = 16, w - 32
        try:
            # onefile 的 _MEIPASS 不含 ICO，优先从 exe 同目录（安装目录）取
            exe_dir = os.path.dirname(os.path.abspath(
                sys.executable if getattr(sys, "frozen", False) else __file__))
            ico_path = os.path.join(exe_dir, "nassafe_agent.ico")
            if not os.path.isfile(ico_path):
                ico_path = _res_path("nassafe_agent.ico")
            if os.path.isfile(ico_path):
                hicon = u32.LoadImageW(None, ico_path, 1, 32, 32, 0x00000010)  # IMAGE_ICON|LR_LOADFROMFILE
                if hicon:
                    tx, tw = 58, w - 58 - 16
                    icon_w = u32.CreateWindowExW(
                        0, "Static", "",
                        0x40000000 | 0x10000000 | 0x00000003 | 0x00000100,   # WS_CHILD|WS_VISIBLE|SS_ICON|SS_NOTIFY
                        16, 16, 32, 32, hwnd, None, self._hinst, None)
                    if icon_w:
                        u32.SendMessageW(icon_w, 0x0172, 1, hicon)   # STM_SETIMAGE
        except Exception:
            pass

        # 标题；必须带 WS_VISIBLE，否则子控件永久隐藏（曾致气泡只有底板没文字）
        title_w = u32.CreateWindowExW(
            0, "Static", "",
            0x40000000 | 0x10000000 | 0x00000100,       # WS_CHILD|WS_VISIBLE|SS_NOTIFY
            tx, 14, tw, 24, hwnd, None, self._hinst, None)
        if title_w:
            self._colors[int(title_w)] = _TOAST_TITLE
            u32.SendMessageW(title_w, 0x0030, self._font_title, 1)   # WM_SETFONT
            u32.SetWindowTextW(title_w, str(title))

        # 正文（常规浅色，自动换行）；同样必须 WS_VISIBLE
        body_w = u32.CreateWindowExW(
            0, "Static", "",
            0x40000000 | 0x10000000 | 0x00000020 | 0x00000080 | 0x00000100,   # WS_CHILD|WS_VISIBLE|SS_WORDBREAK|SS_NOPREFIX|SS_NOTIFY
            tx, 42, tw, h - 42 - 30, hwnd, None, self._hinst, None)
        if body_w:
            self._colors[int(body_w)] = _TOAST_FG
            u32.SendMessageW(body_w, 0x0030, self._font_body, 1)
            u32.SetWindowTextW(body_w, str(body))

        # 灰色小字 CTA（视觉降级，衬托正文）
        try:
            hint_w = u32.CreateWindowExW(
                0, "Static", "",
                0x40000000 | 0x10000000 | 0x00000100,   # WS_CHILD|WS_VISIBLE|SS_NOTIFY
                tx, h - 24, tw, 16, hwnd, None, self._hinst, None)
            if hint_w:
                self._colors[int(hint_w)] = _TOAST_HINT
                u32.SendMessageW(hint_w, 0x0030, self._font_hint, 1)
                u32.SetWindowTextW(hint_w, "点击查看详情 ›")
        except Exception:
            pass

        try:
            # Win11 圆角（DWMWA_WINDOW_CORNER_PREFERENCE=33, DWMWCP_ROUND=2）；Win10 忽略
            try:
                pref = ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
            except Exception:
                pass
            # 显式钉到右下角、置顶、不抢焦点
            u32.SetWindowPos(hwnd, -1, x, y, w, h,
                             0x0010 | 0x0040)           # SWP_NOACTIVATE | SWP_SHOWWINDOW
            u32.ShowWindow(hwnd, 5)                     # SW_SHOW
            u32.SetTimer(hwnd, 1, int(timeout_ms), None)
        except Exception:
            pass


_TOAST = ToastManager()



# --------------------------------------------------------------------------
# 配置与未读消息持久化：%APPDATA%\NASSafeAgent\
# --------------------------------------------------------------------------
def config_dir():
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    return os.path.join(base or os.path.expanduser("~"), APP_DIR)


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


def agent_log(msg):
    """把关键事件写进安装目录的 agent.log，便于无界面环境下排查（如弹窗为何没出现）。"""
    try:
        d = config_dir()
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        with open(os.path.join(d, "agent.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S") + " " + str(msg) + "\n")
    except Exception:
        pass


def bundled_nas() -> str:
    """读取安装包自带的 NAS 地址（与 exe 同目录的 config.json）。

    从 NAS 网页下载的安装包里会预置这个地址，用户双击后直接安装，不用手动选。
    """
    try:
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(base_dir, "config.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            nas = str(cfg.get("nas") or "").strip()
            if nas:
                return nas.rstrip("/")
    except Exception:
        pass
    return ""


def load_unread():
    return load_config().get("unread") or []


def save_unread(items):
    cfg = load_config()
    cfg["unread"] = items[-50:]  # 最多保留 50 条
    save_config(cfg)


def push_unread(key, text):
    """去重追加一条未读提醒并落盘，返回最新条数（供托盘/小窗/收件箱共用）。"""
    with LOCK:
        items = load_unread()
        if any(i.get("key") == key for i in items):
            return len(items)
        items.append({"key": key, "text": text, "ts": time.strftime("%m-%d %H:%M")})
        save_unread(items)
        return len(items)


# --------------------------------------------------------------------------
# 本机控制服务（网页设置页：/ping 检测在线，/stop 请求退出）
# --------------------------------------------------------------------------
class _CtrlHandler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/notify":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                msg = json.loads(body)
                title = str(msg.get("title") or "NAS Safe 提醒")
                detail = str(msg.get("detail") or "")
                level = str(msg.get("level") or "info")
                # 气泡只显示标题 + 简短预览 + 点击查看，完整内容落未读界面
                preview = (detail or title)
                if len(preview) > 46:
                    preview = preview[:45] + "…"
                bubble = preview
                channel = notify(title, bubble, level)
                if _TRAY:
                    key = str(msg.get("key") or f"manual-{time.time()}")
                    _TRAY.add_unread(key, detail or title)
                out = json.dumps({"ok": True, "channel": channel}).encode()
            except Exception as e:
                out = json.dumps({"ok": False, "error": str(e)}).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ping":
            body = json.dumps({"ok": True, "agent": "nassafe", "ver": AGENT_VER,
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


def terminate_existing_agents():
    """强制结束所有正在运行的小助手进程（安装升级前调用，避免文件占用 / 端口占用）。"""
    if os.name != "nt":
        return
    # 旧版可能叫 NASSafeAgent.exe，新版叫 桌面助手.exe；安装包也可能出现 NASSafeAgent
    names = ["桌面助手.exe", "NASSafeAgent.exe", "desktop_agent.py"]
    killed = False
    for name in names:
        try:
            # taskkill /F /IM 在 Windows cmd 下可用；失败不阻断
            r = subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                killed = True
        except Exception:
            pass
    if not killed:
        return
    # 等进程真正退出，避免复制文件时仍被占用
    for _ in range(30):
        still = False
        for name in names:
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}"],
                    capture_output=True, timeout=5,
                    creationflags=CREATE_NO_WINDOW,
                )
                if name in r.stdout.decode("gbk", "replace"):
                    still = True
                    break
            except Exception:
                pass
        if not still:
            break
        time.sleep(0.3)
    # 额外等待 18765 端口释放，避免新版启动时因端口占用直接退出
    for _ in range(20):
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", CTRL_PORT)) != 0:
                s.close()
                break
            s.close()
        except Exception:
            break
        time.sleep(0.3)


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


def ensure_autostart(base, interval, exe=None, token=""):
    if os.name != "nt":
        return False
    try:
        import winreg
        cmd = (f'"{exe}"' if exe else _self_cmd()) + f' --nas "{base}" --interval {interval}'
        if token:
            cmd += f' --token "{token}"'
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print("写开机自启失败：", e, file=sys.stderr)
        return False


def _scan_and_remove_autostart():
    """扫描 HKCU/HKLM 的开机自启项，删除所有指向本程序的项（按命令内容匹配，不限旧版名）。"""
    if os.name != "nt":
        return
    try:
        import winreg
    except Exception:
        return
    run_paths = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Run"),
    ]
    for hkey, sub in run_paths:
        try:
            key = winreg.OpenKey(hkey, sub, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            vals = []
            i = 0
            while True:
                try:
                    nm, data, _ = winreg.EnumValue(key, i)
                    vals.append((nm, data))
                    i += 1
                except OSError:
                    break
            for nm, data in vals:
                if any(m in data for m in _LEGACY_MARKERS):
                    try:
                        winreg.DeleteValue(key, nm)
                        print("清理旧自启项：", sub, nm, file=sys.stderr)
                    except Exception:
                        pass
            winreg.CloseKey(key)
        except Exception:
            pass


def remove_autostart():
    """移除本程序注册的开机自启（并顺手清理所有旧版残留自启项）。"""
    _scan_and_remove_autostart()
    return True


def register_protocol(exe=None):
    if os.name != "nt":
        return False
    try:
        import winreg
        inner = (f'"{exe}"' if exe else _self_cmd()) + ' --protocol "%1"'
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


def _protocol_command_exe():
    """读取当前注册表 nassafe-agent 协议指向的 exe 路径；解析失败返回 None。"""
    if os.name != "nt":
        return None
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             rf"Software\Classes\{PROTOCOL}\shell\open\command")
        cmd, _ = winreg.QueryValueEx(key, "")
        winreg.CloseKey(key)
        # 命令形如："C:\...\桌面助手.exe" --protocol "%1"
        parts = cmd.split('"')
        if len(parts) >= 2:
            return parts[1]
    except Exception:
        pass
    return None


def repair_protocol():
    """如果注册表里的协议指向的不是当前 exe（比如被清理/重装后目录变化），自动重写为当前 exe。"""
    if os.name != "nt":
        return False
    try:
        exe = sys.executable
        registered = _protocol_command_exe()
        if registered and os.path.normcase(os.path.abspath(registered)) == os.path.normcase(os.path.abspath(exe)):
            return True  # 已经正确
        print(f"协议指向 {registered}，与当前 exe {exe} 不一致，重新注册…", file=sys.stderr)
        return register_protocol(exe=exe)
    except Exception as e:
        print("修复协议失败：", e, file=sys.stderr)
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


def install_dir() -> str:
    """小助手的固定安装位置（当前用户目录下，不需要管理员权限）。"""
    base = os.environ.get("APPDATA") if os.name == "nt" else None
    return os.path.join(base or os.path.expanduser("~"), APP_DIR)


def self_install_flow(base_hint="", token=""):
    """双击下载的 EXE 时：把自己安装到固定目录并启动守护。

    步骤：确定 NAS 地址 → 停掉旧实例 → 复制自身 → 写开机自启与协议 →
          显示「正在安装」进度 → 启动安装副本（缩到托盘）→ 自身退出。
    全程不需要管理员权限，也不需要用户解压或选择。
    """
    frozen = getattr(sys, "frozen", False)
    src = sys.executable if frozen else os.path.abspath(__file__)
    dst_dir = install_dir()
    dst = os.path.join(dst_dir, APP_EXE if frozen else "desktop_agent.py")
    if os.path.abspath(os.path.dirname(src)) == os.path.abspath(dst_dir):
        return None  # 已经是安装后的副本，正常守护即可

    # 1) NAS 地址：命令行 > 包内预置 > 已保存 > 探测向导
    base = (base_hint or bundled_nas() or (load_config().get("nas") or "")).rstrip("/")
    if not base:
        base = install_wizard(base_hint) or ""
    if not base:
        return None

    # 2) 强制结束所有正在运行的旧实例（旧版不会自己停，/stop 对旧版可能无效）
    terminate_existing_agents()

    # 2.5) 清理旧版安装目录与自启项（不限旧版名，避免"杀不净"反复复活）
    try:
        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        for nm in os.listdir(appdata):
            full = os.path.join(appdata, nm)
            if os.path.isdir(full) and any(m in nm for m in _LEGACY_MARKERS):
                # 当前安装目录（APP_DIR）会由下方复制覆盖，跳过删除
                if nm == APP_DIR:
                    continue
                shutil.rmtree(full, ignore_errors=True)
    except Exception:
        pass
    remove_autostart()      # 扫描清理所有旧版自启项

    # 3) 复制到固定目录
    try:
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, dst)
        # 同时复制图标资源，安装后运行能找到 ICO 文件
        for ico in ("nassafe_agent.ico", "nassafe_agent_alert.ico"):
            src_ico = _res_path(ico)
            if os.path.isfile(src_ico):
                try:
                    shutil.copy2(src_ico, os.path.join(dst_dir, ico))
                except Exception:
                    pass
    except Exception as e:
        print("复制到安装目录失败：", e, file=sys.stderr)
        notify("NAS Safe 安装未完成",
               "无法写入安装目录，请先退出正在运行的小助手后重新双击")
        return False

    # 4) 写配置 / 开机自启 / 协议（都指向安装后的副本）
    cfg0 = load_config()
    interval = int(cfg0.get("interval") or DEFAULT_INTERVAL)
    cfg0.update({"nas": base, "interval": interval})
    if token:
        cfg0["agent_token"] = token
    save_config(cfg0)
    ensure_autostart(base, interval, exe=dst, token=token)
    register_protocol(exe=dst)

    # 5) 进度窗口 + 启动副本，自身退出
    install_progress(base)
    try:
        cmd_extra = [dst, "--nas", base, "--interval", str(interval)]
        if token:
            cmd_extra += ["--token", token]
        subprocess.Popen(cmd_extra, close_fds=True, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        print("启动小助手失败：", e, file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------
# 现代化窗口基础组件（tkinter，无边框 + 自绘标题栏 + 主色按钮）
# --------------------------------------------------------------------------
FONT = "Microsoft YaHei UI"
ACCENT = "#2563eb"
BG = "#f6f8fc"
CARD = "#ffffff"
LINE = "#e6e9f0"
TEXT = "#111827"
TEXT2 = "#6b7280"


def _modern_window(title, w, h, accent=ACCENT):
    """无边框现代化窗口：返回 (root, body)。标题栏可拖动，右上角 × 关闭。"""
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.configure(bg=BG)
    root.resizable(False, False)
    try:
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
    except Exception:
        root.geometry(f"{w}x{h}")

    bar = tk.Frame(root, bg=accent, height=44)
    bar.pack(fill="x")
    bar.pack_propagate(False)
    tk.Label(bar, text="  " + title, bg=accent, fg="#ffffff",
             font=(FONT, 11, "bold")).pack(side="left")
    close = tk.Label(bar, text="  ✕  ", bg=accent, fg="#dbeafe",
                     font=(FONT, 11), cursor="hand2")
    close.pack(side="right")

    def _close(_e=None):
        try:
            root.destroy()
        except Exception:
            pass

    close.bind("<Button-1>", _close)
    close.bind("<Enter>", lambda e: close.configure(fg="#ffffff"))
    close.bind("<Leave>", lambda e: close.configure(fg="#dbeafe"))

    def _start(e):
        root._dx, root._dy = e.x, e.y

    def _move(e):
        try:
            root.geometry(f"+{root.winfo_x() + e.x - root._dx}+{root.winfo_y() + e.y - root._dy}")
        except Exception:
            pass

    bar.bind("<Button-1>", _start)
    bar.bind("<B1-Motion>", _move)
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True)
    return root, body


def _btn(parent, text, primary=True, width=None, cmd=None, state="normal"):
    import tkinter as tk
    b = tk.Button(
        parent, text=text, command=cmd, state=state,
        bg=ACCENT if primary else "#eef1f7",
        fg="#ffffff" if primary else TEXT,
        activebackground="#1d4ed8" if primary else "#e2e6ef",
        activeforeground="#ffffff" if primary else TEXT,
        relief="flat", bd=0, padx=16, pady=6, cursor="hand2",
        font=(FONT, 10, "bold" if primary else "normal"),
    )
    if width:
        b.configure(width=width)
    return b


def _empty_state(body, text, sub=""):
    import tkinter as tk
    box = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=LINE)
    box.pack(fill="both", expand=True, padx=18, pady=4)
    tk.Label(box, text="✓", bg=CARD, fg="#16a34a", font=(FONT, 26)).pack(pady=(46, 4))
    tk.Label(box, text=text, bg=CARD, fg=TEXT, font=(FONT, 12, "bold")).pack()
    if sub:
        tk.Label(box, text=sub, bg=CARD, fg=TEXT2, font=(FONT, 9)).pack(pady=(4, 0))


def install_progress(base):
    """自动安装：现代化进度窗口（动画进度条约 2.4 秒走满后自动关闭）。"""
    try:
        import tkinter as tk
    except Exception:
        return
    try:
        root, body = _modern_window("安装 NAS Safe 桌面助手", 460, 236)
        root.attributes("-topmost", True)

        tk.Label(body, text="正在安装 NAS Safe 桌面助手", bg=BG, fg=TEXT,
                 font=(FONT, 14, "bold")).pack(pady=(22, 4))
        st = tk.Label(body, text="准备中…", bg=BG, fg=ACCENT, font=(FONT, 10))
        st.pack()
        tk.Label(body, text=f"守护地址  {base}", bg=BG, fg=TEXT2,
                 font=(FONT, 9)).pack(pady=(4, 14))

        track = tk.Frame(body, bg=LINE, height=8)
        track.pack(fill="x", padx=26)
        track.pack_propagate(False)
        fill = tk.Frame(track, bg=ACCENT, width=0, height=8)
        fill.place(x=0, y=0, relheight=1.0, width=0)

        tk.Label(body, text="装好后自动缩到右下角托盘，只在异常时提醒",
                 bg=BG, fg=TEXT2, font=(FONT, 8)).pack(side="bottom", pady=12)

        total = 2100
        steps = 28
        w_full = 408

        def tick(i=0):
            p = min(1.0, i / steps)
            try:
                fill.place(width=int(w_full * p))
            except Exception:
                pass
            if i == 7:
                st.configure(text="正在写入安装目录…")
            elif i == 16:
                st.configure(text="正在设置开机自启…")
            elif i >= steps:
                st.configure(text="安装完成 ✓", fg="#16a34a")
                fill.configure(bg="#16a34a")
                root.after(900, root.destroy)
                return
            root.after(total // steps, lambda: tick(i + 1))

        root.after(250, lambda: tick(0))
        root.mainloop()
    except Exception:
        pass


def install_wizard(base_hint=""):
    """首次运行：正规安装窗口。

    先显示「正在安装 NAS Safe 助手…」，后台探测局域网里的 NAS Safe，
    探测完在同一窗口列出候选让用户确认，点「安装并开机自启」即完成。
    全程不需要管理员权限（只写当前用户的开机自启与协议）。
    """
    try:
        import tkinter as tk
    except Exception:
        return None
    result = {"url": None, "manual": None}

    root, body = _modern_window("安装 NAS Safe 桌面助手", 540, 366)
    root.attributes("-topmost", True)

    tk.Label(body, text="安装 NAS Safe 桌面助手", bg=BG, fg=TEXT,
             font=(FONT, 14, "bold")).pack(pady=(18, 3))
    status = tk.Label(body, text="正在查找局域网内的 NAS Safe…", bg=BG, fg=ACCENT,
                      font=(FONT, 10))
    status.pack()
    sub = tk.Label(body, text="", bg=BG, fg=TEXT2, font=(FONT, 9))
    sub.pack(pady=(2, 12))

    frame = tk.Frame(body, bg=BG)
    frame.pack(fill="both", expand=True, padx=20)
    var = tk.StringVar(value="")
    ent = {"box": None}

    foot = tk.Frame(body, bg=BG)
    foot.pack(fill="x", padx=20, pady=(6, 14))
    btn = _btn(foot, "安装并开机自启", primary=True, width=20, state="disabled")
    btn.pack(side="right")

    def finish(url):
        url = (url or "").strip()
        if url and not url.startswith("http"):
            url = "http://" + url
        result["url"] = url.rstrip("/") if url else None
        try:
            root.destroy()
        except Exception:
            pass

    def on_done(cands):
        status.configure(text="准备就绪", fg="#16a34a")
        if cands:
            status.configure(text="准备就绪", fg="#16a34a")
            sub.configure(text=f"找到 {len(cands)} 个 NAS Safe 服务，选择要守护的地址")
            card = tk.Frame(frame, bg=CARD, highlightthickness=1, highlightbackground=LINE)
            card.pack(fill="both", expand=True)
            for c in cands[:5]:
                row = tk.Frame(card, bg=CARD)
                row.pack(fill="x", padx=12, pady=2)
                tk.Radiobutton(row, text=c, variable=var, value=c, bg=CARD, fg=TEXT,
                               selectcolor="#eaf2ff", activebackground=CARD,
                               font=(FONT, 10), bd=0, highlightthickness=0).pack(anchor="w", pady=4)
            var.set(cands[0])
            btn.configure(state="normal", command=lambda: finish(var.get()))
        else:
            status.configure(text="没有自动找到", fg="#b45309")
            sub.configure(text="请手动填写 NAS 地址（例如 http://192.168.8.62:8848）")
            card = tk.Frame(frame, bg=CARD, highlightthickness=1, highlightbackground=LINE)
            card.pack(fill="both", expand=True)
            box = tk.Entry(card, width=44, relief="flat", bg=CARD, fg=TEXT,
                           font=(FONT, 10), highlightthickness=1,
                           highlightbackground=LINE, highlightcolor=ACCENT)
            box.pack(padx=14, pady=16)
            if base_hint:
                box.insert(0, base_hint)
            ent["box"] = box
            btn.configure(state="normal", command=lambda: finish(box.get().strip()))

    def probe():
        cands = []
        try:
            cands = discover_nas(extra=[base_hint] if base_hint else None)
        except Exception:
            cands = []
        try:
            root.after(0, lambda: on_done(cands))
        except Exception:
            pass

    threading.Thread(target=probe, daemon=True).start()
    root.mainloop()
    return result["url"]


# --------------------------------------------------------------------------
# 傻瓜式配对：无令牌但有 NAS 地址时，提示输入 6 位配对码换取永久令牌
# --------------------------------------------------------------------------

def ask_pairing_code(base):
    """配对码输入窗口：返回 6 位码或空字符串（取消/不可用）。"""
    try:
        import tkinter as tk
    except Exception:
        return ""
    out = {"code": ""}

    def build():
        root, body = _modern_window("配对 NAS Safe 设备", 460, 312)
        root.attributes("-topmost", True)
        tk.Label(body, text="输入配对码", bg=BG, fg=TEXT, font=(FONT, 14, "bold")).pack(pady=(16, 2))
        tk.Label(body, text="在控制台「功能设置 → 添加设备 → Windows 电脑」里\n点击「生成配对码」，把显示的 6 位数字填到这里",
                 bg=BG, fg=TEXT2, font=(FONT, 9), justify="center").pack(pady=(2, 8))
        box = tk.Entry(body, width=8, relief="flat", bg=CARD, fg=TEXT, font=(FONT, 24, "bold"),
                       justify="center", highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
        box.pack(pady=(2, 8))
        box.focus_set()
        err = tk.Label(body, text="", bg=BG, fg="#dc2626", font=(FONT, 9))
        err.pack()
        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", padx=20, pady=(4, 14))

        def submit():
            c = box.get().strip()
            if len(c) != 6 or not c.isdigit():
                err.configure(text="请输入 6 位数字")
                return
            out["code"] = c
            try:
                root.destroy()
            except Exception:
                pass

        btn = _btn(foot, "配对并连接", primary=True, width=18, command=submit)
        btn.pack(side="right")
        box.bind("<Return>", lambda e: submit())
        root.mainloop()

    try:
        build()
    except Exception:
        return ""
    return out["code"]


def pair_flow(base):
    """用配对码换取永久令牌并落盘（带 UI）。返回 token 或空。"""
    code = ask_pairing_code(base)
    if not code:
        return ""
    try:
        r = http_json(base.rstrip("/") + "/api/agent/pair", method="POST",
                      body={"code": code,
                            "hostname": os.environ.get("COMPUTERNAME") or socket.gethostname(),
                            "os": "Windows"})
    except Exception:
        return ""
    if r and r.get("ok"):
        tok = r.get("token")
        cfg = load_config()
        cfg["nas"] = base.rstrip("/")
        cfg["agent_token"] = tok
        save_config(cfg)
        ensure_autostart(base.rstrip("/"), int(cfg.get("interval") or DEFAULT_INTERVAL), token=tok)
        return tok
    return ""


# --------------------------------------------------------------------------
# 系统托盘图标（ctypes + Win32，纯标准库，零第三方依赖）
#
#   正常：产品蓝盾牌（与 NAS Safe 界面同色）；悬停提示「NAS Safe · 快照保护中」
#   告警：蓝色盾牌中央出现红色感叹号；悬停提示未读条数
#   左键：打开未读列表；右键：菜单（查看未读 / 全部已读 / 退出）
# --------------------------------------------------------------------------
class TrayIcon:
    WM_TRAY = 0x0400 + 1        # 托盘回调消息
    WM_UPDATE = 0x0400 + 2      # 主线程通知托盘线程刷新图标
    WM_BALLOON = 0x0400 + 3     # 主线程通知托盘线程弹出气球
    ID_OPEN, ID_READ_ALL, ID_QUIT = 1001, 1002, 1003
    ID_PAIR = 1004               # 右键菜单：输入配对码（把这台电脑纳管进总控台）
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIF_INFO = 0x00000010
    NIIF_INFO = 0x00000001
    NIIF_WARNING = 0x00000002
    NIIF_USER = 0x00000004           # 气泡用自定义图标
    NIIF_LARGE_ICON = 0x00000020     # 气泡里显示 32x32 大图标
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010

    def __init__(self, tip_normal="NAS Safe 桌面助手 · 快照保护中",
                 on_open=None, on_read_all=None, on_quit=None, on_pair=None):
        self.tip_normal = tip_normal
        self.on_open = on_open
        self.on_read_all = on_read_all
        self.on_quit = on_quit
        self.on_pair = on_pair
        self.hwnd = None
        self.thread = None
        self._failed = False
        self._wndproc = None

    # ---------------- 公共接口（其它线程调用） ----------------
    def start(self, timeout=6.0):
        if os.name != "nt":
            return False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        waited = 0.0
        while waited < timeout and not self.hwnd and not self._failed:
            time.sleep(0.1)
            waited += 0.1
        if self.hwnd:
            global _TRAY
            _TRAY = self
            return True
        return False

    def update(self, unread=0):
        """刷新图标与提示（未读数 > 0 显示红色感叹号）。"""
        if not self.hwnd:
            return
        try:
            ctypes.windll.user32.PostMessageW(self.hwnd, self.WM_UPDATE,
                                              ctypes.wintypes.WPARAM(int(unread)),
                                              ctypes.wintypes.LPARAM(0))
        except Exception:
            pass

    def stop(self):
        if not self.hwnd:
            return
        try:
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0002, 0, 0)  # WM_DESTROY
        except Exception:
            pass

    def balloon(self, title, body, level="info"):
        """从任意线程调用: 让托盘线程弹出一个气球提示 (自动消失, 进通知中心)."""
        if not self.hwnd:
            return False
        self._balloon_title = str(title)[:63]
        self._balloon_body = str(body)[:255]
        self._balloon_flags = self.NIIF_WARNING if level in ("warn", "critical") else self.NIIF_INFO
        try:
            ctypes.windll.user32.PostMessageW(self.hwnd, self.WM_BALLOON, 0, 0)
        except Exception:
            return False
        return True

    def add_unread(self, key, text):
        """保存未读并同步刷新图标（未读>0 时显示红感叹号）。"""
        n = push_unread(key, text)
        self.update(n)
        return n

    # ---------------- 托盘图标（优先读 ICO 资源，更清晰；失败回退自绘） ----------------
    @staticmethod
    def _load_hicon(alert: bool):
        """从同目录/打包资源里加载 ICO 图标 (16x16 用于托盘, 高 DPI 自动选帧)."""
        try:
            u32 = ctypes.windll.user32
            name = "nassafe_agent_alert.ico" if alert else "nassafe_agent.ico"
            path = _res_path(name)
            if not os.path.isfile(path):
                return None
            # 取系统建议的小图标尺寸 (通常是 16)
            size = u32.GetSystemMetrics(49) or 16  # SM_CXSMICON
            hicon = u32.LoadImageW(None, path, TrayIcon.IMAGE_ICON,
                                   size, size,
                                   TrayIcon.LR_LOADFROMFILE | 0x00008000)  # LR_SHARED
            return hicon or None
        except Exception:
            return None

    @staticmethod
    def _load_hicon_sized(alert: bool, size: int):
        """按指定尺寸加载 ICO（气泡提示用 32x32 大图标，与托盘同款图标文件）。"""
        try:
            u32 = ctypes.windll.user32
            name = "nassafe_agent_alert.ico" if alert else "nassafe_agent.ico"
            path = _res_path(name)
            if not os.path.isfile(path):
                return None
            hicon = u32.LoadImageW(None, path, TrayIcon.IMAGE_ICON,
                                   size, size,
                                   TrayIcon.LR_LOADFROMFILE)
            return hicon or None
        except Exception:
            return None

    @staticmethod
    def _make_hicon(alert: bool, size: int = 16):
        try:
            u32 = ctypes.windll.user32
            g32 = ctypes.windll.gdi32

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                            ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                            ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                            ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                            ("biClrImportant", ctypes.c_uint32)]

            class ICONINFO(ctypes.Structure):
                _fields_ = [("fIcon", ctypes.c_int32), ("xHotspot", ctypes.c_uint32),
                            ("yHotspot", ctypes.c_uint32), ("hbmMask", ctypes.c_void_p),
                            ("hbmColor", ctypes.c_void_p)]

            hdc = u32.GetDC(None)
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = size
            bmi.biHeight = -size          # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bits = ctypes.c_void_p()
            hbm = g32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            u32.ReleaseDC(None, hdc)
            if not hbm or not bits.value:
                return None

            buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
            cx = cy = (size - 1) / 2.0
            r_out = size / 2.0 - 0.8
            r_in = size * 0.30
            brand = (74, 141, 240)    # 正常：品牌蓝盾牌（与托盘图标同色系）
            red = (239, 68, 68)       # 告警底色
            white = (255, 255, 255)   # 感叹号
            for y in range(size):
                for x in range(size):
                    dx, dy = x - cx, y - cy
                    d = (dx * dx + dy * dy) ** 0.5
                    a = r_out - d + 0.5
                    if a <= 0:
                        continue
                    a = 1.0 if a > 1 else a
                    r, g, b = (red if alert else brand)
                    # 告警：红色盾牌中间画一个白色感叹号
                    if alert:
                        bw = max(2.5, size * 0.13)
                        if abs(dx) <= bw / 2 and (cy - size * 0.19) <= y <= (cy + size * 0.09):
                            r, g, b = white
                            a = 1.0
                        if abs(dx) <= bw / 2 and (cy + size * 0.15) <= y <= (cy + size * 0.24):
                            r, g, b = white
                            a = 1.0
                    # 盾牌轮廓（下缘两侧轻微收窄，看起来像盾不是圆）
                    o = (y - cy) / (size / 2.0)
                    if o > 0.35 and abs(dx) > (r_out - 1.2) * (1.0 - (o - 0.35) * 0.9):
                        continue
                    i = (y * size + x) * 4
                    alpha = int(round(a * 255))
                    # 预乘 alpha 的 BGRA
                    buf[i] = int(b * alpha / 255)
                    buf[i + 1] = int(g * alpha / 255)
                    buf[i + 2] = int(r * alpha / 255)
                    buf[i + 3] = alpha

            hmask = g32.CreateBitmap(size, size, 1, 1, None)
            info = ICONINFO()
            info.fIcon = 1
            info.xHotspot = 0
            info.yHotspot = 0
            info.hbmMask = hmask
            info.hbmColor = hbm
            return u32.CreateIconIndirect(ctypes.byref(info))
        except Exception:
            return None

    # ---------------- 窗口与消息循环（托盘线程内） ----------------
    def _run(self):
        try:
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            s32 = ctypes.windll.shell32
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", ctypes.c_uint32), ("lpfnWndProc", WNDPROC),
                            ("cbClsExtra", ctypes.c_int32), ("cbWndExtra", ctypes.c_int32),
                            ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                            ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                            ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]

            class NOTIFYICONDATAW(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", ctypes.c_void_p),
                            ("uID", ctypes.c_uint32), ("uFlags", ctypes.c_uint32),
                            ("uCallbackMessage", ctypes.c_uint32), ("hIcon", ctypes.c_void_p),
                            ("szTip", ctypes.c_wchar * 128), ("dwState", ctypes.c_uint32),
                            ("dwStateMask", ctypes.c_uint32),
                            ("szInfo", ctypes.c_wchar * 256), ("uTimeout", ctypes.c_uint32),
                            ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", ctypes.c_uint32),
                            ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", ctypes.c_void_p)]

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            class MSG(ctypes.Structure):
                _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
                            ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                            ("time", ctypes.c_uint32), ("pt", POINT)]

            s32.Shell_NotifyIconW.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            s32.Shell_NotifyIconW.restype = ctypes.c_int32
            u32.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                            ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            u32.CreateWindowExW.restype = ctypes.c_void_p
            u32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_size_t, ctypes.c_ssize_t]
            u32.DefWindowProcW.restype = ctypes.c_ssize_t

            hicon_ok = self._load_hicon(False) or self._make_hicon(False)
            hicon_alert = self._load_hicon(True) or self._make_hicon(True)
            self._icons = [hicon_ok, hicon_alert]
            # 气泡提示里显示的图标：跟托盘用同一个 ICO，只是取 32x32，
            # 不再出现「托盘是自家盾牌、气泡是 Windows 自带蓝 i / 黄 !」的割裂感
            self._balloon_icons = [self._load_hicon_sized(False, 32) or hicon_ok,
                                   self._load_hicon_sized(True, 32) or hicon_alert]

            def _wndproc(hwnd, msg, wparam, lparam):
                try:
                    if msg == self.WM_TRAY:
                        if lparam in (0x0202, 0x0203):      # 左键单击 / 双击
                            if self.on_open:
                                threading.Thread(target=self.on_open, daemon=True).start()
                        elif lparam == 0x0405:              # NIN_BALLOONUSERCLICK（点击气泡/通知中心消息）
                            if self.on_open:
                                threading.Thread(target=self.on_open, daemon=True).start()
                        elif lparam == 0x0205:              # 右键
                            self._popup_menu(hwnd)
                    elif msg == self.WM_UPDATE:
                        n = int(wparam)
                        nid = NOTIFYICONDATAW()
                        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                        nid.hWnd = hwnd
                        nid.uID = 1
                        nid.uFlags = self.NIF_ICON | self.NIF_TIP
                        nid.hIcon = self._icons[1] if n > 0 else self._icons[0]
                        nid.szTip = (self.tip_normal if n <= 0
                                     else f"NAS Safe 桌面助手 · {n} 条未读提醒")[:127]
                        s32.Shell_NotifyIconW(1, ctypes.byref(nid))   # NIM_MODIFY
                    elif msg == self.WM_BALLOON:
                        nid = NOTIFYICONDATAW()
                        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                        nid.hWnd = hwnd
                        nid.uID = 1
                        nid.uFlags = self.NIF_INFO
                        nid.szInfoTitle = getattr(self, "_balloon_title", "")[:63]
                        nid.szInfo = getattr(self, "_balloon_body", "")[:255]
                        nid.uTimeout = 12000
                        # 气泡图标统一用自家盾牌：告警用带红点那版，普通用常规版
                        _warn = getattr(self, "_balloon_flags", self.NIIF_INFO) == self.NIIF_WARNING
                        _bicon = (self._balloon_icons[1] if _warn else self._balloon_icons[0]) \
                            if getattr(self, "_balloon_icons", None) else None
                        if _bicon:
                            nid.dwInfoFlags = self.NIIF_USER | self.NIIF_LARGE_ICON
                            nid.hBalloonIcon = _bicon
                        else:
                            nid.dwInfoFlags = getattr(self, "_balloon_flags", self.NIIF_INFO)
                        s32.Shell_NotifyIconW(1, ctypes.byref(nid))   # NIM_MODIFY
                    elif msg == 0x0111:                     # WM_COMMAND
                        if wparam == self.ID_OPEN and self.on_open:
                            threading.Thread(target=self.on_open, daemon=True).start()
                        elif wparam == self.ID_READ_ALL and self.on_read_all:
                            self.on_read_all()
                        elif wparam == self.ID_PAIR and self.on_pair:
                            threading.Thread(target=self.on_pair, daemon=True).start()
                        elif wparam == self.ID_QUIT and self.on_quit:
                            self.on_quit()
                    elif msg == 0x0002:                     # WM_DESTROY
                        u32.PostQuitMessage(0)
                        return 0
                except Exception:
                    pass
                return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = WNDPROC(_wndproc)
            hinstance = k32.GetModuleHandleW(None)
            cls = WNDCLASSW()
            cls.lpfnWndProc = self._wndproc
            cls.hInstance = hinstance
            cls.lpszClassName = "NASSafeTrayWindow"
            if not u32.RegisterClassW(ctypes.byref(cls)):
                # 已注册过（重复运行）不算失败
                pass
            hwnd = u32.CreateWindowExW(0, "NASSafeTrayWindow", "NAS Safe", 0,
                                       0, 0, 0, 0, None, None, hinstance, None)
            if not hwnd:
                self._failed = True
                return
            self.hwnd = hwnd

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = 0x00000001 | self.NIF_ICON | self.NIF_TIP  # MESSAGE | ICON | TIP
            nid.uCallbackMessage = self.WM_TRAY
            nid.hIcon = self._icons[0]
            nid.szTip = self.tip_normal
            if not s32.Shell_NotifyIconW(0, ctypes.byref(nid)):   # NIM_ADD
                self._failed = True
                self.hwnd = None
                u32.DestroyWindow(hwnd)
                return

            msg = MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
            try:
                nid2 = NOTIFYICONDATAW()
                nid2.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid2.hWnd = hwnd
                nid2.uID = 1
                s32.Shell_NotifyIconW(2, ctypes.byref(nid2))   # NIM_DELETE
            except Exception:
                pass
        except Exception as e:
            print("托盘初始化失败：", e, file=sys.stderr)
            self._failed = True

    def _popup_menu(self, hwnd):
        try:
            u32 = ctypes.windll.user32

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            menu = u32.CreatePopupMenu()
            n = len(load_unread())
            u32.AppendMenuW(menu, 0x00000003, 0, "NAS Safe 桌面助手")
            u32.AppendMenuW(menu, 0x00000800, 0, None)
            u32.AppendMenuW(menu, 0x00000000, self.ID_OPEN, f"查看未读提醒（{n}）")
            u32.AppendMenuW(menu, 0x00000000, self.ID_READ_ALL, "全部标记已读")
            u32.AppendMenuW(menu, 0x00000000, self.ID_PAIR, "🔑 输入配对码…")
            u32.AppendMenuW(menu, 0x00000800, 0, None)          # MF_SEPARATOR
            u32.AppendMenuW(menu, 0x00000000, self.ID_QUIT, "退出小助手")
            u32.SetForegroundWindow(hwnd)
            # TPM_RETURNCMD(0x0100)：函数直接返回选中的菜单 ID，不发 WM_COMMAND
            cmd = u32.TrackPopupMenuEx(menu, 0x0100 | 0x0002, pt.x, pt.y, hwnd, None)
            u32.DestroyMenu(menu)
            u32.PostMessageW(hwnd, 0, 0, 0)   # 消除菜单残留
            if cmd == self.ID_OPEN and self.on_open:
                threading.Thread(target=self.on_open, daemon=True).start()
            elif cmd == self.ID_READ_ALL and self.on_read_all:
                self.on_read_all()
            elif cmd == self.ID_PAIR and self.on_pair:
                threading.Thread(target=self.on_pair, daemon=True).start()
            elif cmd == self.ID_QUIT and self.on_quit:
                self.on_quit()
        except Exception:
            pass


class NullUI:
    """无界面模式：只记录未读，不显示任何窗口。"""
    def add_unread(self, key, text):
        pass


class TraySink:
    """把未读变化同步到托盘图标（红感叹号 / 绿盾切换）。"""
    def __init__(self, tray):
        self.tray = tray

    def add_unread(self, key, text):
        n = push_unread(key, text)
        self.tray.update(n)


_INBOX_LOCK = threading.Lock()
_INBOX_THREAD = None


def show_inbox_window(on_change=None):
    """弹出未读提醒列表（tkinter，独立线程；点一条即标记已读）。"""
    global _INBOX_THREAD
    with _INBOX_LOCK:
        if _INBOX_THREAD and _INBOX_THREAD.is_alive():
            # 已有窗口在运行：发送一次"唤醒"请求，由线程内把窗口提到最前
            try:
                _INBOX_THREAD.raise_focus = True
            except Exception:
                pass
            return

    def _run():
        global _INBOX_THREAD
        with _INBOX_LOCK:
            _INBOX_THREAD = threading.current_thread()
            _INBOX_THREAD.raise_focus = False
        try:
            import tkinter as tk
        except Exception:
            notify(APP_NAME, "未读提醒：" + "；".join(
                i.get("text", "") for i in load_unread()[:3]))
            with _INBOX_LOCK:
                _INBOX_THREAD = None
            return
        try:
            root, body = _modern_window("NAS Safe 桌面助手 · 未读提醒", 580, 400)
            root.attributes("-topmost", True)

            hdr = tk.Frame(body, bg=BG)
            hdr.pack(fill="x", padx=20, pady=(16, 10))
            count_lbl = tk.Label(hdr, text="", bg=BG, fg=TEXT, font=(FONT, 13, "bold"))
            count_lbl.pack(side="left")
            tk.Label(hdr, text="点一条即标记为已读", bg=BG, fg=TEXT2,
                     font=(FONT, 9)).pack(side="left", padx=10)

            holder = tk.Frame(body, bg=BG)
            holder.pack(fill="both", expand=True, padx=20)

            lb = None

            def build_list():
                nonlocal lb
                for w in holder.winfo_children():
                    w.destroy()
                lb = None
                items = load_unread()
                count_lbl.configure(text=f"{len(items)} 条未读")
                if not items:
                    _empty_state(holder, "没有未读提醒", "一切正常，有异常会在这里告诉你")
                    return
                card = tk.Frame(holder, bg=CARD, highlightthickness=1, highlightbackground=LINE)
                card.pack(fill="both", expand=True)
                lb = tk.Listbox(card, bd=0, relief="flat", highlightthickness=0,
                                bg=CARD, fg=TEXT, font=(FONT, 10), activestyle="none",
                                selectbackground="#eaf2ff", selectforeground=TEXT,
                                selectborderwidth=0)
                sb = tk.Scrollbar(card, command=lb.yview, width=8, bd=0,
                                  troughcolor=CARD, activebackground="#c7d2e5")
                lb.configure(yscrollcommand=sb.set)
                lb.pack(side="left", fill="both", expand=True, padx=(10, 2), pady=10)
                sb.pack(side="right", fill="y", pady=10, padx=(0, 6))
                for it in items:
                    lb.insert("end", f"   {it.get('ts','')}    {it.get('text','')}")

                def on_pick(_e=None):
                    if not lb:
                        return
                    sel = lb.curselection()
                    if not sel:
                        return
                    arr = load_unread()
                    if sel[0] < len(arr):
                        arr.pop(sel[0])
                        save_unread(arr)
                        if on_change:
                            on_change()
                    build_list()

                lb.bind("<<ListboxSelect>>", on_pick)
                lb.bind("<Double-Button-1>", on_pick)

            build_list()

            foot = tk.Frame(body, bg=BG)
            foot.pack(fill="x", padx=20, pady=(8, 16))

            def read_all():
                save_unread([])
                if on_change:
                    on_change()
                build_list()

            _btn(foot, "全部标记已读", primary=False, cmd=read_all).pack(side="left")
            _btn(foot, "关闭", primary=True, cmd=root.destroy).pack(side="right")

            def _lift_if_asked():
                try:
                    cur = threading.current_thread()
                    if getattr(cur, "raise_focus", False):
                        cur.raise_focus = False
                        root.deiconify()
                        root.attributes("-topmost", True)
                        root.lift()
                        root.attributes("-topmost", True)
                except Exception:
                    pass
                if root.winfo_exists():
                    root.after(300, _lift_if_asked)

            root.after(300, _lift_if_asked)
            root.mainloop()
        except Exception as e:
            print("消息窗口失败：", e, file=sys.stderr)
        finally:
            with _INBOX_LOCK:
                _INBOX_THREAD = None

    threading.Thread(target=_run, daemon=True).start()


# --------------------------------------------------------------------------
# 右下角小窗（托盘不可用时的兜底；不进任务栏）
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
        try:
            self.badge.attributes("-toolwindow", True)  # 不进任务栏 / Alt-Tab
        except Exception:
            pass
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
        STOP_EVENT.set()  # 标记停止，进程退出时会上报离线
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
# 在线状态上报：NAS 端据此决定由谁发微信（助手离线时服务端看门狗接管）
# --------------------------------------------------------------------------
_LAST_REPORT = 0.0
_REPORTED_OFFLINE = False

def report_online(base, force=False):
    global _LAST_REPORT
    now = time.time()
    if not force and now - _LAST_REPORT < 60:
        return
    try:
        http_json(f"{base}/api/agent/status", method="POST", timeout=6,
                  body={"online": True, "host": socket.gethostname()})
        _LAST_REPORT = now
    except Exception:
        pass

def report_offline(base):
    global _REPORTED_OFFLINE
    if _REPORTED_OFFLINE or not base:
        return
    _REPORTED_OFFLINE = True
    try:
        http_json(f"{base}/api/agent/status", method="POST", timeout=6,
                  body={"online": False, "host": socket.gethostname()})
    except Exception:
        pass

# --------------------------------------------------------------------------
# 守护轮询（后台线程）
# --------------------------------------------------------------------------
def _fallback_anomalies(base):
    """老版本服务端没有 /api/anomalies 时，退回本地判定。"""
    m = http_json(f"{base}/api/system/metrics").get("metrics") or {}
    alerts = (http_json(f"{base}/api/alerts") or {}).get("alerts") or []
    out = [{"key": k, "sev": sev, "title": t} for k, sev, t in collect(m)]
    for a in alerts:
        if a.get("level") in ("critical", "warn"):
            out.append({"key": f"alert-{a.get('title')}",
                        "sev": 2 if a.get("level") == "critical" else 1,
                        "title": a.get("title") or "快照保护异常"})
    return out


def poll_loop(base, interval, ui, seen):
    while not STOP_EVENT.is_set():
        try:
            # 异常判定统一走服务端（网页端/小助手/看门狗共用同一套规则）
            try:
                cur = (http_json(f"{base}/api/anomalies", timeout=20) or {}).get("anomalies") or []
                report_online(base)  # 心跳：告诉 NAS 小助手还活着
            except Exception:
                cur = _fallback_anomalies(base)
            keys = {c.get("key") for c in cur}
            for k in list(seen):
                if k not in keys:
                    seen.discard(k)  # 异常恢复后才允许复发提醒
            fresh = [c for c in cur if c.get("key") not in seen]
            if fresh:
                for f in fresh:
                    seen.add(f.get("key"))
                summary = "；".join(f.get("title", "") for f in fresh)
                level = "critical" if any(int(f.get("sev", 1)) >= 2 for f in fresh) else "warn"
                # 同步双发：微信/邮件（服务端按最快通道优选）+ 本机弹窗，两边同一时刻收到
                try:
                    http_json(f"{base}/api/notify/alert", method="POST", timeout=20,
                              body={"title": "NAS Safe 异常提醒", "detail": summary, "level": level,
                                    "keys": [f.get("key") for f in fresh]})
                except Exception as e:
                    print("远端推送跳过：", e)
                # 本机弹一次（气泡只显示"标题消息 + 点击查看"，完整内容放在未读列表）
                n = len(fresh)
                head = fresh[0].get("title", "NAS Safe 异常提醒")
                if len(head) > 26:
                    head = head[:25] + "…"
                cta = head + (f"  等 {n} 项" if n > 1 else "")
                notify("NAS Safe 异常提醒", cta)
                # 每条异常作为一条未读落盘，点击气泡/托盘即可在未读界面看到全部内容
                for f in fresh:
                    ui.add_unread(f.get("key"), f.get("title", "NAS Safe 异常提醒"))
                print("[提醒]", summary)
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
        base = load_config().get("nas") or ""
        if agent_stop_remote():
            remove_autostart()
            notify(APP_NAME + " 已停止", "不再后台守护；异常提醒将改由微信 / 邮件发送")
        else:
            remove_autostart()
        report_offline(base)  # 立即让 NAS 端看门狗接管
        return
    if agent_online():
        notify(APP_NAME + " 已在运行", "无需重复启动")
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


def _do_install_full(base, cmd):
    """远程安装完整版 TS Safe（总控台经心跳下发命令）。

    流程：下载 NAS-Safe-Full.zip（一次性票据鉴权）→ 解压到无中文无空格的
    固定目录 → 以管理员运行 install_windows_service.bat（弹 UAC，用户点
    「是」）→ 脚本自动建 venv/装依赖/注册开机自启服务 → 完整版启动后用
    票据回总控台登记。结果随下一次心跳的 cmd_result 带回总控台。
    """
    result = {"action": "install_full", "ok": False, "error": ""}
    try:
        import tempfile
        import zipfile
        import urllib.parse
        ticket = str(cmd.get("ticket") or "")
        if not ticket:
            raise RuntimeError("命令缺少下载票据")
        url = base.rstrip("/") + "/api/downloads/full-bundle?ticket=" + urllib.parse.quote(ticket)
        dst = os.path.join(tempfile.gettempdir(), "NAS-Safe-Full")
        zip_path = dst + ".zip"
        os.makedirs(dst, exist_ok=True)
        agent_log("install_full: downloading bundle ...")
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dst)
        bat = os.path.join(dst, "install_windows_service.bat")
        if not os.path.exists(bat):
            raise RuntimeError("安装包里没有 install_windows_service.bat")
        agent_log("install_full: launching elevated installer (UAC) ...")
        ps = ("Start-Process -FilePath '{bat}' -WorkingDirectory '{dst}' "
              "-Verb RunAs").format(bat=bat.replace("'", "''"),
                                    dst=dst.replace("'", "''"))
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result["ok"] = True
        result["error"] = "已弹出管理员授权（UAC），请在那台电脑上点「是」"
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        agent_log("install_full failed: %s" % exc)
    agent_log("install_full result: ok=%s" % result["ok"])
    _PENDING_CMD_RESULT.clear()
    _PENDING_CMD_RESULT.update(result)


# 远程命令执行结果（下一次心跳带回总控台）
_PENDING_CMD_RESULT: dict = {}
_INSTALL_FULL_RUNNING = False   # 防重入：同一时刻只跑一次远程安装


def _device_agent_loop(base, token):
    """轻量代理心跳（与托盘 UI 一体）：注册一次，每 60s 心跳一次，静默重试。

    2026-10-08 起兼任「总控台命令通道」：心跳响应里带 cmd 就执行
    （目前支持 install_full=远程安装完整版），执行结果随下一次心跳回传。
    """
    def _post(path, obj):
        try:
            req = urllib.request.Request(base + path,
                                         data=json.dumps(obj).encode("utf-8"),
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
    hostname = os.environ.get("COMPUTERNAME") or socket.gethostname()
    osname = "Windows"
    try:
        _post("/api/agent/register", {"token": token, "hostname": hostname, "os": osname})
    except Exception:
        pass
    while not STOP_EVENT.is_set():
        body = {"token": token}
        if _PENDING_CMD_RESULT:
            body["cmd_result"] = dict(_PENDING_CMD_RESULT)
        resp = _post("/api/agent/heartbeat", body)
        if resp and resp.get("ok"):
            agent_log("agent heartbeat ok")
            if body.get("cmd_result"):
                _PENDING_CMD_RESULT.clear()   # 总控台已收到回执
            cmd = resp.get("cmd")
            if isinstance(cmd, dict) and cmd.get("action") == "install_full":
                global _INSTALL_FULL_RUNNING
                if _INSTALL_FULL_RUNNING:
                    agent_log("install_full skipped: already running")
                else:
                    _INSTALL_FULL_RUNNING = True

                    def _safe_install():
                        try:
                            _do_install_full(base, cmd)
                        finally:
                            globals()["_INSTALL_FULL_RUNNING"] = False

                    threading.Thread(target=_safe_install, daemon=True).start()
        else:
            agent_log("agent heartbeat failed")
        STOP_EVENT.wait(60)


def run_agent(base, interval, first=False, once=False, no_ui=False, token=""):
    repair_protocol()  # 守护启动前自愈协议注册（重装/清理后指向可能错误）
    start_control_server()
    _TOAST.start()     # 启动 Win32 弹窗后台线程（与托盘同源，保证提醒可见）
    atexit.register(report_offline, base)  # 任何退出路径都上报离线，让 NAS 接管微信提醒
    report_online(base, force=True)
    if token:
        threading.Thread(target=_device_agent_loop, args=(base, token), daemon=True).start()

    def _start_heartbeat(tok):
        """配对成功后动态起心跳线程（防重复：已起过就不再起）。"""
        if not tok or getattr(run_agent, "_hb_token", None) == tok:
            return
        run_agent._hb_token = tok
        threading.Thread(target=_device_agent_loop, args=(base, tok), daemon=True).start()

    def _pair_action():
        """托盘菜单「输入配对码」：弹窗收码 → 换令牌 → 立即开始心跳。"""
        tok = pair_flow(base)
        if tok:
            _start_heartbeat(tok)
            notify(APP_NAME + " 配对成功",
                   "这台电脑已登记进总控台并保持在线；以后开机也会自动连上。")
            agent_log(f"pair ok via tray, token set ({tok[:4]}...)")
        else:
            agent_log("pair cancelled or failed via tray")

    seen = set()
    print(f"NAS Safe 小助手已启动：{base}（每 {interval}s 检查一次）")
    if once:
        poll_once(base, NullUI(), seen)
        return

    # 首选：系统托盘图标（右下角通知区域，平时绿色盾牌，告警时中间红色感叹号）
    tray = None
    if not no_ui:
        def _refresh():
            if tray:
                tray.update(len(load_unread()))

        def _read_all():
            save_unread([])
            _refresh()

        tray = TrayIcon(on_open=lambda: show_inbox_window(_refresh),
                        on_read_all=_read_all,
                        on_quit=lambda: STOP_EVENT.set(),
                        on_pair=_pair_action)
        if not tray.start():
            tray = None
        else:
            _refresh()
    if tray:
        if first:
            notify(APP_NAME + " 已启动", "已缩到右下角托盘，异常时图标会亮红感叹号")
        poll_loop(base, interval, TraySink(tray), seen)
        tray.stop()
        return

    # 兜底 1：右下角小窗（不进任务栏）
    ui = AgentUI(base)
    if ui.available():
        if first:
            notify(APP_NAME + " 已启动", f"正在守护 {base}，异常会在这里提醒你")
        threading.Thread(target=poll_loop, args=(base, interval, ui, seen), daemon=True).start()
        ui.run()
        return

    # 兜底 2：纯后台（只弹 Windows 通知）
    if first:
        notify(APP_NAME + " 已启动", f"正在守护 {base}，异常会在这里提醒你")
    poll_loop(base, interval, NullUI(), seen)


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
    ap.add_argument("--token", default="", help="设备轻量代理令牌（中控安装命令下发，带 UI 守护 + 代理心跳一体）")
    ap.add_argument("--once", action="store_true", help="只检测一次（调试用）")
    args = ap.parse_args()

    if args.protocol:
        handle_protocol(args.protocol)
        return

    # 双击下载的 EXE：自己完成安装（复制到固定目录 + 开机自启 + 启动托盘副本）
    if getattr(sys, "frozen", False) and \
            os.path.abspath(os.path.dirname(sys.executable)) != os.path.abspath(install_dir()):
        if self_install_flow(args.nas.rstrip("/"), token=args.token):
            return

    if agent_online():
        print("已有小助手在运行（如需重启，请在网页设置里先关闭再开启）。")
        return

    cfg = load_config()
    base = args.nas.rstrip("/") or (cfg.get("nas") or "")
    first_run = not base
    auto_install = False
    if not base:
        # 安装包自带的地址（从 NAS 网页下载的包里已预置）：不再让用户选，直接装
        bundled = bundled_nas()
        if bundled:
            base = bundled
            first_run = True
            auto_install = True
    if not base:
        # 没有内置地址：走安装向导（后台探测后让用户确认）
        print("首次运行，打开安装向导…")
        base = install_wizard(args.nas.rstrip("/")) or ""
    if not base:
        print("未选择 NAS 地址，退出。")
        return

    interval = args.interval or int(cfg.get("interval") or DEFAULT_INTERVAL)
    token = args.token or (cfg.get("agent_token") or "")
    if not token and base and not args.no_ui and not args.once:
        # 傻瓜式配对：没有令牌但有 NAS 地址 → 提示输入 6 位配对码换取令牌
        paired = pair_flow(base)
        if paired:
            token = paired
    if args.install or first_run:
        cfg2 = load_config()
        cfg2.update({"nas": base, "interval": interval})
        if token:
            cfg2["agent_token"] = token
        save_config(cfg2)
        ensure_autostart(base, interval, token=token)
        register_protocol()
        if auto_install:
            install_progress(base)  # 只显示进度，不弹选择窗口
    run_agent(base, interval, first=args.install or first_run,
              once=args.once, no_ui=args.no_ui, token=token)


if __name__ == "__main__":
    main()
