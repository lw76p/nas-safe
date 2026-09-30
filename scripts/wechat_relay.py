# -*- coding: utf-8 -*-
"""微信中继服务（部署在阿里云网关 47.108.213.178，固定 IP 在服务号白名单内）。
职责：
  1. GET  /health      健康检查
  2. GET  /token       返回 access_token（带缓存，提前 300s 刷新）
  3. POST /send        发送模板消息 {touser, template_id, data, url?, miniprogram?}
  4. GET/POST /callback  微信服务器配置回调：GET 验签 echostr；POST 收关注/取关/消息/菜单点击事件，openid 落盘
  5. GET/POST /menu      菜单管理：GET 查询当前菜单；POST(带共享密钥) 创建菜单
  6. POST /cs            发送客服消息(带共享密钥) {touser, content}
安全：仅监听 127.0.0.1:18841，外网经 nginx 反代（配共享 token 后再开放）。
AppSecret 经环境变量 WECHAT_APPID / WECHAT_SECRET 注入，不落代码。
菜单点击自动回复文案存 STATE_DIR/menu_texts.json（key → 文本），改文案无需改代码。
"""
import json, os, time, hashlib, threading, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APPID = os.environ.get("WECHAT_APPID", "")
SECRET = os.environ.get("WECHAT_SECRET", "")
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "")  # /send 共享密钥（防公网滥用）
STATE_DIR = os.environ.get("RELAY_STATE", "/opt/wechat-relay")
TOKEN_CACHE_FILE = os.path.join(STATE_DIR, "token_cache.json")
OPENIDS_FILE = os.path.join(STATE_DIR, "openids.json")
MENU_TEXTS_FILE = os.path.join(STATE_DIR, "menu_texts.json")
UA = "Mozilla/5.0 (compatible; NAS-Safe-Relay/1.0)"

_lock = threading.Lock()
_token = {"value": "", "expire_at": 0}


def _http(url, data=None, method="GET"):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, json.dumps({"errcode": -1, "errmsg": str(e)})


def _save_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def get_token(force=False):
    with _lock:
        now = time.time()
        if not force and _token["value"] and now < _token["expire_at"]:
            return _token["value"]
        url = ("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
               f"&appid={APPID}&secret={SECRET}")
        code, body = _http(url)
        try:
            d = json.loads(body)
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"token bad response: {code} {body[:200]}")
        if "access_token" not in d:
            raise RuntimeError(f"token failed: {body[:300]}")
        _token["value"] = d["access_token"]
        _token["expire_at"] = now + max(60, int(d.get("expires_in", 7200)) - 300)
        _save_json(TOKEN_CACHE_FILE, {"token": _token["value"], "expire_at": _token["expire_at"]})
        return _token["value"]


def send_template(payload):
    token = get_token()
    url = f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={token}"
    code, body = _http(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
    return code, body


def send_cs_text(openid, content):
    """客服消息文本（仅 48h 内互动过的粉丝可发）。"""
    token = get_token()
    url = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}"
    payload = {"touser": openid, "msgtype": "text", "text": {"content": content}}
    return _http(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")


def create_menu(buttons):
    token = get_token()
    url = f"https://api.weixin.qq.com/cgi-bin/menu/create?access_token={token}"
    return _http(url, data=json.dumps({"button": buttons}, ensure_ascii=False).encode("utf-8"), method="POST")


def get_menu():
    token = get_token()
    url = f"https://api.weixin.qq.com/cgi-bin/get_current_selfmenu_info?access_token={token}"
    return _http(url)


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, text, ctype="application/json; charset=utf-8"):
        b = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):  # noqa: A003
        print(time.strftime("%F %T"), fmt % args, flush=True)

    def do_GET(self):  # noqa: N802
        p = urllib.parse.urlparse(self.path).path
        if p == "/health":
            return self._reply(200, json.dumps({"ok": True, "time": time.strftime("%FT%T%z")}))
        if p == "/token":
            try:
                return self._reply(200, json.dumps({"ok": True, "access_token": get_token()}))
            except Exception as e:  # noqa: BLE001
                return self._reply(502, json.dumps({"ok": False, "error": str(e)[:300]}))
        if p == "/menu":
            code, body = get_menu()
            return self._reply(200, json.dumps({"wechat_status": code, "resp": body}))
        if p == "/callback":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            echostr = q.get("echostr", [""])[0]
            token = os.environ.get("WECHAT_CB_TOKEN", "")
            if echostr:
                # 微信服务器验签请求：校验签名后原样返回 echostr
                sig = (q.get("signature", [""])[0], q.get("timestamp", [""])[0], q.get("nonce", [""])[0])
                if token:
                    calc = hashlib.sha1("".join(sorted([token, sig[1], sig[2]])).encode()).hexdigest()
                    if calc != sig[0]:
                        return self._reply(403, "bad signature", "text/plain")
                return self._reply(200, echostr, "text/plain")
            # 非验签访问（浏览器直开）→ 友好提示
            return self._reply(200, "wechat callback endpoint ready", "text/plain")
        return self._reply(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        p = urllib.parse.urlparse(self.path).path
        if p == "/send":
            if RELAY_TOKEN:
                supplied = (self.headers.get("X-Relay-Token", "")
                            or self.headers.get("Authorization", "").replace("Bearer ", "").strip())
                if supplied != RELAY_TOKEN:
                    return self._reply(403, json.dumps({"ok": False, "error": "bad relay token"}))
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                return self._reply(400, json.dumps({"ok": False, "error": "bad json"}))
            try:
                code, body = send_template(payload)
                return self._reply(200, json.dumps({"wechat_status": code, "resp": json.loads(body) if body.startswith("{") else body}))
            except Exception as e:  # noqa: BLE001
                return self._reply(502, json.dumps({"ok": False, "error": str(e)[:300]}))
        if p == "/menu":
            if not self._check_relay_token():
                return self._reply(403, json.dumps({"ok": False, "error": "bad relay token"}))
            try:
                payload = json.loads(raw.decode("utf-8"))
                buttons = payload.get("button") or payload.get("buttons")
                if not buttons:
                    return self._reply(400, json.dumps({"ok": False, "error": "missing button"}))
                code, body = create_menu(buttons)
                return self._reply(200, json.dumps({"wechat_status": code, "resp": json.loads(body) if body.startswith("{") else body}))
            except Exception as e:  # noqa: BLE001
                return self._reply(502, json.dumps({"ok": False, "error": str(e)[:300]}))
        if p == "/cs":
            if not self._check_relay_token():
                return self._reply(403, json.dumps({"ok": False, "error": "bad relay token"}))
            try:
                payload = json.loads(raw.decode("utf-8"))
                openid, content = payload.get("touser", ""), payload.get("content", "")
                if not openid or not content:
                    return self._reply(400, json.dumps({"ok": False, "error": "missing touser/content"}))
                code, body = send_cs_text(openid, content)
                return self._reply(200, json.dumps({"wechat_status": code, "resp": json.loads(body) if body.startswith("{") else body}))
            except Exception as e:  # noqa: BLE001
                return self._reply(502, json.dumps({"ok": False, "error": str(e)[:300]}))
        if p == "/callback":
            # 微信事件推送（XML）：关注/取关/消息/菜单点击 → 提取 openid 落盘；CLICK 事件后台自动回复
            import re as _re
            text = raw.decode("utf-8", "replace")
            m = _re.search(r"<FromUserName><!\[CDATA\[(.+?)\]\]></FromUserName>", text)
            ev = _re.search(r"<MsgType><!\[CDATA\[(.+?)\]\]></MsgType>", text)
            evt = _re.search(r"<Event><!\[CDATA\[(.+?)\]\]></Event>", text)
            ek = _re.search(r"<EventKey><!\[CDATA\[(.+?)\]\]></EventKey>", text)
            if m:
                store = _load_json(OPENIDS_FILE, {})
                store[m.group(1)] = {
                    "event": evt.group(1) if evt else (ev.group(1) if ev else ""),
                    "time": time.strftime("%FT%T"),
                }
                _save_json(OPENIDS_FILE, store)
                # 菜单点击 → 按 menu_texts.json 自动回复客服消息（后台线程，不阻塞应答）
                if evt and evt.group(1) == "CLICK" and ek and m.group(1):
                    reply = _load_json(MENU_TEXTS_FILE, {}).get(ek.group(1))
                    if reply:
                        threading.Thread(
                            target=send_cs_text, args=(m.group(1), reply), daemon=True
                        ).start()
            return self._reply(200, "success", "text/plain")
        return self._reply(404, json.dumps({"ok": False, "error": "not found"}))

    def _check_relay_token(self):
        if not RELAY_TOKEN:
            return True
        supplied = (self.headers.get("X-Relay-Token", "")
                    or self.headers.get("Authorization", "").replace("Bearer ", "").strip())
        return supplied == RELAY_TOKEN


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    print("wechat-relay listening 127.0.0.1:18841", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 18841), Handler).serve_forever()
