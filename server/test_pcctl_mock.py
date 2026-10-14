"""test_pcctl_mock — 用假 WinRM 服务器验证 pcctl 客户端的完整协议流程。

不打真实电脑：本地起一个 ThreadingHTTPServer 假装 WinRM，
按 create→command→receive(带 base64 stdout + CommandState=Done)→delete 应答，
验证 pcctl.run/test/status/scan 的 SOAP 编排、Base64 解码、超时与错误路径。
"""
import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, ".")
import pcctl  # noqa: E402

PASSWORD = "pw123"


class FakeWinRM(BaseHTTPRequestHandler):
    shell_id = "SHELL-1"
    cmd_id = "CMD-1"
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        FakeWinRM.calls.append(body)
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Basic "):
            self.send_response(401)
            self.end_headers()
            return
        # 真校验账号密码：错误凭据也必须 401（复现真实 WinRM 行为）
        import base64 as _b64
        try:
            raw = _b64.b64decode(auth[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001
            raw = ""
        if raw != "u:%s" % PASSWORD:
            self.send_response(401)
            self.end_headers()
            return
        if b"create" in body.encode():
            resp = self._env(
                '<rsp:Shell xmlns:rsp="%s" ShellId="%s"/>' % (pcctl._NS["rsp"], FakeWinRM.shell_id))
        elif "CommandLine" in body:
            resp = self._env('<rsp:CommandResponse xmlns:rsp="%s" CommandId="%s"/>'
                             % (pcctl._NS["rsp"], FakeWinRM.cmd_id))
        elif "DesiredStream" in body:
            payload = base64.b64encode(
                json.dumps({"ok": True, "cpu": 7, "host": "FAKEPC"}).encode()).decode()
            resp = self._env(
                '<rsp:Receive xmlns:rsp="%s">'
                '<rsp:Stream Name="stdout" CommandId="%s">%s</rsp:Stream>'
                '<rsp:CommandState State="http://schemas.microsoft.com/wbem/wsman/1/windows/shell/CommandState/Done" ExitCode="0"/>'
                "</rsp:Receive>" % (pcctl._NS["rsp"], FakeWinRM.cmd_id, payload))
        else:
            resp = self._env("")
        data = resp.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml;charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _env(self, body):
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<s:Envelope xmlns:s="%s" xmlns:w="%s" xmlns:rsp="%s">'
                "<s:Header/><s:Body>%s</s:Body></s:Envelope>"
                % (pcctl._NS["s"], pcctl._NS["w"], pcctl._NS["rsp"], body))


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 55985), FakeWinRM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dev = {"host": "127.0.0.1", "port": 55985, "https": False,
           "winrm_user": "u", "winrm_pass": PASSWORD}

    fails = []

    # 1) test()：完整 create→command→receive→delete 流程
    r = pcctl.test(dev)
    print("test:", r)
    if not r.get("ok"):
        fails.append("test 失败")

    # 2) status()：JSON 透传解析
    r = pcctl.status(dev)
    print("status:", r)
    if not (r.get("ok") and r.get("host") == "FAKEPC" and r.get("cpu") == 7):
        fails.append("status JSON 解析不对")

    # 3) scan()：paths 透传
    r = pcctl.scan(dev, ["C:\\Users", "D:\\"])
    print("scan:", r)
    if not (r.get("ok") and r.get("paths") == ["C:\\Users", "D:\\"]):
        fails.append("scan 不对")

    # 4) 错误路径：密码错 → 401 友好提示
    bad = dict(dev, winrm_pass="wrong")
    try:
        pcctl.test(bad)
        fails.append("401 未抛错")
    except pcctl.PcCtlError as e:
        print("401 →", e)
        if "拒绝" not in str(e):
            fails.append("401 提示不友好")

    # 5) 错误路径：端口没人听 → URLError 友好提示
    try:
        pcctl.test({"host": "127.0.0.1", "port": 55999, "https": False,
                    "winrm_user": "u", "winrm_pass": "p"})
        fails.append("拒连接未抛错")
    except pcctl.PcCtlError as e:
        print("refused →", e)
        if "连不上" not in str(e):
            fails.append("拒连接提示不友好")

    # 6) delete 必须被调用（shell 泄漏检测）
    if not any("Delete" in c for c in FakeWinRM.calls):
        fails.append("没有 delete shell（会泄漏）")

    srv.shutdown()
    print("\ncalls:", len(FakeWinRM.calls))
    if fails:
        print("FAIL:", fails)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
