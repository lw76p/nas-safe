"""
NAS Safe — 多渠道告警通知分发器（仅标准库，零第三方依赖）

设计原则（见 PRODUCT.md §九）：
  - A 类零门槛：企业微信 / 飞书 / 钉钉 群机器人 Webhook（粘一条 URL 即用）
  - B 类核心：微信服务号模板消息（需 appid/appsecret/template_id/openid）
  - 极客向：Bark / ntfy
  - 补充：邮件（SMTP）
  - 全部配置存于 state 目录的 notify.json，**绝不进仓库**；未配置任何通道时
    dispatch() 为空操作，核心功能零影响。

所有发送失败都吞掉异常、返回 (ok, msg)，不波及主流程。
"""

from __future__ import annotations

import json
import os
import smtplib
import time
import urllib.request
import urllib.error
from email.mime.text import MIMEText

import storage  # 复用 state_dir()


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------

def config_path() -> str:
    return os.path.join(storage.state_dir(), "notify.json")


def load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"enabled": False, "channels": []}


def save_config(cfg: dict) -> None:
    os.makedirs(storage.state_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 消息格式化
# ---------------------------------------------------------------------------

def _plain(alerts: list, events: list) -> str:
    lines = ["【NAS Safe 安全动态】"]
    if events:
        lines.append("— 变动 —")
        for e in events:
            lines.append(f"• {e.get('title','')}：{e.get('detail','')}")
    if alerts:
        lines.append("— 告警 —")
        for a in alerts:
            lvl = "🔴" if a.get("level") == "critical" else ("🟠" if a.get("level") == "warn" else "⚪")
            lines.append(f"{lvl} {a.get('title','')}：{a.get('detail','')}")
    if not events and not alerts:
        lines.append("暂无新动态")
    return "\n".join(lines)


def _markdown(alerts: list, events: list) -> str:
    return _plain(alerts, events)


# ---------------------------------------------------------------------------
# 各通道实现（每个返回 (ok: bool, msg: str)）
# ---------------------------------------------------------------------------

def _http_post_json(url: str, payload: dict, token: str = "", timeout: int = 10) -> (bool, str):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    # 必须伪装常规 UA：Python 默认 "Python-urllib/x.y" 会被 Cloudflare WAF 判为机器人并 403
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; NAS Safe/1.0)")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return True, body[:200]
    except urllib.error.URLError as exc:
        return False, f"网络错误: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"发送失败: {exc}"


def _send_webhook(ch: dict, text: str, *_) -> (bool, str):
    """企业微信 / 飞书 / 钉钉 群机器人：统一走 markdown / text 字段。"""
    url = ch.get("url", "").strip()
    if not url:
        return False, "缺少 webhook URL"
    # 飞书/企业微信支持 markdown；钉钉支持 text。统一发 text 最稳。
    payload = {"msgtype": "text", "text": {"content": text}}
    return _http_post_json(url, payload)


def _send_bark(ch: dict, text: str, *_) -> (bool, str):
    url = ch.get("url", "").strip()
    if not url:
        return False, "缺少 Bark URL"
    # Bark: https://api.day.app/<key>/标题/内容
    key = ch.get("key", "").strip()
    if not key and "day.app" in url:
        pass
    endpoint = url.rstrip("/")
    if endpoint.endswith("/"):
        endpoint = endpoint[:-1]
    # 支持直接填完整 key URL 或 分开填
    if key and "day.app" not in endpoint:
        endpoint = f"https://api.day.app/{key}"
    payload = {"title": "NAS Safe 安全动态", "body": text}
    return _http_post_json(endpoint, payload)


def _send_ntfy(ch: dict, text: str, *_) -> (bool, str):
    topic = ch.get("topic", "").strip()
    base = ch.get("base", "https://ntfy.sh").strip().rstrip("/")
    if not topic:
        return False, "缺少 ntfy topic"
    url = f"{base}/{topic}"
    req = urllib.request.Request(url, data=text.encode("utf-8"), method="POST")
    req.add_header("Title", "NAS Safe 安全动态")
    req.add_header("Priority", "high")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"发送失败: {exc}"


# ---- 微信服务号模板消息 ----------------------------------------------------

def _wechat_token_path() -> str:
    return os.path.join(storage.state_dir(), "wechat_token.json")


def get_wechat_access_token(appid: str, secret: str) -> (str, str):
    """获取并缓存 access_token（有效期 7200s）。返回 (token, err)。"""
    cache = {}
    try:
        with open(_wechat_token_path(), "r", encoding="utf-8") as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if cache.get("token") and cache.get("expires_at", 0) > time.time() + 300:
        return cache["token"], ""
    url = ("https://api.weixin.qq.com/cgi-bin/token"
           f"?grant_type=client_credential&appid={appid}&secret={secret}")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return "", f"获取 access_token 失败: {exc}"
    if "access_token" not in data:
        return "", f"微信返回: {data}"
    token = data["access_token"]
    cache = {"token": token, "expires_at": time.time() + data.get("expires_in", 7200)}
    try:
        with open(_wechat_token_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass
    return token, ""


def _send_wechat_sa(ch: dict, text: str, alerts: list, events: list) -> (bool, str):
    appid = ch.get("appid", "").strip()
    secret = ch.get("appsecret", "").strip()
    template_id = ch.get("template_id", "").strip()
    openid = ch.get("openid", "").strip()
    if not all([appid, secret, template_id, openid]):
        return False, "缺少 appid/appsecret/template_id/openid"
    token, err = get_wechat_access_token(appid, secret)
    if err:
        return False, err
    # 把动态压进模板字段
    first = "NAS Safe 检测到新的安全动态" if (alerts or events) else "NAS Safe 心跳"
    keyword1 = "告警" if alerts else "信息"
    keyword2 = time.strftime("%Y-%m-%d %H:%M:%S")
    remark = text[:200]
    payload = {
        "touser": openid,
        "template_id": template_id,
        "data": {
            "first": {"value": first},
            "keyword1": {"value": keyword1},
            "keyword2": {"value": keyword2},
            "remark": {"value": remark},
        },
    }
    url = f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={token}"
    return _http_post_json(url, payload)


def _send_email(ch: dict, text: str, *_) -> (bool, str):
    host = ch.get("host", "").strip()
    port = int(ch.get("port", 465))
    user = ch.get("user", "").strip()
    pwd = ch.get("pass", "")
    to = ch.get("to", "").strip() or user
    if not all([host, user, to]):
        return False, "缺少 SMTP host/user/to"
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = "NAS Safe 安全动态"
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL(host, port, timeout=10) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        return True, "邮件已发送"
    except Exception as exc:  # noqa: BLE001
        return False, f"邮件失败: {exc}"


# ---- 厂商邮件中继（C 档默认·终端用户零配置通道）------------------------------
# 终端用户只填「接收邮箱」即可；发信密钥由厂商侧通过环境变量下发，绝不进用户配置。
#   NASSAFE_RELAY_PROVIDER  resend | brevo  （默认 resend）
#   NASSAFE_RELAY_APIKEY    厂商邮件 API Key
#   NASSAFE_RELAY_FROM      已验证发件人，如 alerts@tsetch.com
#   NASSAFE_RELAY_FROM_NAME 发件人显示名（默认 NAS Safe）

def relay_configured() -> bool:
    return bool(os.environ.get("NASSAFE_RELAY_APIKEY", "").strip())


def _parse_recipients(raw) -> list:
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").replace(";", ",").replace("\n", ",").split(",")
    out = []
    for x in items:
        x = x.strip()
        if x and "@" in x and "." in x.split("@")[-1]:
            out.append(x)
    return out


def _send_relay(ch: dict, text: str, *_) -> (bool, str):
    """终端用户零配置通道：厂商运营邮件中继，用户只提供接收邮箱。

    未配置厂商中继（NASSAFE_RELAY_APIKEY 缺失）→ 优雅降级提示，不崩溃。
    """
    api_key = os.environ.get("NASSAFE_RELAY_APIKEY", "").strip()
    if not api_key:
        return False, "厂商邮件中继未配置（环境变量 NASSAFE_RELAY_APIKEY 缺失）"
    recipients = _parse_recipients(ch.get("recipients")) or _parse_recipients(
        load_config().get("recipients")
    )
    if not recipients:
        return False, "未设置接收邮箱（请在通道中填写接收邮箱）"
    provider = os.environ.get("NASSAFE_RELAY_PROVIDER", "resend").strip().lower()
    sender = os.environ.get("NASSAFE_RELAY_FROM", "alerts@tsetch.com").strip()
    sender_name = os.environ.get("NASSAFE_RELAY_FROM_NAME", "NAS Safe").strip()
    subject = "NAS Safe 安全动态"
    if provider == "brevo":
        payload = {
            "sender": {"name": sender_name, "email": sender},
            "to": [{"email": r} for r in recipients],
            "subject": subject,
            "textContent": text,
        }
        url = "https://api.brevo.com/v3/smtp/email"
    else:  # resend（默认）
        payload = {
            "from": f"{sender_name} <{sender}>",
            "to": recipients,
            "subject": subject,
            "text": text,
        }
        url = "https://api.resend.com/emails"
    return _http_post_json(url, payload, token=api_key)


_CHANNEL_DISPATCH = {
    "relay": _send_relay,
    "webhook": _send_webhook,
    "bark": _send_bark,
    "ntfy": _send_ntfy,
    "wechat_service_account": _send_wechat_sa,
    "email": _send_email,
}


# ---------------------------------------------------------------------------
# 对外：分发
# ---------------------------------------------------------------------------

def dispatch(alerts: list = None, events: list = None) -> dict:
    """把告警 + 变动推送到所有已启用的通道。

    返回 {"sent": [...通道结果...], "skipped": "未启用" 或 [...]}
    未配置 / 未启用 → 返回 skipped，不影响主流程。
    """
    alerts = alerts or []
    events = events or []
    cfg = load_config()
    if not cfg.get("enabled", False):
        return {"enabled": False, "sent": [], "skipped": "通知总开关未开启"}
    channels = [c for c in cfg.get("channels", []) if c.get("type")]
    if not channels:
        return {"enabled": True, "sent": [], "skipped": "没有启用任何通道"}
    text = _plain(alerts, events)
    results = []
    for ch in channels:
        kind = ch.get("type")
        fn = _CHANNEL_DISPATCH.get(kind)
        if not fn:
            results.append({"channel": kind, "ok": False, "msg": "未知通道类型"})
            continue
        ok, msg = fn(ch, text, alerts, events)
        results.append({"channel": kind, "ok": ok, "msg": msg})
    return {"enabled": True, "sent": results}


def send_test(channel: dict) -> dict:
    """测试单个通道配置是否可用。"""
    kind = channel.get("type")
    fn = _CHANNEL_DISPATCH.get(kind)
    if not fn:
        return {"ok": False, "msg": "未知通道类型"}
    ok, msg = fn(channel, _plain([], [{"title": "测试消息", "detail": "这是一条来自 NAS Safe 的测试推送"}]), [],
                 [{"title": "测试消息", "detail": "这是一条来自 NAS Safe 的测试推送"}])
    return {"ok": ok, "msg": msg}


# ---------------------------------------------------------------------------
# 后台自动推送（扫描新告警并去重分发）
# ---------------------------------------------------------------------------

def _sent_state_path() -> str:
    return os.path.join(storage.state_dir(), "notify_sent.json")


def _alert_sig(a: dict) -> str:
    return f"{a.get('level')}|{a.get('title')}|{a.get('detail')}"


def scan_and_dispatch() -> None:
    """扫描一次受保护快照告警（含 v2 完整性），把"新增"的推送到已配置通道。

    仅当配置启用时工作；去重：同一告警签名已推送过则不再发。
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        return
    try:
        alerts = storage.scan_tamper(include_integrity=True)
    except Exception:  # noqa: BLE001
        return
    sigs = [_alert_sig(a) for a in alerts]
    last = []
    try:
        with open(_sent_state_path(), "r", encoding="utf-8") as f:
            last = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        last = []
    new_alerts = [a for a, s in zip(alerts, sigs) if s not in set(last)]
    if new_alerts:
        dispatch(new_alerts, [])
    # 滚动保留最近 200 个签名，避免无限增长
    try:
        with open(_sent_state_path(), "w", encoding="utf-8") as f:
            json.dump(sigs[-200:], f)
    except OSError:
        pass


def start_notifier(interval: int = 60) -> "threading.Thread":
    """启动后台守护线程，每隔 interval 秒扫描并推送新告警。"""
    import threading

    def _loop() -> None:
        while True:
            time.sleep(max(10, interval))
            try:
                scan_and_dispatch()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
