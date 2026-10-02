#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接入 auth.py：登录会话 + 权限分级，未登录只能看静态页，所有写操作/功能开关需管理员。"""
import io
import os

ROOT = r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nas-safe-clone"
APP = os.path.join(ROOT, "server", "app.py")
DEPLOY = os.path.join(ROOT, "scripts", "deploy_server_to_cloud.py")


def patch(path, pairs):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        s = f.read()
    crlf = s.count("\r\n") * 2 > s.count("\n")
    s = s.replace("\r\n", "\n")
    for old, new in pairs:
        if s.count(old) != 1:
            raise AssertionError((path, s.count(old), old[:90]))
        s = s.replace(old, new)
    if crlf:
        s = s.replace("\n", "\r\n")
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    print("patched", os.path.basename(path))


patch(APP, [
    # 1) 导入 auth
    (
        "import migrate  # noqa: E402  换机迁移（配置包导出 / 导入 / 路径映射 / 能力降级）\n",
        "import migrate  # noqa: E402  换机迁移（配置包导出 / 导入 / 路径映射 / 能力降级）\nimport auth      # noqa: E402  账号/会话/权限分级\n",
    ),
    # 2) 替换鉴权块
    (
        """    # -- Web 访问控制（Basic Auth）----------------------------------------
    WEB_USER = os.environ.get("NASSAFE_WEB_USER", "nassafe")
    WEB_PASS = os.environ.get("NASSAFE_WEB_PASS", "nassafe-dev-8848")

    def _require_auth(self) -> bool:
        expect = self.WEB_PASS
        if not expect:
            return True  # 未配置密码视为关闭（默认已有开发密码，不应走到这）
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            self._send_401()
            return False
        try:
            import base64
            decoded = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
            user, _, pw = decoded.partition(":")
        except Exception:  # noqa: BLE001
            self._send_401()
            return False
        if user == self.WEB_USER and pw == expect:
            return True
        self._send_401()
        return False

    def _send_401(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="NAS Safe"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()""",
        """    # -- Web 访问控制（登录会话 + Basic Auth 兜底）---------------------------
    WEB_USER = os.environ.get("NASSAFE_WEB_USER", "nassafe")
    WEB_PASS = os.environ.get("NASSAFE_WEB_PASS", "nassafe-dev-8848")

    def _get_cookie(self, name: str) -> str:
        for c in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = c.strip().partition("=")
            if k == name:
                return v
        return ""

    def _current_user(self) -> dict | None:
        \"\"\"先读会话 cookie，失败再回退 Basic Auth（给脚本/扫描用）。\"\"\"
        sid = self._get_cookie(auth._COOKIE)
        user = auth.verify_session(sid) if sid else None
        if user:
            return user
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                decoded = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
                u, _, pw = decoded.partition(":")
                if u == self.WEB_USER and pw == self.WEB_PASS:
                    return {"username": u, "role": "admin"}
            except Exception:  # noqa: BLE001
                pass
        return None

    def _require_auth(self) -> bool:
        if self._current_user():
            return True
        self._send_401()
        return False

    def _require_admin(self) -> bool:
        user = self._current_user()
        if user and user.get("role") == "admin":
            return True
        self._send_json({"ok": False, "error": "需要管理员权限，请先登录"}, 403)
        return False

    def _set_session_cookie(self, sid: str, max_age: int) -> None:
        self.send_header("Set-Cookie", auth.cookie_header(sid, max_age))

    def _send_401(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("WWW-Authenticate", 'Basic realm="NAS Safe"')
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "error": "请先登录"}).encode("utf-8"))""",
    ),
    # 3) GET 入口：公开接口 + 认证路由
    (
        """    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        try:""",
        """    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        # 公开接口：健康检查、认证相关、静态文件；其余都需要登录
        public_api = ("/api/health", "/api/auth/setup", "/api/auth/check", "/api/auth/logout")
        needs_auth = route.startswith("/api/") and route not in public_api

        try:
            if route == "/api/auth/setup":
                self._send_json({"ok": True, "needs_setup": auth.needs_setup()})
                return
            if route == "/api/auth/check":
                user = self._current_user()
                self._send_json({"ok": True,
                                 "needs_setup": auth.needs_setup(),
                                 "authenticated": bool(user),
                                 "user": user.get("username") if user else None,
                                 "role": user.get("role") if user else None})
                return
            if route == "/api/auth/logout":
                self.send_response(200)
                self.send_header("Set-Cookie", auth.logout_cookie_header())
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
                return

            if needs_auth and not self._require_auth():
                return""",
    ),
    # 4) POST 入口：认证路由 + 管理员权限
    (
        """    def do_POST(self):
        if not self._require_auth():
            return
        route = urlparse(self.path).path

        try:
            payload = self._read_json()""",
        """    def do_POST(self):
        route = urlparse(self.path).path

        try:
            if route == "/api/auth/setup":
                payload = self._read_json()
                if not auth.needs_setup():
                    raise StorageError("已经初始化过，请直接登录")
                username = (payload.get("username") or "").strip()
                password = (payload.get("password") or "").strip()
                auth.create_user(username, password, "admin")
                sess = auth.login(username, password)
                self.send_response(200)
                self._set_session_cookie(sess["sid"], sess["max_age"])
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "user": sess["username"], "role": sess["role"]}).encode("utf-8"))
                return
            if route == "/api/auth/login":
                payload = self._read_json()
                if auth.needs_setup():
                    raise StorageError("请先创建管理员账号")
                username = (payload.get("username") or "").strip()
                password = payload.get("password") or ""
                sess = auth.login(username, password)
                self.send_response(200)
                self._set_session_cookie(sess["sid"], sess["max_age"])
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "user": sess["username"], "role": sess["role"]}).encode("utf-8"))
                return
            if route == "/api/auth/logout":
                self.send_response(200)
                self.send_header("Set-Cookie", auth.logout_cookie_header())
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
                return

            # 除认证接口外，所有 POST 都需要管理员权限（功能开关/写操作）
            if not self._require_admin():
                return
            payload = self._read_json()""",
    ),
    # 5) main：首次启动自动创建管理员（若环境变量给了密码）
    (
        """    server = ThreadingHTTPServer((HOST, PORT), Handler)
    # 后台自动推送线程：定期扫描新告警/变动并分发到已配置通道""",
        """    # 若还没账号且环境变量给了初始密码，自动创建管理员（首次部署/演示环境）
    if auth.needs_setup():
        init_user = os.environ.get("NASSAFE_WEB_USER", "nassafe")
        init_pass = os.environ.get("NASSAFE_WEB_PASS", "")
        if init_pass:
            try:
                auth.create_user(init_user, init_pass, "admin")
                print(f"  初始账号  : {init_user}（已自动创建）")
            except Exception as exc:
                print(f"  [提示] 自动创建初始账号失败: {exc}")
        else:
            print("  [提示] 尚未创建管理员账号，首次打开页面会引导初始化")

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    # 后台自动推送线程：定期扫描新告警/变动并分发到已配置通道""",
    ),
])

# 6) 部署脚本补 auth.py、anomalies.py、daily_report.py
patch(DEPLOY, [
    (
        '''FILES = [
    "brands.py",
    "storage.py",
    "metrics.py",
    "snapshot_vss.py",
    "snapshot_apfs.py",
    "snapshot_rsync.py",
    "app.py",
    "devices.py",
    "netscan.py",
]''',
        '''FILES = [
    "brands.py",
    "storage.py",
    "metrics.py",
    "anomalies.py",
    "daily_report.py",
    "snapshot_vss.py",
    "snapshot_apfs.py",
    "snapshot_rsync.py",
    "app.py",
    "devices.py",
    "netscan.py",
    "auth.py",
]''',
    ),
])

print("OK")
