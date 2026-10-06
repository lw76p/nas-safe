"""TS Safe — 桌面小助手本机诊断与拉起。

为什么需要这个模块
------------------
用户在网页上点「重新启动」只能靠 `nassafe-agent://` 自定义协议拉起本机助手，
一旦协议没注册、或注册指向的文件已被清理/改名，浏览器会**静默失败**——网页只
知道「127.0.0.1:18765 ping 不通」，拿不到任何原因，用户只能干瞪眼。

而本机 TS Safe 服务端**就跑在这台电脑上**，它可以直接检查注册表、程序目录、
控制端口、助手日志，并**直接拉起进程**（不经过浏览器协议）。于是把
「没反应」变成可定位、可修复的问题。

注意：本模块只关心**本机**的桌面助手；在 Linux/macOS 上没有桌面托盘概念，
返回 applicable=False，前端据此隐藏相关入口。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

APP_DIR_NAME = "NAS Safe 桌面助手"   # 必须与 desktop_agent.py 的 APP_DIR 完全一致
PROTOCOL_KEY = r"Software\Classes\nassafe-agent\shell\open\command"
CTRL_PORT = 18765
EXE_NAMES = ("桌面助手.exe", "NASSafeAgent.exe")
# 协议命令形如："C:\Users\xx\AppData\Roaming\NASSafeAgent\桌面助手.exe" --protocol "%1"
# 这里用 GBK 兜底：Windows 简体中文控制台默认代码页就是 GBK
_DECODERS = ("utf-8", "gbk", "mbcs", "latin-1")


def _is_windows() -> bool:
    return os.name == "nt"


def app_dir() -> str:
    base = os.environ.get("APPDATA") if _is_windows() else None
    return os.path.join(base or os.path.expanduser("~"), APP_DIR_NAME)


def _decode(raw) -> str:
    if isinstance(raw, str):
        return raw
    for enc in _DECODERS:
        try:
            return raw.decode(enc)
        except Exception:  # noqa: BLE001
            continue
    return repr(raw)


def port_listening(port: int = CTRL_PORT) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def protocol_state() -> dict:
    """读注册表看 nassafe-agent:// 是否注册、指向的文件还在不在。"""
    out = {"registered": False, "command": "", "target": "", "target_exists": False}
    if not _is_windows():
        return out
    try:
        import winreg
    except Exception:  # noqa: BLE001
        return out
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY) as k:
            val, _ = winreg.QueryValueEx(k, "")
        out["registered"] = True
        cmd = _decode(val).strip()
        out["command"] = cmd
        # 取出第一个被引号包起来的路径
        if '"' in cmd:
            target = cmd.split('"')[1]
        else:
            target = cmd.split(" --")[0].strip()
        out["target"] = target
        out["target_exists"] = bool(target) and os.path.isfile(target)
    except FileNotFoundError:
        out["registered"] = False
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def program_state() -> dict:
    """助手程序目录：有没有 exe、有没有配置、有没有日志。"""
    d = app_dir()
    out: dict = {"dir": d, "dir_exists": os.path.isdir(d), "exe": "", "files": []}
    if out["dir_exists"]:
        try:
            out["files"] = sorted(os.listdir(d))[:20]
        except OSError:
            out["files"] = []
        for name in EXE_NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                out["exe"] = p
                break
        out["config_exists"] = os.path.isfile(os.path.join(d, "config.json"))
    return out


def log_tail(lines: int = 15) -> list:
    p = os.path.join(app_dir(), "agent.log")
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-lines:]]
    except (OSError, FileNotFoundError):
        return []


def diagnose() -> dict:
    """汇总本机桌面助手的可诊断信息（供 /api/agent/diag）。"""
    if not _is_windows():
        return {"ok": True, "applicable": False,
                "reason": "桌面小助手只在 Windows 上可用，本机是 %s" % (
                    sys.platform or os.name)}
    proto = protocol_state()
    prog = program_state()
    listening = port_listening(CTRL_PORT)
    diag = {
        "ok": True,
        "applicable": True,
        "running": listening,
        "ctrl_port": CTRL_PORT,
        "protocol": proto,
        "program": prog,
        "log": log_tail(15),
    }
    # 结论 + 下一步建议（直接给用户看的白话）
    if listening:
        diag["verdict"] = "助手正在运行"
        diag["advice"] = ""
    elif not proto.get("registered"):
        diag["verdict"] = "「重新启动」用不了：nassafe-agent:// 协议没有注册"
        diag["advice"] = ("点下面的「重新下载并安装」重装一次桌面小助手；"
                          "装完就能一键拉起。")
    elif not proto.get("target_exists"):
        diag["verdict"] = "协议注册了，但指向的程序已经不在（多半被清理过或改名了）"
        diag["advice"] = "点下面的「重新下载并安装」重装一次即可。"
    elif not prog.get("dir_exists"):
        diag["verdict"] = "协议注册了，但助手目录不存在"
        diag["advice"] = "点下面的「重新下载并安装」重装一次即可。"
    else:
        diag["verdict"] = "程序在、协议也在，但拉起后没起来（可能启动即崩溃）"
        diag["advice"] = ("先点「用服务直接拉起」试试；"
                          "仍不行就看下面的助手日志，把报错发给我。")
    return diag


def launch() -> dict:
    """由本机服务端直接拉起桌面助手（绕开浏览器协议，最可靠）。"""
    if not _is_windows():
        return {"ok": False, "error": "桌面小助手只在 Windows 上可用"}
    if port_listening(CTRL_PORT):
        return {"ok": True, "already_running": True}
    prog = program_state()
    exe = prog.get("exe")
    if not exe:
        # 兜底：按协议注册的目标找
        t = protocol_state().get("target") or ""
        if t and os.path.isfile(t):
            exe = t
    if not exe:
        return {"ok": False, "error": "本机没找到桌面小助手程序（%APPDATA%\\%s）" % APP_DIR_NAME,
                "installed": False}
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    try:
        subprocess.Popen([exe, "--protocol", "start"],
                         cwd=os.path.dirname(exe),
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "拉起失败：%s" % exc, "exe": exe}
    return {"ok": True, "launched": True, "exe": exe}


if __name__ == "__main__":
    print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
