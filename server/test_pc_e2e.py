# -*- coding: utf-8 -*-
"""E2E：真起 TS Safe 服务（8850 端口 + 临时状态目录）+ mock WinRM，
走完整 HTTP 链路：登录 → 添加电脑 → 状态 → 执行 → 关机(不发) → 摄像头登记 → 脚本下载。"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NASSAFE_PORT"] = "8850"
os.environ["NASSAFE_STATE_DIR"] = os.path.join(tempfile.gettempdir(), "tsafe_e2e_state")
os.environ["NASSAFE_WEB_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")

import test_pcctl_mock as mock  # noqa: E402
import pcctl  # noqa: E402

BASE = "http://127.0.0.1:8850"
fails = []


def req(method, path, body=None, cookie=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=30) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload.decode("utf-8", "replace"))), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload.decode("utf-8", "replace")), None
        except Exception:  # noqa: BLE001
            return e.code, payload[:200], None


def main():
    # 清干净临时状态目录，保证 setup 从零开始
    import shutil
    shutil.rmtree(os.environ["NASSAFE_STATE_DIR"], ignore_errors=True)

    # mock WinRM（电脑）
    srv = ThreadingHTTPServer(("127.0.0.1", 55985), mock.FakeWinRM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 真服务（子进程；cwd 与 NASSAFE_STATE_DIR 都指向临时目录，
    # 远离真实服务的 server/state，绝不碰线上数据）
    env = dict(os.environ)
    state_dir = env["NASSAFE_STATE_DIR"]
    env["NASSAFE_STATE_DIR"] = state_dir
    os.makedirs(state_dir, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath("app.py")], cwd=state_dir,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                st, _, _ = req("GET", "/api/health")
                if st == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        else:
            print(proc.stdout.read().decode() if proc.stdout else "")
            raise SystemExit("服务没起来")

        # 1) 初始化管理员
        st, r, _ = req("POST", "/api/auth/setup", {"username": "admin", "password": "test123456"})
        print("setup:", st, r)
        if st != 200:
            fails.append("setup 失败")

        # 2) 登录拿 cookie
        st, r, sc = req("POST", "/api/auth/login", {"username": "admin", "password": "test123456"})
        cookie = (sc or "").split(";")[0]
        print("login:", st, "cookie:", bool(cookie))
        if not cookie:
            fails.append("登录失败")

        # 3) 一键开启脚本下载（GET 默认公开）
        st, raw, _ = req("GET", "/api/downloads/enable-winrm.ps1", raw=True)
        text = raw.decode("utf-8-sig", "replace")
        print("enable-winrm.ps1:", st, len(text), "bytes | 含 Enable-PSRemoting:", "Enable-PSRemoting" in text)
        if st != 200 or "Enable-PSRemoting" not in text:
            fails.append("一键脚本下载异常")

        # 4) 添加电脑（指向 mock WinRM）
        st, r, _ = req("POST", "/api/devices/pc/add", {
            "name": "测试电脑", "host": "127.0.0.1", "port": 55985,
            "winrm_user": "u", "winrm_pass": "pw123"}, cookie=cookie)
        print("pc/add:", st, r)
        pc_id = (r or {}).get("id")
        if st != 200 or not pc_id:
            fails.append("加电脑失败")

        # 5) 未登录调转控 → 必须被挡（401/403）
        st, r, _ = req("POST", "/api/devices/pc/%s/status" % pc_id, {})
        print("未登录 status:", st, r)
        if st not in (401, 403):
            fails.append("未登录居然能转控")

        # 6) 状态（走 mock）
        st, r, _ = req("POST", "/api/devices/pc/%s/status" % pc_id, {}, cookie=cookie)
        print("pc/status:", st, r)
        if not (r or {}).get("ok") or (r or {}).get("host") != "FAKEPC":
            fails.append("状态解析不对")

        # 7) exec
        st, r, _ = req("POST", "/api/devices/pc/%s/exec" % pc_id,
                       {"script": "Get-Date", "timeout": 30}, cookie=cookie)
        print("pc/exec:", st, r)
        if not (r or {}).get("ok"):
            fails.append("exec 失败")

        # 8) power（restart 到 mock 也会 ok）
        st, r, _ = req("POST", "/api/devices/pc/%s/power" % pc_id,
                       {"action": "restart"}, cookie=cookie)
        print("pc/power:", st, r)
        if not (r or {}).get("ok"):
            fails.append("power 失败")

        # 9) 扫描
        st, r, _ = req("POST", "/api/devices/pc/%s/scan" % pc_id,
                       {"paths": ["C:\\Users"]}, cookie=cookie)
        print("pc/scan:", st, r)
        if not (r or {}).get("ok"):
            fails.append("scan 失败")

        # 10) 摄像头登记 + manage 列表带 kind
        st, r, _ = req("POST", "/api/devices/camera/add",
                       {"name": "大门摄像头", "host": "192.168.1.64"}, cookie=cookie)
        print("camera/add:", st, r)
        st, r, _ = req("GET", "/api/devices/manage", cookie=cookie)
        kinds = [(d.get("name"), d.get("kind"), d.get("status")) for d in (r or {}).get("devices", [])]
        print("manage:", kinds)
        if not any(k == "camera" for _, k, _ in kinds):
            fails.append("manage 里没有 camera")
        if not any(k == "pc" for _, k, _ in kinds):
            fails.append("manage 里没有 pc")

        # 11) 不存在的设备 → 友好报错
        st, r, _ = req("POST", "/api/devices/pc/nope/status", {}, cookie=cookie)
        print("坏 id:", st, r)
        if st != 400:
            fails.append("坏 id 没走 400")

    finally:
        proc.terminate()
        srv.shutdown()

    if fails:
        print("\nFAIL:", fails)
        sys.exit(1)
    print("\nE2E ALL PASS")


if __name__ == "__main__":
    main()
