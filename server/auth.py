# -*- coding: utf-8 -*-
"""账号、登录会话与权限分级。

设计目标：
1. 不登录 = 只能看页面，任何功能开关/按钮点不动（服务端也会拒绝）。
2. 管理员（admin）能做一切；只读成员（viewer）只能看状态，不能改设置、不能删文件。
3. 不引入任何第三方依赖：口令用标准库 PBKDF2 加盐存储，会话用 HMAC 签名 cookie。

口令存储格式：pbkdf2_sha256$<迭代次数>$<salt_b64>$<hash_b64>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

try:
    from . import state as _state_pkg  # type: ignore
except Exception:  # noqa: BLE001
    _state_pkg = None

_LOCK = threading.Lock()
_ITERS = 120_000
_COOKIE = "nassafe_sid"
_SESSION_HOURS = 12

# 登录失败限速：同一账号 5 分钟内最多 10 次，防止暴力猜口令
_FAIL_WINDOW = 300
_FAIL_MAX = 10
_fail_log: dict[str, list[float]] = {}


# --------------------------------------------------------------------------- 存储

def _state_dir() -> str:
    try:
        from .state import state_dir  # type: ignore
        return state_dir()
    except Exception:  # noqa: BLE001
        return os.path.join(os.getcwd(), "state")


def users_path() -> str:
    return os.path.join(_state_dir(), "users.json")


def _secret_path() -> str:
    return os.path.join(_state_dir(), ".auth_secret")


def _load_secret() -> bytes:
    """会话签名密钥：首次生成后存盘（0600），重启后旧会话仍有效。"""
    p = _secret_path()
    try:
        with open(p, "rb") as fh:
            b = fh.read().strip()
            if b:
                return b
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    b = secrets.token_bytes(32)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b)
        try:
            os.chmod(p, 0o600)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    return b


def _read_users() -> list[dict]:
    try:
        with open(users_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []
    return data.get("users") if isinstance(data, dict) else []


def _write_users(users: list[dict]) -> None:
    p = users_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"users": users, "updated_at": time.time()}, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(p, 0o600)
    except Exception:  # noqa: BLE001
        pass


def list_users() -> list[dict]:
    return [
        {"username": u.get("username", ""), "role": u.get("role", "viewer"),
         "created_at": u.get("created_at"), "last_login": u.get("last_login")}
        for u in _read_users()
    ]


def needs_setup() -> bool:
    """还没有任何账号 —— 首次使用要先建管理员。"""
    return not _read_users()


def get_user(username: str) -> dict | None:
    for u in _read_users():
        if u.get("username", "").lower() == (username or "").lower():
            return u
    return None


# --------------------------------------------------------------------------- 口令

def _hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _ITERS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        _ITERS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def _verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
    except Exception:  # noqa: BLE001
        return False
    got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, int(iters))
    return hmac.compare_digest(got, want)


def _valid_username(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]{2,32}", name or ""))


# --------------------------------------------------------------------------- 账号管理

def create_user(username: str, password: str, role: str = "viewer") -> dict:
    if not _valid_username(username):
        raise ValueError("账号名只能是 2~32 位字母、数字、下划线、点或短横线")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    if role not in ("admin", "viewer"):
        raise ValueError("角色只能是 admin 或 viewer")
    with _LOCK:
        if get_user(username):
            raise ValueError("这个账号名已经存在了")
        users = _read_users()
        user = {
            "username": username,
            "role": role,
            "password": _hash_password(password),
            "created_at": time.time(),
            "last_login": None,
        }
        users.append(user)
        _write_users(users)
    return {"username": username, "role": role}


def delete_user(username: str) -> None:
    with _LOCK:
        users = _read_users()
        left = [u for u in users if u.get("username", "").lower() != (username or "").lower()]
        if len(left) == len(users):
            raise ValueError("没找到这个账号")
        if not any(u.get("role") == "admin" for u in left):
            raise ValueError("至少要保留一个管理员账号")
        _write_users(left)


def change_role(username: str, role: str) -> None:
    if role not in ("admin", "viewer"):
        raise ValueError("角色只能是 admin 或 viewer")
    with _LOCK:
        users = _read_users()
        hit = None
        for u in users:
            if u.get("username", "").lower() == (username or "").lower():
                hit = u
        if not hit:
            raise ValueError("没找到这个账号")
        if hit.get("role") == "admin" and role != "admin":
            if sum(1 for u in users if u.get("role") == "admin") <= 1:
                raise ValueError("至少要保留一个管理员账号")
        hit["role"] = role
        _write_users(users)


def change_password(username: str, old_pw: str, new_pw: str) -> None:
    if len(new_pw or "") < 6:
        raise ValueError("新密码至少 6 位")
    with _LOCK:
        users = _read_users()
        hit = None
        for u in users:
            if u.get("username", "").lower() == (username or "").lower():
                hit = u
        if not hit:
            raise ValueError("没找到这个账号")
        if not _verify_password(old_pw or "", hit.get("password", "")):
            raise ValueError("原密码不对")
        hit["password"] = _hash_password(new_pw)
        _write_users(users)


def reset_password(username: str, new_pw: str) -> None:
    """管理员重设他人密码（不需要原密码）。"""
    if len(new_pw or "") < 6:
        raise ValueError("新密码至少 6 位")
    with _LOCK:
        users = _read_users()
        hit = None
        for u in users:
            if u.get("username", "").lower() == (username or "").lower():
                hit = u
        if not hit:
            raise ValueError("没找到这个账号")
        hit["password"] = _hash_password(new_pw)
        _write_users(users)


# --------------------------------------------------------------------------- 登录 / 会话

def _too_many_fails(username: str) -> bool:
    now = time.time()
    arr = [t for t in _fail_log.get(username.lower(), []) if now - t < _FAIL_WINDOW]
    _fail_log[username.lower()] = arr
    return len(arr) >= _FAIL_MAX


def _note_fail(username: str) -> None:
    arr = _fail_log.setdefault(username.lower(), [])
    arr.append(time.time())


def login(username: str, password: str) -> dict:
    """校验账号密码，成功返回 {"username","role","sid","max_age"}。"""
    if _too_many_fails(username or ""):
        raise ValueError("试错太多次了，请 5 分钟后再试")
    user = get_user(username)
    if not user or not _verify_password(password or "", user.get("password", "")):
        _note_fail(username or "")
        raise ValueError("账号或密码不对")
    with _LOCK:
        users = _read_users()
        for u in users:
            if u.get("username", "").lower() == (username or "").lower():
                u["last_login"] = time.time()
        _write_users(users)
    _fail_log.pop((username or "").lower(), None)
    return {
        "username": user.get("username", ""),
        "role": user.get("role", "viewer"),
        "sid": _make_session(user.get("username", ""), user.get("role", "viewer")),
        "max_age": _SESSION_HOURS * 3600,
    }


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64u(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _make_session(username: str, role: str) -> str:
    payload = {"u": username, "r": role, "exp": time.time() + _SESSION_HOURS * 3600}
    raw = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(_load_secret(), raw.encode(), hashlib.sha256).digest())
    return raw + "." + sig


def verify_session(sid: str) -> dict | None:
    """校验会话 cookie，返回 {"username","role"} 或 None（过期/被篡改/账号已删）。"""
    if not sid or "." not in sid:
        return None
    raw, _, sig = sid.rpartition(".")
    try:
        want = _b64u(hmac.new(_load_secret(), raw.encode(), hashlib.sha256).digest())
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(sig, want):
        return None
    try:
        p = json.loads(_unb64u(raw).decode())
    except Exception:  # noqa: BLE001
        return None
    if float(p.get("exp", 0)) < time.time():
        return None
    user = get_user(p.get("u", ""))
    if not user:
        return None  # 账号被删了，会话立即失效
    # 角色以库里为准（管理员被降级后旧会话同步失效）
    return {"username": user.get("username", ""), "role": user.get("role", "viewer")}


def cookie_header(sid: str, max_age: int) -> str:
    # HttpOnly：JS 拿不到，XSS 也偷不走；SameSite=Lax 防跨站冒用
    return ("%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
            % (_COOKIE, sid, int(max_age)))


def logout_cookie_header() -> str:
    return "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % _COOKIE
