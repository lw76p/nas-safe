"""pcctl — 电脑端远程管控通道（纯标准库 WinRM / WS-Management 客户端）

为什么不用 pywinrm：
  TS Safe 铁律是「零第三方依赖」——NAS（QTS / Linux 容器）上直接能跑，
  不能要求用户 pip install。WinRM 协议本身不复杂：向
  http(s)://<电脑>:5985(或5986)/wsman POST 几个 SOAP 信封即可。

认证：Basic（电脑端一次性开启，脚本见 /api/downloads/enable-winrm.ps1）。
传输：局域网内可走 HTTP（AllowUnencrypted）；设备勾选 https 则走 5986
      HTTPS（自签证书，本客户端不校验证书链，仅校验协议）。

设备字段（devices.json 里 kind=="pc" 的设备）：
  host / port(默认5985) / https / winrm_user / winrm_pass
  —— winrm_pass 明文存状态目录（ProgramData，系统权限），与 token 同等对待。

提供操作：
  test(dev)           连通性测试（返回 PowerShell 版本）
  status(dev)         系统状态（CPU/内存/磁盘/系统/开机时长/防火墙/Defender）
  run(dev, script)    远程执行 PowerShell，返回 stdout/stderr/exit
  power(dev, action)  重启 / 关机（WinRM 远程会话对管理员默认全令牌，无需再提权）
  scan(dev, paths)    远程重复文件扫描（按大小预分组 + SHA256 内容哈希，零误报）

安全约定：与 TS Safe 其它模块一致——状态/扫描只读；power 是不可逆操作，
      只允许已登录的网页会话显式触发（路由层已做会话校验，前端 confirm 二次确认）。
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# WS-Management 常量
# ---------------------------------------------------------------------------

_SHELL_URI = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/cmd"
_A_CREATE = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/create"
_A_COMMAND = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Command"
_A_RECEIVE = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Receive"
_A_DELETE = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Delete"
_A_SIGNAL = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Signal"

_NS = {
    "s": "http://www.w3.org/2003/09/soap-envelope",
    "w": "http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd",
    "rsp": "http://schemas.microsoft.com/wbem/wsman/1/windows/shell",
}

DEFAULT_PORT = 5985


class PcCtlError(Exception):
    """电脑端管控失败（网络/认证/协议/对端报错），message 面向用户可读。"""


# ---------------------------------------------------------------------------
# SOAP 信封构造
# ---------------------------------------------------------------------------

def _envelope(header: str, body: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="%(s)s" xmlns:w="%(w)s" xmlns:rsp="%(rsp)s">'
        "<s:Header>%(h)s</s:Header>"
        "<s:Body>%(b)s</s:Body>"
        "</s:Envelope>"
    ) % {"s": _NS["s"], "w": _NS["w"], "rsp": _NS["rsp"], "h": header, "b": body}
    return xml.encode("utf-8")


def _iso_timeout(seconds: float) -> str:
    return "PT%dS" % max(1, int(seconds))


def _create_env(timeout: float) -> bytes:
    header = (
        '<w:ResourceURI s:mustUnderstand="true">%s</w:ResourceURI>'
        '<w:Action s:mustUnderstand="true">%s</w:Action>'
        '<w:MaxEnvelopeSize s:mustUnderstand="true">512000</w:MaxEnvelopeSize>'
        '<w:OperationTimeout s:mustUnderstand="true">%s</w:OperationTimeout>'
        "<w:OptionSet>"
        '<w:Option Name="WINRS_NOPROFILE">TRUE</w:Option>'
        '<w:Option Name="WINRS_CODEPAGE">65001</w:Option>'
        "</w:OptionSet>"
    ) % (_SHELL_URI, _A_CREATE, _iso_timeout(timeout))
    body = (
        "<rsp:Shell>"
        "<rsp:InputStreams>stdin</rsp:InputStreams>"
        "<rsp:OutputStreams>stdout stderr</rsp:OutputStreams>"
        "</rsp:Shell>"
    )
    return _envelope(header, body)


def _command_env(shell_id: str, args: list, timeout: float) -> bytes:
    header = (
        '<w:ResourceURI s:mustUnderstand="true">%s</w:ResourceURI>'
        '<w:Action s:mustUnderstand="true">%s</w:Action>'
        '<w:OperationTimeout s:mustUnderstand="true">%s</w:OperationTimeout>'
        '<w:SelectorSet><w:Selector Name="ShellId">%s</w:Selector></w:SelectorSet>'
    ) % (_SHELL_URI, _A_COMMAND, _iso_timeout(timeout), escape(shell_id))
    body = '<rsp:CommandLine CommandId=""><rsp:Command>powershell</rsp:Command>'
    body += "".join("<rsp:Arguments>%s</rsp:Arguments>" % escape(str(a)) for a in args)
    body += "</rsp:CommandLine>"
    return _envelope(header, body)


def _receive_env(shell_id: str, command_id: str) -> bytes:
    header = (
        '<w:ResourceURI s:mustUnderstand="true">%s</w:ResourceURI>'
        '<w:Action s:mustUnderstand="true">%s</w:Action>'
        '<w:SelectorSet><w:Selector Name="ShellId">%s</w:Selector></w:SelectorSet>'
    ) % (_SHELL_URI, _A_RECEIVE, escape(shell_id))
    body = "<rsp:Receive>"
    for stream in ("stdout", "stderr"):
        body += '<rsp:DesiredStream CommandId="%s">%s</rsp:DesiredStream>' % (
            escape(command_id), stream)
    body += "</rsp:Receive>"
    return _envelope(header, body)


def _delete_env(shell_id: str) -> bytes:
    header = (
        '<w:ResourceURI s:mustUnderstand="true">%s</w:ResourceURI>'
        '<w:Action s:mustUnderstand="true">%s</w:Action>'
        '<w:SelectorSet><w:Selector Name="ShellId">%s</w:Selector></w:SelectorSet>'
    ) % (_SHELL_URI, _A_DELETE, escape(shell_id))
    return _envelope(header, "")


def _signal_env(shell_id: str, command_id: str, code: str = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/signal/terminate") -> bytes:
    header = (
        '<w:ResourceURI s:mustUnderstand="true">%s</w:ResourceURI>'
        '<w:Action s:mustUnderstand="true">%s</w:Action>'
        '<w:SelectorSet><w:Selector Name="ShellId">%s</w:Selector></w:SelectorSet>'
    ) % (_SHELL_URI, _A_SIGNAL, escape(shell_id))
    body = (
        '<rsp:Signal CommandId="%s">'
        '<rsp:Code>%s</rsp:Code>'
        "</rsp:Signal>"
    ) % (escape(command_id), code)
    return _envelope(header, body)


# ---------------------------------------------------------------------------
# HTTP 传输（Basic 认证；HTTPS 自签证书不校验）
# ---------------------------------------------------------------------------

def _endpoint(dev: dict) -> str:
    host = (dev.get("host") or "").strip()
    if not host:
        raise PcCtlError("设备没有填写电脑地址")
    scheme = "https" if dev.get("https") else "http"
    port = int(dev.get("port") or DEFAULT_PORT)
    return "%s://%s:%d/wsman" % (scheme, host, port)


def _auth_header(dev: dict) -> str:
    user = (dev.get("winrm_user") or "").strip()
    pwd = dev.get("winrm_pass") or ""
    if not user or not pwd:
        raise PcCtlError("设备没有配置 WinRM 账号密码")
    raw = ("%s:%s" % (user, pwd)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _post(endpoint: str, body: bytes, auth: str, timeout: float) -> str:
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/soap+xml;charset=UTF-8")
    req.add_header("Authorization", auth)
    # 关键：局域网请求必须绕过系统/环境变量里的 http_proxy，
    # 否则 JT-PC/用户机上配了代理时，WinRM 流量会被代理劫持（表现为 502）。
    handlers = [urllib.request.ProxyHandler({})]
    if endpoint.startswith("https"):
        # 自签证书不校验（局域网一次性开启脚本签的证书）
        handlers.append(urllib.request.HTTPSHandler(
            context=ssl._create_unverified_context()))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # WinRM 的 SOAP Fault 也是 HTTP 200/500 带 body，这里把 fault 文本抠出来
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
        msg = _extract_fault(raw) or ("HTTP %s" % e.code)
        raise PcCtlError(_friendly_http_error(e.code, msg))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        raise PcCtlError("连不上电脑（%s）。请确认：电脑开机、地址端口正确、"
                         "已在电脑上运行过「一键开启」脚本" % reason)
    except TimeoutError:
        raise PcCtlError("电脑响应超时")
    except OSError as e:
        raise PcCtlError("网络错误：%s" % e)


def _friendly_http_error(code: int, msg: str) -> str:
    if code in (401, 403):
        return ("账号或密码被电脑拒绝（HTTP %d）。请检查 WinRM 账号密码；"
                "电脑上是否用了管理员账户" % code)
    return "电脑返回错误（HTTP %s）：%s" % (code, msg[:200])


def _extract_fault(xml_text: str) -> str:
    if not xml_text:
        return ""
    m = re.search(r"<s:Fault.*?</s:Fault>", xml_text, re.S)
    if not m:
        return ""
    block = m.group(0)
    for pat in (r"<faultstring[^>]*>(.*?)</faultstring>",
                r"<wsman:Message>(.*?)</wsman:Message>",
                r"<Message>(.*?)</Message>"):
        mm = re.search(pat, block, re.S)
        if mm:
            return re.sub(r"\s+", " ", mm.group(1)).strip()
    return ""


# ---------------------------------------------------------------------------
# SOAP 解析
# ---------------------------------------------------------------------------

def _find(xml_text: str, path: str) -> object:
    """极简 XML 取值：手工 namespace 剥离后用 ElementTree 找。
    路径支持末尾 @属性，如 Envelope/Body/Shell/@ShellId。"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    def local(tag: str) -> str:
        return tag.split("}")[-1] if "}" in tag else tag

    want = [p for p in path.strip("/").split("/") if p]
    attr = None
    if want and want[-1].startswith("@"):
        attr = want.pop()[1:]
    nodes = [root]
    for i, name in enumerate(want):
        if i == 0 and local(root.tag) == name:
            continue  # 第一段就是根元素本身，不往 children 里找
        nxt = []
        for node in nodes:
            for child in list(node):
                if local(child.tag) == name:
                    nxt.append(child)
        nodes = nxt
        if not nodes:
            return None
    if attr:
        return nodes[0].get(attr)
    return nodes[0] if len(nodes) == 1 else nodes


# ---------------------------------------------------------------------------
# 协议流程：create shell → command → receive 循环 → delete
# ---------------------------------------------------------------------------

def run(dev: dict, script: str, timeout: float = 60.0) -> dict:
    """在电脑上执行 PowerShell 脚本，返回 {ok, exit, stdout, stderr, ms}。"""
    timeout = min(max(float(timeout), 5), 1800)
    endpoint = _endpoint(dev)
    auth = _auth_header(dev)
    t0 = time.time()

    # 1) 创建 shell
    raw = _post(endpoint, _create_env(timeout + 30), auth, timeout + 30)
    shell_id = _find(raw, "Envelope/Body/Shell/@ShellId")
    if not shell_id:
        raise PcCtlError("电脑没有返回 ShellId，WinRM 可能未开启或端口不对")

    try:
        # 2) 下发命令
        args = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-Command", script]
        raw = _post(endpoint, _command_env(shell_id, args, timeout), auth, timeout + 10)
        command_id = _find(raw, "Envelope/Body/CommandResponse/@CommandId")
        if not command_id:
            raise PcCtlError("电脑没有接受命令（CommandId 缺失）")

        # 3) 接收循环直到 CommandState=Done
        out_buf, err_buf = [], []
        exit_code = None
        deadline = time.time() + timeout + 20
        while time.time() < deadline:
            raw = _post(endpoint, _receive_env(shell_id, command_id), auth,
                        min(60, max(10, deadline - time.time())))
            # 注意：不能用 `_find(...) or []`——ElementTree 里无子元素的 Element
            # （如自闭合的 CommandState/Stream）bool 值为 False，会被 or 吞掉。
            streams = _find(raw, "Envelope/Body/Receive/Stream")
            if streams is None:
                streams = []
            elif not isinstance(streams, list):
                streams = [streams]
            for st in streams:
                name = (st.get("Name") or "").lower()
                text = (st.text or "").strip()
                if not text:
                    continue
                try:
                    data = base64.b64decode(text).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    data = text
                if name == "stderr":
                    err_buf.append(data)
                else:
                    out_buf.append(data)
            states = _find(raw, "Envelope/Body/Receive/CommandState")
            if states is None:
                states = []
            elif not isinstance(states, list):
                states = [states]
            for stt in states:
                state = stt.get("State") or ""
                if state.endswith("Done"):
                    try:
                        exit_code = int(stt.get("ExitCode") or 0)
                    except (TypeError, ValueError):
                        exit_code = 0
            if exit_code is not None:
                break
        else:
            # 超时：发 terminate 信号，按失败返回已有输出
            try:
                _post(endpoint, _signal_env(shell_id, command_id), auth, 15)
            except PcCtlError:
                pass
            return {"ok": False, "exit": -1,
                    "stdout": "".join(out_buf), "stderr": "".join(err_buf),
                    "error": "命令执行超时（%ds）" % timeout,
                    "ms": int((time.time() - t0) * 1000)}

        return {"ok": (exit_code or 0) == 0, "exit": exit_code or 0,
                "stdout": "".join(out_buf), "stderr": "".join(err_buf),
                "ms": int((time.time() - t0) * 1000)}
    finally:
        # 4) 关 shell（失败不影响主结果）
        try:
            _post(endpoint, _delete_env(shell_id), auth, 20)
        except PcCtlError:
            pass


# ---------------------------------------------------------------------------
# 高层操作
# ---------------------------------------------------------------------------

def test(dev: dict) -> dict:
    r = run(dev, "$PSVersionTable.PSVersion.Major", timeout=20)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("stderr") or r.get("error") or "连接失败"}
    return {"ok": True, "ps_version": (r.get("stdout") or "").strip()}


_STATUS_SCRIPT = r"""
$o = [ordered]@{}
$os = Get-CimInstance Win32_OperatingSystem
$o.cpu = [int](Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$o.mem_total = [uint64]$os.TotalVisibleMemorySize * 1024
$o.mem_free  = [uint64]$os.FreePhysicalMemory * 1024
$o.os = $os.Caption
$o.host = $env:COMPUTERNAME
$o.uptime = ((Get-Date) - $os.LastBootUpTime).ToString()
$o.disks = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    [ordered]@{ name=$_.DeviceID; size=[uint64]$_.Size; free=[uint64]$_.FreeSpace }
})
$o.firewall = @(Get-NetFirewallProfile | ForEach-Object {
    [ordered]@{ name=$_.Name; enabled=[bool]$_.Enabled }
})
try { $o.defender = [bool](Get-MpComputerStatus).RealTimeProtectionEnabled } catch { $o.defender = $null }
$o | ConvertTo-Json -Compress -Depth 5
"""


def status(dev: dict) -> dict:
    r = run(dev, _STATUS_SCRIPT, timeout=45)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("stderr") or r.get("error") or "取状态失败"}
    txt = (r.get("stdout") or "").strip()
    # 有些机器会先吐 BOM/杂讯，抠第一个 { 到最后一个 }
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j <= i:
        return {"ok": False, "error": "对端返回了无法解析的状态数据"}
    try:
        data = json.loads(txt[i:j + 1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "对端状态 JSON 解析失败"}
    data["ok"] = True
    return data


def power(dev: dict, action: str) -> dict:
    action = (action or "").strip().lower()
    if action not in ("restart", "shutdown"):
        raise PcCtlError("只支持 restart / shutdown")
    script = "Restart-Computer -Force" if action == "restart" else "Stop-Computer -Force"
    # 关机命令对端可能来不及回包，短超时 + 允许 exit!=0（连接被重置也属正常）
    try:
        r = run(dev, script, timeout=25)
    except PcCtlError:
        # 关机瞬间连接必然被切断，视为已下发
        return {"ok": True, "sent": True,
                "note": "命令已下发，电脑正在%s" % ("重启" if action == "restart" else "关机")}
    return {"ok": True, "sent": True, "exit": r.get("exit"),
            "stderr": (r.get("stderr") or "")[:200]}


_SCAN_SCRIPT_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$paths = @(%s)
$files = Get-ChildItem -LiteralPath $paths -Recurse -File -Force |
    Select-Object FullName, Length, LastWriteTimeUtc
$total = @($files).Count
$groups = @()
$bySize = $files | Group-Object Length | Where-Object { $_.Count -gt 1 }
$hashed = 0
foreach ($g in $bySize) {
    $items = @($g.Group | ForEach-Object {
        $h = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        $hashed++
        [ordered]@{ path=$_.FullName; hash=$h; mtime=$_.LastWriteTimeUtc.ToString('s') }
    })
    foreach ($hg in ($items | Group-Object Hash | Where-Object { $_.Count -gt 1 })) {
        if ($groups.Count -ge 100) { break }
        $groups += [ordered]@{
            size = [uint64]$g.Name
            count = @($hg.Group).Count
            files = @($hg.Group | ForEach-Object { $_.path })
        }
    }
    if ($hashed -ge 20000) { break }
}
[ordered]@{
    ok = $true
    total_files = $total
    hashed_files = $hashed
    dup_groups = $groups.Count
    groups = $groups
    truncated = ($hashed -ge 20000)
} | ConvertTo-Json -Compress -Depth 6
"""


def scan(dev: dict, paths: list, timeout: float = 600.0) -> dict:
    """远程重复文件扫描：按大小预分组 + SHA256 内容哈希（与 NAS 端判定标准一致）。"""
    safe = [str(p).replace("'", "''") for p in (paths or []) if str(p).strip()]
    if not safe:
        raise PcCtlError("请填写要扫描的目录")
    quoted = ", ".join("'%s'" % p for p in safe)
    r = run(dev, _SCAN_SCRIPT_TEMPLATE % quoted, timeout=timeout)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("stderr") or r.get("error") or "远程扫描失败"}
    txt = (r.get("stdout") or "").strip()
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j <= i:
        return {"ok": False, "error": "对端返回了无法解析的扫描结果"}
    try:
        data = json.loads(txt[i:j + 1])
    except json.JSONDecodeError:
        return {"ok": False, "error": "对端扫描 JSON 解析失败"}
    data["ok"] = True
    data["paths"] = safe
    return data
