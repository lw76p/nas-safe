#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端接入登录鉴权：cookie 会话、登录/初始化弹窗、未登录点功能按钮时提示。"""
import io
import os

ROOT = r"C:\Users\aa\WorkBuddy\2026-09-29-16-08-29\nas-safe-clone"
JS = os.path.join(ROOT, "web", "app.js")
CSS = os.path.join(ROOT, "web", "style.css")
HTML = os.path.join(ROOT, "web", "index.html")


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


patch(JS, [
    # 1) 全局当前用户
    (
        "const state = {",
        "let currentUser = null;\nconst state = {",
    ),
    # 2) api：带 cookie，遇到 401 弹登录框
    (
        """async function api(path, options = {}, timeoutMs = 30000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
      signal: ctl.signal,
    });
    let data;
    try {
      data = await res.json();
    } catch {
      throw new Error(`服务返回了非法响应 (HTTP ${res.status})`);
    }
    if (!data.ok) throw new Error(data.error || "未知错误");
    return data;
  } finally {
    clearTimeout(t);
  }
}""",
        """async function api(path, options = {}, timeoutMs = 30000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      ...options,
      signal: ctl.signal,
    });
    if (res.status === 401) {
      openLoginModal();
      throw new Error("请先登录");
    }
    if (res.status === 403) {
      toast("需要管理员权限，请先登录", "warn");
      openLoginModal();
      throw new Error("需要管理员权限");
    }
    let data;
    try {
      data = await res.json();
    } catch {
      throw new Error(`服务返回了非法响应 (HTTP ${res.status})`);
    }
    if (!data.ok) throw new Error(data.error || "未知错误");
    return data;
  } finally {
    clearTimeout(t);
  }
}""",
    ),
    # 3) boot 开头先鉴权
    (
        """async function boot() {
  try {
    if (document.getElementById("jsVer")) document.getElementById("jsVer").textContent = APP_JS_VER;
    const sys = await api("/api/system");""",
        """async function boot() {
  try {
    if (document.getElementById("jsVer")) document.getElementById("jsVer").textContent = APP_JS_VER;
    // 先确认登录/初始化：未登录时功能按钮会被服务端拒绝，前端同步弹出登录框
    const authOk = await initAuth();
    if (!authOk) return;
    const sys = await api("/api/system");""",
    ),
    # 4) 在 boot 前插入 auth 函数
    (
        """function showBanner(kind, title, body) {""",
        """/* ------------------------- 登录鉴权 ------------------------- */

function updateAuthUI() {
  const box = $("authStatus");
  const btn = $("authBtn");
  if (!box) return;
  if (currentUser) {
    box.innerHTML = \`<span class="user"><b>\${escapeHtml(currentUser.username)}</b><small>\${escapeHtml(currentUser.role === "admin" ? "管理员" : "只读")}</small></span>
      <button class="btn ghost sm" data-act="logout" id="authBtn">退出</button>\`;
    box.querySelector("[data-act='logout']").onclick = async () => {
      await api("/api/auth/logout", { method: "POST" });
      currentUser = null;
      updateAuthUI();
      toast("已退出登录");
    };
  } else if (btn) {
    btn.textContent = "登录";
    btn.onclick = openLoginModal;
  }
}

function openLoginModal() {
  const body = \`<div class="auth-form">
    <p class="muted">功能开关、保存设置、创建快照、添加设备等操作需要管理员登录。</p>
    <label>账号<input class="text-input" id="loginUser" placeholder="管理员账号" autocomplete="username"></label>
    <label>密码<input class="text-input" id="loginPass" type="password" placeholder="密码" autocomplete="current-password"></label>
    <p id="loginErr" style="color:#ff6b6b;min-height:18px;font-size:12px"></p>
  </div>\`;
  const foot = \`<button class="btn ghost" data-act="close">稍后再说</button>
    <button class="btn primary" data-act="login">登录</button>\`;
  openModal("登录", body, foot, {
    login: async () => {
      const u = $("loginUser").value.trim();
      const p = $("loginPass").value;
      try {
        const r = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username: u, password: p }) });
        currentUser = { username: r.user, role: r.role };
        updateAuthUI();
        closeModal();
        toast(\`欢迎回来，\${escapeHtml(r.user)}\`);
        await boot();
      } catch (e) {
        const el = $("loginErr");
        if (el) el.textContent = e.message;
      }
    }
  }, { stay: true });
  const pass = $("loginPass");
  if (pass) pass.onkeydown = (e) => { if (e.key === "Enter" && modalActions.login) modalActions.login(); };
}

function openSetupModal() {
  const body = \`<div class="auth-form">
    <p class="muted">首次使用，请先创建一个管理员账号。该账号拥有所有功能权限。</p>
    <label>账号<input class="text-input" id="setupUser" placeholder="2~32 位字母/数字/下划线"></label>
    <label>密码<input class="text-input" id="setupPass" type="password" placeholder="至少 6 位"></label>
    <label>确认密码<input class="text-input" id="setupPass2" type="password" placeholder="再输一次"></label>
    <p id="setupErr" style="color:#ff6b6b;min-height:18px;font-size:12px"></p>
  </div>\`;
  const foot = \`<button class="btn primary" data-act="setup">创建管理员</button>\`;
  openModal("初始化管理员账号", body, foot, {
    setup: async () => {
      const u = $("setupUser").value.trim();
      const p = $("setupPass").value;
      const p2 = $("setupPass2").value;
      const err = $("setupErr");
      if (p !== p2) { err.textContent = "两次输入的密码不一致"; return; }
      try {
        const r = await api("/api/auth/setup", { method: "POST", body: JSON.stringify({ username: u, password: p }) });
        currentUser = { username: r.user, role: r.role };
        updateAuthUI();
        closeModal();
        toast("管理员账号已创建");
        await boot();
      } catch (e) { err.textContent = e.message; }
    }
  }, { stay: true });
}

async function initAuth() {
  try {
    const r = await api("/api/auth/check");
    if (r.needs_setup) {
      openSetupModal();
      return false;
    }
    if (r.authenticated) {
      currentUser = { username: r.user, role: r.role };
      updateAuthUI();
      return true;
    }
    openLoginModal();
    return false;
  } catch (e) {
    // 401 已由 api() 自动弹出登录框
    return false;
  }
}

function showBanner(kind, title, body) {""",
    ),
])

patch(CSS, [
    (
        ".status { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 999px; background: var(--surface-2); border: 1px solid var(--border); font-size: 12.5px; color: var(--text-2); }",
        """.status { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 999px; background: var(--surface-2); border: 1px solid var(--border); font-size: 12.5px; color: var(--text-2); }
.auth-status { margin-left: 10px; }
.auth-status .user { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-2); }
.auth-form { display: flex; flex-direction: column; gap: 12px; min-width: 260px; }
.auth-form label { display: flex; flex-direction: column; gap: 5px; font-size: 13px; color: var(--text-2); }
.auth-form .text-input { width: 100%; box-sizing: border-box; }""",
    ),
])

patch(HTML, [
    (
        """  <div class="status" id="statusChip">
    <span class="dot"></span><span id="statusText">连接中</span>
  </div>
</header>""",
        """  <div class="status" id="statusChip">
    <span class="dot"></span><span id="statusText">连接中</span>
  </div>
  <div class="auth-status" id="authStatus" title="未登录时只能查看，功能按钮需登录后使用">
    <button class="btn ghost sm" data-act="login" id="authBtn">登录</button>
  </div>
</header>""",
    ),
])

print("OK")
