"""一机一码授权激活。

模型：
  1. 每台中控机器有一个「机器码」（指纹，8 位），在设置页展示给用户。
  2. 厂商用发码工具按 机器码 + 版本 + 有效期 生成激活码（HMAC 签名，离线可验）。
  3. 用户把激活码粘贴进设置页 → 服务端验签、核对机器码、检查有效期 →
     通过后写入 state/license.json，editions.get_edition() 从此读到正式版本。

安全边界（务实级别）：
  - 签名密钥存 state/license_secret.key（0600），只存中控机本地，绝不进代码仓库。
  - 没有密钥时激活接口直接不可用（免费版照常能用，不阻塞任何现有功能）。
  - 激活码与机器码绑定，拿到别的机器上无法激活。

CLI 用法（在 server 目录下）：
  python3 licensing.py init                       # 生成签名密钥（仅首次）
  python3 licensing.py show                       # 查看本机机器码 / 当前授权
  python3 licensing.py issue <版本> <机器码> [天数]  # 生成激活码（天数 0 = 永久）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import time
import uuid

import editions

_LICENSE_FILE = "license.json"
_SECRET_FILE = "license_secret.key"
_KEY_PREFIX = "NS1"
_B62 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _state_dir() -> str:
    try:
        from storage import state_dir
        return state_dir()
    except Exception:
        return os.path.join(os.getcwd(), "state")


def _license_path() -> str:
    return os.path.join(_state_dir(), _LICENSE_FILE)


def _secret_path() -> str:
    return os.path.join(_state_dir(), _SECRET_FILE)


# ---------------------------------------------------------------- 机器码

def machine_fingerprint() -> str:
    """本机指纹：主机名 + 平台 + 第一块网卡 MAC，sha256 取 8 位大写。"""
    parts = [socket.gethostname(), sys.platform]
    try:
        mac = uuid.getnode()
        parts.append(":".join(f"{(mac >> i) & 0xFF:02x}" for i in range(0, 48, 8)))
    except Exception:
        parts.append("nomac")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8].upper()


# ---------------------------------------------------------------- 密钥

def ensure_secret() -> bool:
    """确保签名密钥存在。返回 True=可用。密钥用系统随机数生成，64 字节。"""
    path = _secret_path()
    if os.path.exists(path):
        return True
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        key = os.urandom(64)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return True
    except Exception:
        return False


def _secret() -> bytes:
    path = _secret_path()
    if not os.path.exists(path):
        return b""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except Exception:
        return b""


# ---------------------------------------------------------------- 编码

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_dec(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str, secret: bytes) -> str:
    return _b64url(hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest())


# ---------------------------------------------------------------- 发码 / 验码

def issue_key(edition: str, fingerprint: str, days: int = 0) -> str:
    """生成激活码。days=0 表示永久。"""
    if edition not in editions.EDITIONS:
        raise ValueError(f"未知版本: {edition}")
    fp = fingerprint.strip().upper()
    if len(fp) != 8 or not all(c in "0123456789ABCDEF" for c in fp):
        raise ValueError("机器码应为 8 位字母数字（设置页可查）")
    now = int(time.time())
    payload = {
        "v": 1,
        "ed": edition,
        "fp": fp,
        "iat": now,
        "exp": now + days * 86400 if days and days > 0 else 0,
    }
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    secret = _secret()
    if not secret:
        raise RuntimeError("签名密钥不存在，请先在服务端执行: python3 licensing.py init")
    return f"{_KEY_PREFIX}.{payload_b64}.{_sign(payload_b64, secret)}"


def verify_key(key: str) -> tuple[bool, str, dict]:
    """验码。返回 (是否有效, 提示, payload)。只验签名/格式/有效期，不核对机器码。"""
    k = (key or "").strip()
    k = "".join(k.split())  # 去掉粘贴时混入的空白
    parts = k.split(".")
    if len(parts) != 3 or parts[0] != _KEY_PREFIX:
        return False, "激活码格式不对，请完整复制后重试", {}
    payload_b64, sig = parts[1], parts[2]
    secret = _secret()
    if not secret:
        return False, "服务端缺少签名密钥，激活功能不可用（请联系厂商）", {}
    if not hmac.compare_digest(sig, _sign(payload_b64, secret)):
        return False, "激活码无效（校验不通过）", {}
    try:
        payload = json.loads(_b64url_dec(payload_b64).decode("utf-8"))
    except Exception:
        return False, "激活码内容损坏", {}
    exp = int(payload.get("exp") or 0)
    if exp and exp < time.time():
        return False, "激活码已过期，请联系厂商续期", {}
    ed = payload.get("ed") or ""
    if ed not in editions.EDITIONS:
        return False, "激活码里的版本不存在", {}
    return True, "", payload


def activate(key: str) -> tuple[bool, str]:
    """激活：验码 + 核对本机机器码 + 落盘。"""
    ok, msg, payload = verify_key(key)
    if not ok:
        return False, msg
    fp = machine_fingerprint()
    if (payload.get("fp") or "").upper() != fp:
        return False, "这个激活码是给别的机器的（机器码不符）。请在本机生成订单时填对本机机器码"
    record = {
        "key": (key or "").strip(),
        "edition": payload.get("ed"),
        "fingerprint": fp,
        "activated_at": int(time.time()),
        "expires_at": int(payload.get("exp") or 0),
    }
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        fd = os.open(_license_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        return False, f"激活信息写入失败: {exc}"
    return True, f"激活成功，已升级为{editions.limits(payload.get('ed')).get('label', payload.get('ed'))}"


def deactivate() -> tuple[bool, str]:
    """取消本机授权（回落到免费版）。"""
    try:
        if os.path.exists(_license_path()):
            os.remove(_license_path())
        return True, "已取消授权，回到免费版"
    except Exception as exc:
        return False, f"取消失败: {exc}"


def info() -> dict:
    """给设置页 / API 用的完整授权信息。"""
    ed = editions.get_edition()
    lim = editions.limits(ed)
    fp = machine_fingerprint()
    record = {}
    try:
        with open(_license_path(), encoding="utf-8") as fh:
            record = json.load(fh)
    except Exception:
        pass
    exp = int(record.get("expires_at") or 0)
    out = {
        "fingerprint": fp,
        "edition": ed,
        "label": lim.get("label", ""),
        "licensed": bool(record),
        "max_devices": lim.get("max_devices", editions.UNLIMITED),
        "can_migrate": bool(lim.get("migrate")),
        "max_snapshots": lim.get("max_snapshots", editions.UNLIMITED),
    }
    if record:
        out["activated_at"] = record.get("activated_at")
        out["expires_at"] = exp
        out["permanent"] = exp == 0
    return out


# ---------------------------------------------------------------- CLI

def _cli(argv: list) -> int:
    if not argv or argv[0] in ("show", "info"):
        inf = info()
        print(f"机器码: {inf['fingerprint']}")
        print(f"当前版本: {inf['label']} ({inf['edition']})")
        print(f"已授权: {'是' if inf['licensed'] else '否'}")
        if inf.get("expires_at"):
            import datetime
            print("有效期至: " + datetime.datetime.fromtimestamp(
                inf["expires_at"]).strftime("%Y-%m-%d %H:%M"))
        elif inf.get("permanent"):
            print("有效期至: 永久")
        return 0
    if argv[0] == "init":
        print("OK" if ensure_secret() else "FAIL")
        return 0
    if argv[0] == "issue":
        if len(argv) < 3:
            print("用法: python3 licensing.py issue <free|home|business> <机器码> [天数]")
            return 2
        days = int(argv[3]) if len(argv) > 3 else 0
        print(issue_key(argv[1], argv[2], days))
        return 0
    if argv[0] == "verify":
        ok, msg, payload = verify_key(argv[1] if len(argv) > 1 else "")
        print(("OK " + json.dumps(payload, ensure_ascii=False)) if ok else ("BAD " + msg))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
