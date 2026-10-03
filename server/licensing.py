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

try:
    import rsa as _rsa
except Exception:  # noqa: BLE001
    _rsa = None

import editions

_LICENSE_FILE = "license.json"
_SECRET_FILE = "license_secret.key"
_KEY_PREFIX = "NS1"
_B62 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# 升级价格（元）：默认 8/18，state/prices.json 可覆盖（发卡后台以后可同步写这份文件）
_PRICES_FILE = "prices.json"
_DEFAULT_PRICES = {"home": 8, "business": 18}


def get_prices() -> dict:
    """升级价格：默认 8/18，state/prices.json 可覆盖。"""
    prices = dict(_DEFAULT_PRICES)
    try:
        with open(os.path.join(_state_dir(), _PRICES_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        for k in ("home", "business"):
            if isinstance(data.get(k), (int, float)) and data[k] >= 0:
                prices[k] = data[k]
    except Exception:
        pass
    return prices


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


# 升级激活采用 RSA 非对称签名：私钥只在官方签发环境（NASSAFE_LICENSE_SIGN_KEY），
# 公钥硬编码下发到所有客户端，客户端本地独立验签，不再信任云端返回值，消除 MITM 伪造风险。
_KEY_PREFIX2 = "NS2"
_PUBKEY_PEM = """-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEAmtLJ5sM5pi5FRH3hnQgzuo855sEOVC7ikKw1odo+ti4G8IMfuaDb
eSzCP65O8pCFdW+thEQwOm8/5csVOoyJMYbEq23udMyoSTwRvs0bFOD4Fh3BLxGX
JYr0imK3ONZxqOKFogqR4JZXiZ/5IIRGSPGx6AjQ8Vf+FaG/Qy45QE2VEnwWavHY
QtD/ncGM08VegFjI+8ru2J3vRJGqY5JaZUYnvQbNzdqNcD5P2eO3B1JZ49u6g2A1
POKoxHJnTFpBjDn5wJKbU09VcwbgXYe+el3dugdkQENB8iXgJj/i8jjTHT7T4AzI
YrH57K/ix9Grv5Cr/LOb5uWc51LQFAE1IwIDAQAB
-----END RSA PUBLIC KEY-----
"""


def _pubkey():
    if _rsa is None:
        return None
    try:
        return _rsa.PublicKey.load_pkcs1(_PUBKEY_PEM.encode("utf-8"))
    except Exception:
        return None


def _privkey():
    if _rsa is None:
        return None
    # 1) 固定文件路径（签发机专用：绕过 systemd EnvironmentFile 不支持多行 PEM 值的限制）
    fixed = os.path.join(_state_dir(), "license_sign_key.pem")
    if os.path.exists(fixed):
        try:
            return _rsa.PrivateKey.load_pkcs1(open(fixed, "r").read().strip().encode("utf-8"))
        except Exception:
            pass
    # 2) 环境变量直接给 PEM
    raw = os.environ.get("NASSAFE_LICENSE_SIGN_KEY", "").strip()
    # 3) 环境变量给文件路径指针
    if not raw:
        fp = os.environ.get("NASSAFE_LICENSE_SIGN_KEY_FILE", "").strip()
        if fp and os.path.exists(fp):
            try:
                raw = open(fp, "r").read().strip()
            except Exception:
                raw = ""
    if not raw:
        return None
    try:
        return _rsa.PrivateKey.load_pkcs1(raw.encode("utf-8"))
    except Exception:
        return None


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
    # master 密钥：官方签发环境用 NASSAFE_MASTER_SECRET(hex) 注入，全网一致；
    # 未配置时回落本机 state 文件（兼容旧自签模式，用户机器将走云端验签）。
    hx = os.environ.get("NASSAFE_MASTER_SECRET", "").strip()
    if hx:
        try:
            return bytes.fromhex(hx)
        except Exception:
            pass
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
    """生成激活码。days=0 表示永久。优先用 RSA 私钥签 NS2；无私钥时回落旧 HMAC(NS1)。"""
    if edition not in editions.EDITIONS:
        raise ValueError(f"未知版本: {edition}")
    fp = fingerprint.strip().upper()
    if len(fp) != 8 or not all(c in "0123456789ABCDEF" for c in fp):
        raise ValueError("机器码应为 8 位字母数字（设置页可查）")
    now = int(time.time())
    payload = {
        "v": 2,
        "ed": edition,
        "fp": fp,
        "iat": now,
        "exp": now + days * 86400 if days and days > 0 else 0,
    }
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    priv = _privkey()
    if priv is not None:
        sig = _b64url(_rsa.sign(payload_b64.encode("ascii"), priv, "SHA-256"))
        return f"{_KEY_PREFIX2}.{payload_b64}.{sig}"
    # 回落：旧 HMAC（需 _secret，仅官方旧机）
    secret = _secret()
    if not secret:
        raise RuntimeError("没有可用的签发密钥（需 NASSAFE_LICENSE_SIGN_KEY 或旧 license_secret.key）")
    return f"{_KEY_PREFIX}.{payload_b64}.{_sign(payload_b64, secret)}"


def _is_official_issuer() -> bool:
    """官方签发机器：配置了 master 密钥，可本地验签。"""
    return bool(os.environ.get("NASSAFE_MASTER_SECRET", "").strip())


def verify_key(key: str) -> tuple[bool, str, dict]:
    """验码。返回 (是否有效, 提示, payload)。支持 NS2(RSA 公钥本地验) 与 NS1(HMAC 兼容)。
    只验签名/格式/有效期，不核对机器码（机器码由 activate 核对）。"""
    k = (key or "").strip()
    k = "".join(k.split())  # 去掉粘贴时混入的空白
    parts = k.split(".")
    if len(parts) != 3:
        return False, "激活码格式不对，请完整复制后重试", {}
    prefix, payload_b64, sig = parts[0], parts[1], parts[2]
    if prefix == _KEY_PREFIX2:
        # RSA 非对称：任何客户端用内置公钥本地验，无需网络/私钥，MITM 无法伪造
        pub = _pubkey()
        if pub is None or _rsa is None:
            return False, "本机缺少 RSA 验签组件，激活不可用", {}
        try:
            _rsa.verify(payload_b64.encode("ascii"), _b64url_dec(sig), pub)
        except Exception:
            return False, "激活码无效（RSA 校验不通过）", {}
    elif prefix == _KEY_PREFIX:
        # 旧 HMAC 格式：需 _secret，仅官方机可验
        secret = _secret()
        if not secret:
            return False, "本机缺少签名密钥（旧格式需官方机验证）", {}
        if not hmac.compare_digest(sig, _sign(payload_b64, secret)):
            return False, "激活码无效（校验不通过）", {}
    else:
        return False, "不支持的激活码版本", {}
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
    """激活：本地验签（NS1/NS2 均支持，RSA 公钥本地验，不依赖网络/云端）+ 核对本机机器码 + 落盘。"""
    ok, msg, payload = verify_key((key or "").strip())
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
