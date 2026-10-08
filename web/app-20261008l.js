/* TS Safe — 前端逻辑 */
const APP_JS_VER = "20261008l";

const $ = (id) => document.getElementById(id);

let currentUser = null;
let needsSetup = false;            // 后端是否还未初始化管理员账号（首次需注册）
let pendingProtectedView = null;   // 未登录时点过受保护入口，登录成功后跳转到此视图
// 受保护视图：涉及读取系统目录 / 删除文件 / 改配置，必须登录后才可进入
const PROTECTED_VIEWS = ["dups", "junk", "migrate", "settings"];
// 这三个是「工具」，从「功能设置」菜单里启动（点击后跳到对应全屏视图）
const TOOL_VIEWS = ["dups", "junk", "migrate"];
const state = {
  system: null,
  volumes: [],
  activeVolume: null,
  snapshots: [],
  snapDevice: "local",   // 快照页当前看哪台设备："local"=本机，其它=联机设备 id
  snapVolumes: [],       // 当前设备的卷列表（本机用 state.volumes，联机设备单独存）
  snapReadonly: false,   // 联机设备的快照只能看、不能动
  browseSnapshot: null,
  browsePath: null,
  browseStack: [],
  restoreDir: localStorage.getItem("nassafe.restoreDir") || "",
  tamperAlerts: [],   // v1 基线对比法（30s 轮询）
  deepAlerts: [],     // v2 内容完整性 + v3 勒索行为（主动巡检后写入）
  alertsData: null,   // 告警信息页：本机 + 联机设备的告警与历史汇总
  autoMonitor: false, // 自动持续监控开关
  autoMonitorTimer: null,
  dupTarget: "local", // 重复文件清理：本机或联机设备 id
  junkTarget: "local", // 磁盘清理：本机或联机设备 id
  remoteDevices: [],   // 联机设备列表（来自 /api/devices/manage）
};

// 把本机接口路径改写成「经控制台代理到指定联机设备」的路径。
// 例：targetApi("/api/duplicates/scan", "dev-123")
//   -> "/api/devices/dev-123/proxy/api/duplicates/scan"
// 设备端（完整版 TS Safe）会信任控制台下发的 center_link 令牌，从而放行写操作。
function targetApi(basePath, target) {
  if (!target || target === "local") return basePath;
  return `/api/devices/${encodeURIComponent(target)}/proxy${basePath}`;
}

// 拉取联机设备列表并填充重复文件/磁盘清理页的设备选择器
async function refreshDeviceOptions() {
  if (state.remoteDevices.length) { fillDeviceSelects(); return; }
  try {
    const data = await api("/api/devices/manage", {}, 15000);
    const devs = (data && data.devices) || [];
    state.remoteDevices = devs.filter((d) => d.type !== "local");
  } catch {
    state.remoteDevices = state.remoteDevices || [];
  }
  fillDeviceSelects();
}

function fillDeviceSelects() {
  for (const selId of ["dupDevice", "junkDevice"]) {
    const sel = $(selId);
    if (!sel) continue;
    const stateTarget = selId === "dupDevice" ? state.dupTarget : state.junkTarget;
    const cur = sel.value;
    sel.innerHTML = '<option value="local">本机（运行控制台的这台机器）</option>' +
      state.remoteDevices.map((d) => {
        const off = d.status !== "online";
        const tag = d.full_server ? "完整服务端" : (d.agent_status === "installed" ? "代理" : "未装代理");
        const label = `${escapeHtml(d.name)}（${escapeHtml(tag)}）${off ? " · 离线" : ""}`;
        return `<option value="${escapeAttr(d.id)}"${off ? " disabled" : ""}>${label}</option>`;
      }).join("");
    const want = stateTarget && stateTarget !== "local" ? stateTarget : cur;
    if (want && [...sel.options].some((o) => o.value === want && !o.disabled)) sel.value = want;
    else if (cur && [...sel.options].some((o) => o.value === cur && !o.disabled)) sel.value = cur;
  }
  updateTargetHints();
}

function updateTargetHints() {
  const hint = (selId, elId) => {
    const sel = $(selId), el = $(elId);
    if (!sel || !el) return;
    const d = state.remoteDevices.find((x) => x.id === sel.value);
    if (!d) { el.textContent = ""; return; }
    if (d.status !== "online") { el.textContent = "（设备离线，暂不可用）"; return; }
    if (!d.full_server && d.agent_status !== "installed") {
      el.textContent = "（该设备为轻量代理模式，需装完整版才能使用清理功能）";
    } else { el.textContent = ""; }
  };
  hint("dupDevice", "dupTargetHint");
  hint("junkDevice", "junkTargetHint");
}

function bindDeviceSelectors() {
  const dsel = $("dupDevice"), jsel = $("junkDevice");
  if (dsel) dsel.onchange = async (e) => {
    state.dupTarget = e.target.value; updateTargetHints();
    $("dupRoot").value = ""; dupSetStatus("");
    if (state.dupTarget !== "local") await refreshDeviceOptions();
    loadDupReport();
  };
  if (jsel) jsel.onchange = async (e) => {
    state.junkTarget = e.target.value; updateTargetHints();
    junkSetStatus("");
    if (state.junkTarget !== "local") await refreshDeviceOptions();
    loadJunkReport();
  };
}

// 快照是否为威联通（QNAP）远程后端：这类快照没有本地实体路径，
// 浏览/取回必须走 snapshot_id + volume_id 通道。
function isQnapSnap(snap) {
  return snap && (snap.backend === "qnap" || snap.fs_type === "qnap");
}

/* ------------------------- 网络 ------------------------- */

async function api(path, options = {}, timeoutMs = 60000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    let res;
    try {
      res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        ...options,
        signal: ctl.signal,
      });
    } catch (fetchErr) {
      // 网络层中断（超时/连接挂起/DNS 失败）会抛 AbortError，浏览器消息是
      // "signal is aborted without reason" 这种技术黑话，对普通用户毫无意义——
      // 统一翻译成人话，避免把底层异常直接弹给用户。
      if (fetchErr && (fetchErr.name === "AbortError" || /aborted/i.test(fetchErr.message || ""))) {
        throw new Error("请求超时了，请稍后重试，或检查网络是否通畅");
      }
      throw fetchErr;
    }
    // 登录/找回/注册接口自己的 401 不能触发「重开登录弹窗」——否则密码输错时
    // 弹窗被重建、输入被清空、错误提示被吞，看起来像「点了没反应」
    const isAuthCall = path.startsWith("/api/auth/login") || path.startsWith("/api/auth/reset")
      || path.startsWith("/api/auth/forgot") || path.startsWith("/api/auth/setup");
    if (res.status === 401 && !isAuthCall) {
      // 未登录（currentUser 为空）时多为后台轮询/初始加载命中鉴权接口，静默失败即可，
      // 不弹登录框——让用户自己点右上角「登录」；仅当曾经登录过、会话失效才弹框重登
      if (currentUser) openLoginModal();
      throw new Error("请先登录");
    }
    if (res.status === 403) {
      if (currentUser) {
        toast("需要管理员权限，请先登录", "warn");
        openLoginModal();
      } else {
        toast("请先登录后再操作", "warn");
      }
      throw new Error("需要管理员权限");
    }
    const respClone = res.clone();
    let data;
    try {
      data = await res.json();
    } catch {
      // 响应体不是合法 JSON：把原始内容读出来，给出可定位的提示，而不是笼统的「非法响应」
      let raw = "";
      try { raw = (await respClone.text()).slice(0, 300); } catch {}
      const trimmed = (raw || "").trim();
      if (trimmed.startsWith("<")) {
        throw new Error("服务端返回了网页而非数据（可能会话已失效被重定向），请刷新页面后重新登录");
      }
      if (!trimmed) {
        throw new Error(`服务端未返回任何内容（HTTP ${res.status}），请刷新页面后重试`);
      }
      throw new Error(`服务端返回了无法解析的内容（HTTP ${res.status}），请刷新页面后重试`);
    }
    if (!data.ok) {
      // 服务端卡点（设备数/迁移等）返回 upgrade:true——这正是展示升级页的时机
      if (data.upgrade && !window.__suppressUpgradeJump && typeof openUpgradeModal === "function") openUpgradeModal();
      throw new Error(data.error || "未知错误");
    }
    return data;
  } finally {
    clearTimeout(t);
  }
}

/* ------------------------- 提示 ------------------------- */

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  $("toastRoot").appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function setStatus(text, kind = "") {
  $("statusText").textContent = text;
  $("statusChip").className = "status" + (kind ? " " + kind : "");
}

/* ------------------------- 初始化 ------------------------- */

async function boot() {
  try {
    if (document.getElementById("jsVer")) document.getElementById("jsVer").textContent = APP_JS_VER;
    // 看数据免登录：只拦截「首次未初始化」(initAuth 会弹注册框)；游客态继续加载公开数据
    await initAuth();
    if (needsSetup) return;
    const sys = await api("/api/system");
    state.system = sys.system;
    const s = sys.system;
    // 顶栏不再外露系统细节（内核/文件系统等黑话），保持产品化文案（index.html 静态文案）

    setStatus("已连接", "ok");

    if (s.warnings && s.warnings.length) {
      showBanner("warn", "使用提示", s.warnings.join(" "));
    }

    await loadVolumes();
  } catch (err) {
    setStatus("连接失败", "err");
    showBanner("error", "无法连接到 TS Safe 服务", err.message);
    return;
  }
  // 以下为「非核心」恢复逻辑：即使某子视图（控制台/时间轴等）加载失败，
  // 也不该让整页退回「连接失败」——核心数据（系统+存储单元）已就绪即视为已连接。
  try {
    // 启动即恢复上次选中的卷（不强制跳转视图）：刷新后勒索行为扫描等
    // 依赖 state.activeVolume 的功能才不会报「请先选择一个卷」。
    const savedVol = localStorage.getItem("nassafe_volume");
    if (savedVol && !state.activeVolume) {
      const v = state.volumes.find(
        (x) => x.mountpoint === savedVol || String(x.id) === savedVol
      );
      if (v) {
        state.activeVolume = v;
        const wp = $("watchPaths");
        if (wp && wp.value.trim() === "") wp.value = v.mountpoint;
        const sel = $("tlVolumeSel");
        if (sel) sel.value = v.mountpoint ?? String(v.id);
      }
    }
    // 强刷后恢复上次所在视图的数据：默认进入设备控制台
    onEnterView(localStorage.getItem("nassafe_view") || "console");
    // 绑定重复文件/磁盘清理页的「目标设备」选择器，并拉取联机设备列表填充
    try { bindDeviceSelectors(); await refreshDeviceOptions(); } catch (e) {}
  } catch (e) {
    toast("部分数据加载较慢或失败：" + (e.message || e), "warn");
  }
}

/* ------------------------- 登录鉴权 ------------------------- */

function updateAuthUI() {
  const box = $("authStatus");
  const btn = $("authBtn");
  if (!box) return;
  if (currentUser) {
    box.innerHTML = `<span class="user"><b>${escapeHtml(currentUser.username)}</b><small>${escapeHtml(currentUser.role === "admin" ? "管理员" : "只读")}</small></span>
      <button class="btn ghost sm" data-act="logout" id="authBtn">退出</button>`;
    box.querySelector("[data-act='logout']").onclick = async () => {
      try {
        await api("/api/auth/logout", { method: "POST" });
      } catch (e) {
        // 即便登出接口异常也照常刷新：本地会话必然清掉，刷新后由服务端权威状态决定
        toast(e.message || "退出请求失败，仍会刷新页面", "warn");
      }
      // 先立即把右上角改成「登录」并清掉本地登录态，不等刷新就有可见反馈（避免「点退出没反应」误感）
      currentUser = null;
      updateAuthUI();
      toast("已退出登录");
      // 再整页刷新，由服务端权威状态兜底
      location.reload();
    };
  } else if (btn) {
    btn.textContent = "登录";
    btn.onclick = openLoginModal;
  }
}

// 受保护入口：未登录时先要求注册（首次）或登录，验证通过后再进入目标视图
function requireLoginFor(view) {
  if (currentUser) { showView(view); return; }
  pendingProtectedView = view;
  if (needsSetup) openSetupModal();
  else openLoginModal();
}

function openLoginModal() {
  const body = `<div class="auth-form">
    <p class="muted">功能开关、保存设置、创建快照、添加设备等操作需要管理员登录。</p>
    <label>账号<input class="text-input" id="loginUser" placeholder="管理员账号" autocomplete="username"></label>
    <label>密码<input class="text-input" id="loginPass" type="password" placeholder="密码" autocomplete="current-password"></label>
    <p id="loginErr" style="color:#ff6b6b;min-height:18px;font-size:12px"></p>
    <p class="auth-links"><a href="javascript:void(0)" id="forgotLink">忘记密码？</a></p>
  </div>`;
  const foot = `<button class="btn ghost" data-act="close">稍后再说</button>
    <button class="btn primary" data-act="login">登录</button>`;
  openModal("登录", body, foot, {
    login: async () => {
      const u = $("loginUser").value.trim();
      const p = $("loginPass").value;
      try {
        const r = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username: u, password: p }) });
        if (!r || !r.user) throw new Error("登录成功但服务端未返回账号信息，请刷新页面");
        // 登录成功：先把本地状态改成「已登录」并关弹窗，立即有反馈；
        // 再整页刷新，由服务端会话权威决定「已登录」状态，避免前端内存态出错
        currentUser = { username: r.user, role: r.role };
        updateAuthUI();
        closeModal();
        location.reload();
        return;
      } catch (e) {
        const el = $("loginErr");
        if (el) el.textContent = e.message;
        else toast(e.message || "登录失败", "warn");
      }
    }
  }, { stay: true });
  // 直接把登录按钮的 onclick 绑到 login 动作上，避免只依赖 modalRoot 委托（防止任何 data-act 选择器歧义导致点不动）
  const lb = document.querySelector('#modalFoot button[data-act="login"]');
  if (lb) lb.onclick = modalActions.login;
  const pass = $("loginPass");
  if (pass) pass.onkeydown = (e) => { if (e.key === "Enter" && modalActions.login) modalActions.login(); };
  const fl = $("forgotLink");
  if (fl) fl.onclick = () => openForgotModal();
}

/* 找回密码：账号 + 注册邮箱 → 邮件收 6 位验证码 → 验证码 + 新密码 */
function openForgotModal() {
  openModal(
    "找回密码",
    `<div class="auth-form">
      <p class="muted">输入账号和注册时留的找回邮箱，我们会发一个 6 位验证码到邮箱（10 分钟内有效）。</p>
      <label>账号<input class="text-input" id="fgUser" placeholder="管理员账号" autocomplete="username"></label>
      <label>注册时的邮箱<input class="text-input" id="fgMail" type="email" placeholder="如 you@example.com"></label>
      <p id="fgErr" style="color:#ff6b6b;min-height:18px;font-size:12px"></p>
    </div>`,
    `<button class="btn ghost" data-act="backlogin">返回登录</button>
     <button class="btn primary" data-act="sendcode">📧 发送验证码</button>`,
    {
      backlogin: () => { closeModal(); openLoginModal(); },
      sendcode: async () => {
        const u = $("fgUser").value.trim();
        const mail = $("fgMail").value.trim();
        const err = $("fgErr");
        const btn = document.querySelector('button[data-act="sendcode"]');
        if (!u || !mail) { err.textContent = "请填写账号和邮箱"; toast("请填写账号和邮箱", "warn"); return; }
        if (btn) { btn.disabled = true; btn.dataset.origText = btn.innerHTML; btn.innerHTML = "📧 发送中..."; }
        try {
          const r = await api("/api/auth/forgot", { method: "POST", body: JSON.stringify({ username: u, email: mail }) }, 90000);
          toast(`验证码已发到 ${escapeHtml(r.sent_to)}，10 分钟内有效`, "ok");
          openForgotStep2(u);
        } catch (e) {
          err.textContent = e.message;
          toast(e.message || "发送失败", "warn");
        } finally {
          if (btn) { btn.disabled = false; btn.innerHTML = btn.dataset.origText || "📧 发送验证码"; }
        }
      },
    }, { stay: true });
}

function openForgotStep2(username) {
  openModal(
    "找回密码 · 第 2 步",
    `<div class="auth-form">
      <p class="muted">验证码已发到你的邮箱，请查收（可能在垃圾邮件里）。</p>
      <label>邮箱验证码<input class="text-input" id="fgCode" placeholder="6 位数字" inputmode="numeric" maxlength="6"></label>
      <label>新密码<input class="text-input" id="fgNew" type="password" placeholder="至少 6 位" autocomplete="new-password"></label>
      <label>确认新密码<input class="text-input" id="fgNew2" type="password" placeholder="再输一次" autocomplete="new-password"></label>
      <p id="fgErr2" style="color:#ff6b6b;min-height:18px;font-size:12px"></p>
    </div>`,
    `<button class="btn ghost" data-act="backlogin">返回登录</button>
     <button class="btn primary" data-act="doreset">重置密码</button>`,
    {
      backlogin: () => { closeModal(); openLoginModal(); },
      doreset: async () => {
        const code = $("fgCode").value.trim();
        const p = $("fgNew").value;
        const p2 = $("fgNew2").value;
        const err = $("fgErr2");
        if (p !== p2) { err.textContent = "两次输入的新密码不一致"; return; }
        try {
          await api("/api/auth/reset", { method: "POST", body: JSON.stringify({ username, code, new_password: p }) }, 90000);
          toast("密码已重置，用新密码登录吧", "ok");
          closeModal();
          openLoginModal();
          const el = $("loginUser");
          if (el) el.value = username;
        } catch (e) { err.textContent = e.message; }
      },
    }, { stay: true });
  const code = $("fgCode");
  if (code) code.onkeydown = (e) => { if (e.key === "Enter" && modalActions.doreset) modalActions.doreset(); };
}

function openSetupModal() {
  const body = `<div class="auth-form">
    <p class="muted">欢迎使用 TS Safe 完整版。先注册一个管理员账号，注册登录后才能使用快照、防勒索、设备管控等全部功能（不注册只能看页面）。注册完会直接带你去设置页做初次配置。</p>
    <div class="setup-note">💡 <b>如果你是想把这台设备交回原来的「总控台」统一管理：</b>在本机注册完、设好基础设置后，回到原来的总控台，点「＋ 添加设备 / 扫描」，本机会以 <b>TS Safe 服务端</b> 身份出现（不再是只能监控的端点），迁移 / 快照 / 重复文件 / 磁盘清理 / 日报 等全部功能都可直接使用。</div>
    <label>账号<input class="text-input" id="setupUser" placeholder="2~32 位字母/数字/下划线" autocomplete="username"></label>
    <label>密码<input class="text-input" id="setupPass" type="password" placeholder="至少 6 位" autocomplete="new-password"></label>
    <label>确认密码<input class="text-input" id="setupPass2" type="password" placeholder="再输一次" autocomplete="new-password"></label>
    <label>找回邮箱<input class="text-input" id="setupMail" type="email" placeholder="忘记密码时用它找回（建议填写）"></label>
    <p id="setupErr" style="color:#ff6b6b;min-height:18px;font-size:12px"></p>
  </div>`;
  const foot = `<button class="btn primary" data-act="setup">注册并登录</button>`;
  openModal("注册管理员账号", body, foot, {
    setup: async () => {
      const u = $("setupUser").value.trim();
      const p = $("setupPass").value;
      const p2 = $("setupPass2").value;
      const mail = $("setupMail").value.trim();
      const err = $("setupErr");
      if (p !== p2) { err.textContent = "两次输入的密码不一致"; return; }
      try {
        const r = await api("/api/auth/setup", { method: "POST", body: JSON.stringify({ username: u, password: p, email: mail }) });
        currentUser = { username: r.user, role: r.role };
        updateAuthUI();
        closeModal();
        toast(`注册成功，欢迎 ${escapeHtml(r.user)}！先完成基础设置`);
        await boot();
        if (pendingProtectedView) {
          const pv = pendingProtectedView; pendingProtectedView = null;
          showView(pv);
        } else {
          showView("settings");
        }
      } catch (e) { err.textContent = e.message; }
    }
  }, { stay: true });
  const pass = $("setupPass2");
  if (pass) pass.onkeydown = (e) => { if (e.key === "Enter" && modalActions.setup) modalActions.setup(); };
}

async function initAuth() {
  try {
    const r = await api("/api/auth/check");
    needsSetup = !!r.needs_setup;
    if (r.needs_setup) {
      openSetupModal();
      return false;
    }
    if (r.authenticated) {
      currentUser = { username: r.user, role: r.role };
      updateAuthUI();
      return true;
    }
    // 一键登录：URL 带 ?login=账号:密码 时自动登录（演示站/帮助新人免输凭证），用后即从地址栏清除
    const lp = new URLSearchParams(location.search).get("login");
    if (lp && lp.includes(":")) {
      const au = lp.slice(0, lp.indexOf(":"));
      const ap = lp.slice(lp.indexOf(":") + 1);
      try {
        const lr = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username: au, password: ap }) });
        currentUser = { username: lr.user, role: lr.role };
        updateAuthUI();
        history.replaceState(null, "", location.pathname);
        toast(`已自动登录：${escapeHtml(lr.user)}`, "ok");
        return true;
      } catch (e) {
        openLoginModal();
        const el = document.getElementById("loginErr");
        if (el) el.textContent = `自动登录失败：${e.message}（若提示「尝试太频繁」请等 5 分钟再试）`;
        return false;
      }
    }
    // 不再一开页面就弹登录框：只绑定右上角「登录」按钮，用户主动点才弹窗
    updateAuthUI();
    return false;
  } catch (e) {
    // 401 已由 api() 自动弹出登录框
    updateAuthUI();  // 即使接口异常也要让右上角登录按钮可点
    return false;
  }
}

function showBanner(kind, title, body) {
  const el = $("alertBanner");
  el.hidden = false;
  el.className = "alert-banner" + (kind === "warn" ? " warn" : "");
  $("alertTitle").textContent = title;
  $("alertBody").textContent = body;
}

/* ------------------------- 存储单元 ------------------------- */

async function loadVolumes() {
  const box = $("volumeList");
  box.innerHTML = `<p class="muted"><span class="spinner"></span>正在扫描存储单元…</p>`;

  const data = await api("/api/volumes", {}, 60000);
  state.volumes = data.volumes;

  // 时间轴页的卷切换下拉框：选项 = 所有存储卷
  // 快照页正看着某台联机设备时，下拉里装的是那台设备的卷，别被本机卷覆盖回去
  const sel = $("tlVolumeSel");
  if (sel && (!state.snapDevice || state.snapDevice === "local")) {
    sel.innerHTML = state.volumes
      .map((v) => `<option value="${escapeHtml(v.mountpoint ?? String(v.id))}">${escapeHtml(v.name)}</option>`)
      .join("");
    if (state.activeVolume) sel.value = state.activeVolume.mountpoint ?? String(state.activeVolume.id);
  }

  // 用户当前停在时间轴页但还没选过卷（如刷新后），卷列表到位后自动补选
  if (!state.activeVolume && (localStorage.getItem("nassafe_view") || "home") === "snapshots") {
    autoSelectVolume();
  }

  if (!state.volumes.length) {
    box.innerHTML = `<p class="muted">
      未发现可快照的存储单元。请确认存储池使用的是 btrfs 或 ZFS 文件系统。<br>
      如果是 ext4，需要重建存储池为 btrfs 才能使用快照功能。
    </p>`;
    return;
  }

  const unprotected = state.volumes.filter((v) => !v.protected);
  if (unprotected.length) {
    showBanner(
      "error",
      `有 ${unprotected.length} 个存储单元没有任何快照保护`,
      "这些数据目前无法回滚。建议点击对应单元，立即创建第一张快照。"
    );
  } else {
    $("alertBanner").hidden = true;
  }

  box.innerHTML = "";
  for (const vol of state.volumes) {
    const card = document.createElement("div");
    card.className = "volume-card";
    if (state.activeVolume && state.activeVolume.mountpoint === vol.mountpoint) {
      card.classList.add("active");
    }

    const badge = vol.protected
      ? `<span class="badge ok">${vol.snapshot_count} 张快照</span>`
      : `<span class="badge risk">无保护</span>`;

    const latest = vol.latest_snapshot
      ? `<div class="vol-path">最近 ${formatWhen(vol.latest_snapshot)}</div>`
      : `<div class="vol-path">尚未创建快照</div>`;

    card.innerHTML = `
      <span class="vol-fs ${vol.fs_type === "zfs" ? "zfs" : ""}">${vol.fs_type}</span>
      <div class="vol-main">
        <div class="vol-name">${escapeHtml(vol.name)}</div>
        <div class="vol-path">${escapeHtml(vol.mountpoint)}</div>
      </div>
      <div class="vol-meta">${badge}</div>
    `;
    card.onclick = () => selectVolume(vol);
    box.appendChild(card);
  }

  updateOverview();
}

/* ------------------------- 一眼概览卡 ------------------------- */

// 顶部概览：把关键状态浓缩成 4 张彩色卡，进页面一眼看清全局。
function updateOverview() {
  const vols = state.volumes || [];
  const units = vols.length;
  const snaps = vols.reduce((n, v) => n + (v.snapshot_count || 0), 0);
  const unprotected = vols.filter((v) => !v.protected).length;

  $("ovUnits").textContent = units || "0";
  $("ovSnaps").textContent = snaps || "0";

  // 防护状态卡：颜色跟随真实状态（绿=全部已保护；琥珀=有待保护项），避免固定琥珀色被误读为告警
  const guard = $("ovGuard");
  const guardCard = guard.closest(".ov-card");
  if (!units) {
    guard.textContent = "—"; guard.className = "ov-num";
    guardCard.className = "ov-card zone-guard";
  } else if (unprotected) {
    guard.textContent = `${unprotected} 个待保护`;
    guard.className = "ov-num is-bad";
    guardCard.className = "ov-card zone-guard";
  } else {
    guard.textContent = "健康";
    guard.className = "ov-num is-ok";
    guardCard.className = "ov-card zone-monitor";
  }

  const mon = $("ovMonitor");
  const monCard = mon.closest(".ov-card");
  if (state.autoMonitor) {
    mon.textContent = "运行中";
    mon.className = "ov-num is-ok";
    if (monCard) monCard.classList.remove("is-off");
  } else {
    mon.textContent = "未开启";
    mon.className = "ov-num";
    if (monCard) monCard.classList.add("is-off");
  }
}

/* ------------------------- 系统仪表盘 ------------------------- */

// 按能力渲染硬件指标卡片；拿不到的字段自动隐藏（跨品牌分层适配）。
function fmtBps(v) {
  if (v == null) return "--";
  if (v < 1024) return v + " B/s";
  if (v < 1048576) return (v / 1024).toFixed(1) + " KB/s";
  return (v / 1048576).toFixed(1) + " MB/s";
}
function fmtKB(kb) {
  if (kb >= 1048576) return (kb / 1048576).toFixed(1) + " GB";
  if (kb >= 1024) return (kb / 1024).toFixed(0) + " MB";
  return kb + " KB";
}
function donut(label, percent, color) {
  const p = Math.max(0, Math.min(100, percent || 0));
  const r = 34, c = 2 * Math.PI * r;
  return `
    <div class="donut">
      <svg viewBox="0 0 84 84" width="66" height="66">
        <circle cx="42" cy="42" r="${r}" fill="none" stroke="var(--surface-2)" stroke-width="9"/>
        <circle cx="42" cy="42" r="${r}" fill="none" stroke="${color}" stroke-width="9"
          stroke-linecap="round" stroke-dasharray="${(p / 100 * c).toFixed(1)} ${c.toFixed(1)}"
          transform="rotate(-90 42 42)"/>
        <text x="42" y="47" text-anchor="middle" fill="var(--text)" font-size="16" font-weight="700">${Math.round(p)}%</text>
      </svg>
      <span class="donut-label">${label}</span>
    </div>`;
}

async function loadMetrics(force) {
  const body = $("metricsBody");
  if (!body) return;
  try {
    const data = await api("/api/system/metrics" + (force ? "?force=1" : ""));
    if (!data.ok) throw new Error(data.error || "采集失败");
    renderMetrics(data.metrics);
  } catch (e) {
    body.innerHTML = `<p class="muted">暂时无法读取硬件指标：${escapeHtml(e.message)}（核心防勒索功能不受影响）</p>`;
  }
}

// 仪表盘交互状态：15s 轮询重渲染后保持用户所选的网卡/存储卷
let selIface = null;  // null=自动选流量最大的物理网卡
let selVol = null;    // null=取第一个卷

function renderMetrics(m) {
  const body = $("metricsBody");
  const cap = m.capabilities || {};

  // 状态评级：0=正常(绿) 1=注意(琥珀) 2=异常(红)。每张卡独立评级。
  const grade = (v, warn, bad) => (v == null ? 0 : v >= bad ? 2 : v >= warn ? 1 : 0);
  const GRADE = { 0: ["ok", "正常"], 1: ["warn", "注意"], 2: ["bad", "异常"] };
  const BIG = { 0: ["ok", "良好"], 1: ["warn", "注意"], 2: ["bad", "异常"] };
  const dot = (g) => `<span class="status-dot ${GRADE[g][0]}"></span>`;
  const cardHead = (title, g, extra = "") => {
    const [cls, txt] = GRADE[g];
    return `<h4><span>${title}</span><span class="mc-pill ${cls}">${txt}</span>${extra}</h4>`;
  };

  // 卡1：系统运行状况 —— 大字评级 + 主机名 + 运行时长
  const up = m.uptime || {};
  const gTemp = cap.cpu_temp ? grade(m.cpu.temp_c, 80, 90) : 0;
  const gLoad = grade(m.cpu && m.cpu.load1 != null ? m.cpu.load1 : null, 8, 16);
  const g1 = Math.max(gTemp, gLoad);
  const worst1 = gTemp >= gLoad
    ? (gTemp ? (gTemp === 2 ? "CPU 温度过高" : "CPU 温度偏高") : "")
    : (gLoad ? "系统负载过高" : "");
  const card1 = `
    <div class="metric-card metric-card-sm mc-${GRADE[g1][0]}">
      ${cardHead("系统运行状况", g1)}
      <div class="grade-big ${GRADE[g1][0]}">${BIG[g1][1]}</div>
      <div class="metric-row"><span class="status-dot ${GRADE[g1][0]}"></span>
        <b>${escapeHtml(m.hostname || "NAS")}</b>
        ${worst1 ? `<span class="mc-reason">${worst1}</span>` : ""}</div>
      <div class="metric-kv"><span>运行时间</span><b>${up.days || 0} 天 ${up.hours || 0} 小时 ${up.minutes || 0} 分</b></div>
    </div>`;

  // 卡2：硬件信息 —— 温度/风扇/负载清单，逐项状态灯；一样都没有就整卡隐藏
  const fanRows = m.fan || {};
  const hwRow = (label, val, g) =>
    `<div class="metric-kv hw"><span>${label}</span><span class="hw-val">${val}${dot(g)}</span></div>`;
  const hwRows = [
    cap.cpu_temp ? hwRow("CPU 温度", `${m.cpu.temp_c}°C`, gTemp) : "",
    cap.fan && fanRows.cpu_fan_rpm ? hwRow("CPU 风扇", `${fanRows.cpu_fan_rpm} RPM`, 0) : "",
    cap.fan && fanRows.fan_rpm ? hwRow("系统风扇", `${fanRows.fan_rpm} RPM`, 0) : "",
    m.cpu && m.cpu.load1 != null ? hwRow("系统负载", `${m.cpu.load1}`, gLoad) : "",
  ].join("");
  const card2 = hwRows ? `
    <div class="metric-card metric-card-sm mc-${GRADE[g1][0]}">
      ${cardHead("硬件信息", g1)}
      ${hwRows}
    </div>` : "";

  // 卡3：资源监控 —— CPU/RAM 环形图 + 网卡下拉切换
  const gCpu = cap.cpu_percent ? grade(m.cpu.percent, 80, 95) : 0;
  const gMem = cap.mem ? grade(m.mem.percent, 80, 90) : 0;
  const g2 = Math.max(gCpu, gMem);
  const hasNet = m.net && m.net.ifaces && m.net.ifaces.length;
  let netBlock = "";
  if (hasNet) {
    // 只显示实体网卡：白名单认物理口，其余（docker/br-/lxcbr/veth/bond 等虚拟口）全部排除
    // 白名单为空时逐级回退，保证任何系统都不会出现空下拉
    const VIRT = /^(veth|docker|br-|lxcbr|virbr|tun|tap|sit|zt|wg)/;
    const PHYS = /^(eth|en|em|wl|lan|xgbe|sfc|mlx|bcm|bond)/;
    let pool = m.net.ifaces.filter((i) => PHYS.test(i.iface) && !VIRT.test(i.iface));
    if (!pool.length) pool = m.net.ifaces.filter((i) => !VIRT.test(i.iface));
    if (!pool.length) pool = m.net.ifaces;
    pool.sort((a, b) => a.iface.localeCompare(b.iface, undefined, { numeric: true, sensitivity: "base" }));
    const ifaceName = (n) => {
      if (/^eth\d+$/.test(n)) return `网卡 ${Number(n.slice(3)) + 1}`;
      if (/^en/.test(n)) return `网口 ${n}`;
      if (/^wl/.test(n)) return `无线 ${n}`;
      if (/^bond/.test(n)) return `聚合网卡 ${n}`;
      return n;
    };
    if (!selIface || !pool.some((i) => i.iface === selIface)) {
      selIface = pool.reduce((a, b) =>
        ((b.rx_bps || 0) + (b.tx_bps || 0) > (a.rx_bps || 0) + (a.tx_bps || 0) ? b : a), pool[0]).iface;
    }
    const cur = pool.find((i) => i.iface === selIface) || pool[0];
    const opts = pool.map((i) => {
      const cn = ifaceName(i.iface);
      return `<option value="${escapeHtml(i.iface)}" ${i.iface === cur.iface ? "selected" : ""}>${
        cn === i.iface ? escapeHtml(cn) : `${escapeHtml(cn)}（${escapeHtml(i.iface)}）`}</option>`;
    }).join("");
    netBlock = `
      <div class="net-sel-row">
        <select id="ifaceSel" class="mc-select">${opts}</select>
        <div class="metric-kv net"><span>↓ ${fmtBps(cur.rx_bps)}</span><span>↑ ${fmtBps(cur.tx_bps)}</span></div>
      </div>`;
  }
  const card3 = `
    <div class="metric-card mc-${GRADE[g2][0]}">
      ${cardHead("资源监控", g2)}
      <div class="donut-row">
        ${cap.cpu_percent ? donut("CPU", m.cpu.percent, gCpu === 2 ? "var(--red)" : gCpu === 1 ? "var(--amber)" : "var(--z-storage)") : ""}
        ${cap.mem ? donut("RAM", m.mem.percent, gMem === 2 ? "var(--red)" : gMem === 1 ? "var(--amber)" : "var(--z-monitor)") : ""}
        ${!cap.cpu_percent && !cap.mem ? `<p class="muted">不可用</p>` : ""}
      </div>
      ${netBlock || `<p class="muted">未检测到网卡</p>`}
    </div>`;

  // 卡4：存储 —— 卷下拉切换 + 大环形占用图 + 趋势提示；脏数据卷过滤
  const vols = (m.volumes || []).filter((v) => v && v.mount && v.total_kb > 0);
  let card4 = "";
  if (vols.length) {
    const g3 = vols.reduce((g, v) => Math.max(g, grade(v.percent, 75, 90)), 0);
    // 卷名中文化：优先用 /api/volumes 的真实名称（QNAP 的 mountpoint 是卷 ID，
    // 路径 /share/CACHEDEVn_DATA 中的 n 即 ID）；其他品牌路径直接匹配
    const volLabel = (v) => {
      const byId = (id) => state.volumes.find((x) => String(x.mountpoint) === id || String(x.volume_id) === id);
      const m = String(v.mount).match(/CACHEDEV(\d+)_DATA/i);
      if (m) {
        const hit = byId(m[1]);
        if (hit) return hit.name;
        return `存储卷 ${m[1]}`;
      }
      const known = state.volumes.find((x) => x.mountpoint === v.mount);
      if (known) return known.name;
      return v.mount.split("/").pop() || v.mount;
    };
    if (!selVol || !vols.some((v) => v.mount === selVol)) selVol = vols[0].mount;
    const cur = vols.find((v) => v.mount === selVol) || vols[0];
    const pct = Math.max(0, Math.min(100, Number(cur.percent) || 0));
    const gSel = grade(cur.percent, 75, 90);
    const trend = (m.trends || []).find((t) => t.mount === cur.mount);
    const trendHtml = trend
      ? (trend.days_to_full
        ? `<p class="muted t-warn" style="margin:8px 0 0">📈 按最近增长速度，预计约 <b>${trend.days_to_full} 天后存满</b>，可考虑清理或扩容</p>`
        : `<p class="muted" style="margin:8px 0 0">📈 在缓慢增长（当前 ${trend.percent}%），暂不用担心</p>`)
      : "";
    const volOpts = vols.map((v) =>
      `<option value="${escapeHtml(v.mount)}" ${v.mount === cur.mount ? "selected" : ""}>${escapeHtml(volLabel(v))}</option>`).join("");
    const avail = Math.max(0, cur.available_kb != null ? cur.available_kb : cur.total_kb - cur.used_kb);
    card4 = `
      <div class="metric-card metric-card-lg mc-${GRADE[g3][0]}">
        ${cardHead("存储", g3)}
        <select id="volSel" class="mc-select" style="margin-bottom:8px">${volOpts}</select>
        <div class="storage-flex">
          <div class="donut-row">${donut(volLabel(cur), pct, gSel === 2 ? "var(--red)" : gSel === 1 ? "var(--amber)" : "var(--z-storage)")}</div>
          <div class="storage-info">
            <div class="metric-kv"><span>已使用</span><b>${fmtKB(cur.used_kb)}</b></div>
            <div class="metric-kv"><span>可用</span><b>${fmtKB(avail)}</b></div>
            <div class="metric-kv"><span>总容量</span><b>${fmtKB(cur.total_kb)}</b></div>
          </div>
        </div>
        ${trendHtml}
      </div>`;
  }

  // 卡5：磁盘 —— 独占整行，汇总"n/n 正常" + 按类型分组横向铺开；脏数据整行过滤
  const disks = (m.disks || []).filter((d) => d && d.name);
  const gDisk = (d) => d.temp_c == null ? 0 : grade(d.temp_c, 50, 60);
  // SMART 健康（跨品牌，取不到时缺省）—— 与磁盘按 name 对应
  const smart = (m.smart && m.smart.available) ? m.smart : null;
  const smartMap = {};
  (smart ? (smart.disks || []) : []).forEach((s) => { smartMap[s.name] = s; });
  const smartWorst = smart ? (smart.worst || 0) : 0;
  // SMART 四级严重程度：ok 正常绿 / warn 注意黄 / danger 警告橙 / bad 异常红
  const smartTier = (s) => {
    if (!s || !s.health || s.health === "unknown") return "na";
    if (s.health === "fail") return "bad";
    if (s.health === "warn") {
      const critical = (s.reallocated || 0) > 0 || (s.pending || 0) > 0 || (s.uncorrectable || 0) > 0 ||
                       (s.critical_warning || 0) > 0 || (s.media_errors || 0) > 0 ||
                       (s.percentage_used != null && s.percentage_used >= 95);
      return critical ? "danger" : "warn";
    }
    return "ok";
  };
  const smartCounts = { ok: 0, warn: 0, danger: 0, bad: 0, na: 0 };
  const TIER_ORDER = ["ok", "warn", "danger", "bad"];
  let smartWorstTier = "ok";
  if (smart) {
    (smart.disks || []).forEach((s) => {
      const tier = smartTier(s);
      smartCounts[tier]++;
      if (TIER_ORDER.indexOf(tier) > TIER_ORDER.indexOf(smartWorstTier)) smartWorstTier = tier;
    });
  }
  const g4 = Math.max(
    disks.reduce((g, d) => Math.max(g, gDisk(d)), 0),
    smartWorst
  );
  const okCount = disks.filter((d) => gDisk(d) === 0).length;
  const ioTxt = (d) => {
    if (d.read_bps == null && d.write_bps == null) return "";
    return `<span class="chip-io">↓${fmtBps(d.read_bps)} ↑${fmtBps(d.write_bps)}</span>`;
  };
  const chip = (d, label) => {
    const hasSize = d.size_b != null && d.size_b > 0;
    const tb = hasSize ? `<span>${(d.size_b / 1024**4).toFixed(1)}TB</span>` : "";
    const gd = gDisk(d);
    const temp = d.temp_c != null ? `<span class="chip-temp ${GRADE[gd][0]}">${d.temp_c}°C</span>` : "";
    // SMART 健康小徽标与整块盘变色（绿/黄/橙/红）
    const sm = smartMap[d.name];
    const smTier = smartTier(sm);
    let smBadge = "";
    if (smTier !== "na") {
      const txtMap = { ok: "良", warn: "注意", danger: "警告", bad: "异常" };
      smBadge = `<span class="chip-smart ${smTier}">${txtMap[smTier]}</span>`;
    }
    const smCls = smTier !== "na" ? ` ${smTier}` : "";
    const title = d.model ? ` title="${escapeHtml(d.model)}${hasSize ? " " + (d.size_b / 1024**4).toFixed(1) + "TB" : ""}"` : "";
    return `
      <div class="disk-chip${smCls}"${title}>
        <b>${label}</b>
        ${tb}
        ${temp}
        ${smBadge}
        ${ioTxt(d)}
      </div>`;
  };
  const nvme = disks.filter((d) => d.name.startsWith("nvme"));
  const sata = disks.filter((d) => !d.name.startsWith("nvme"));
  let diskGroups = "";
  if (nvme.length) {
    diskGroups += `<div class="bay-group"><span class="bay-label">固态硬盘（M.2）</span><div class="disk-grid">${
      nvme.map((d, i) => chip(d, `固态 ${i + 1}`)).join("")}</div></div>`;
  }
  if (sata.length) {
    diskGroups += `<div class="bay-group"><span class="bay-label">机械硬盘（SATA）</span><div class="disk-grid">${
      sata.map((d, i) => chip(d, `硬盘 ${i + 1}`)).join("")}</div></div>`;
  }
  // 磁盘柜图标：随最差盘状态变色（绿=正常 / 琥珀=偏热 / 红=过热）
  const bayIco = (g) => {
    const c = g === 2 ? "var(--red)" : g === 1 ? "var(--amber)" : "var(--green)";
    return `<svg class="bay-ico" viewBox="0 0 24 24" width="19" height="19" fill="none"
      stroke="${c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="16" rx="2"/>
      <line x1="6.5" y1="8.5" x2="17.5" y2="8.5"/>
      <line x1="6.5" y1="12.5" x2="17.5" y2="12.5"/>
      <line x1="6.5" y1="16.5" x2="11.5" y2="16.5"/>
      <circle cx="15.6" cy="16.5" r="1.4" fill="${c}" stroke="none"/>
    </svg>`;
  };
  // SMART 健康总览横条：绿/黄/橙/红四级，异常时红色脉冲告警
  let smartBanner = "";
  if (smart) {
    const txtMap = { ok: "正常", warn: "注意", danger: "警告", bad: "异常" };
    const bcls = smartWorstTier;
    const blabel = txtMap[smartWorstTier];
    const alertText = smartCounts.bad
      ? `⚠ 有 ${smartCounts.bad} 块硬盘状态异常，建议立即备份并排查`
      : smartCounts.danger
      ? `⚠ 有 ${smartCounts.danger} 块硬盘存在严重告警，请关注`
      : smartCounts.warn
      ? `⚠ 有 ${smartCounts.warn} 块硬盘处于注意状态`
      : "";
    smartBanner = `
      <div class="smart-banner ${bcls}">
        <div class="sb-main">
          <span class="sb-title">SMART 硬盘健康</span>
          <span class="sb-grade">${blabel}</span>
        </div>
        <div class="sb-stats">
          ${smartCounts.ok ? `<span class="sb-ok">${smartCounts.ok} 良好</span>` : ""}
          ${smartCounts.warn ? `<span class="sb-warn">${smartCounts.warn} 注意</span>` : ""}
          ${smartCounts.danger ? `<span class="sb-danger">${smartCounts.danger} 警告</span>` : ""}
          ${smartCounts.bad ? `<span class="sb-bad">${smartCounts.bad} 异常</span>` : ""}
          ${smartCounts.na ? `<span class="sb-na">${smartCounts.na} 未检</span>` : ""}
        </div>
        ${alertText ? `<div class="sb-alert">${alertText}</div>` : ""}
      </div>`;
  }
  // SMART 脚注：未取到时给白话开启提示（不报错）
  let smartFoot = "";
  if (!smart && m.smart) {
    smartFoot = `<div class="smart-foot"><span class="muted">硬盘健康检测需装 Smartmontools（威联通应用中心免费）即开启</span></div>`;
  }
  const card5 = disks.length ? `
    <div class="metric-card metric-card-wide mc-${GRADE[g4][0]}">
      ${smartBanner}
      ${diskGroups}
      ${smartFoot}
    </div>` : "";

  body.innerHTML = card1 + card2 + card3 + card4 + card5;
  checkAnomalies(m);  // AI 主动异常提醒：渲染后即检测（15s 轮询自动跟进）

  // 交互绑定：切换网卡/存储卷时局部重渲染（不重新请求，15s 轮询照常）
  const is = $("ifaceSel");
  if (is) is.onchange = () => { selIface = is.value; renderMetrics(m); };
  const vs = $("volSel");
  if (vs) vs.onchange = () => { selVol = vs.value; renderMetrics(m); };
}

/* ------------------------- AI 主动异常提醒 ------------------------- */
// 页面出现异常（磁盘过热 / CPU 高温高负载 / 卷快满 / 存满趋势 / 快照告警）时：
// ① toast + 顶部横幅主动提醒（横幅可点击）② 打开异常面板逐项查看
// ③ 一键「AI 修复方案」—— AI 只给建议不代操作（产品红线），由用户确认后自行处理。

let lastMetrics = null;
let knownAnoms = new Set();  // 本次会话已提醒过的异常（异常消失会自动解除，复发会再提醒）
const dismissedAnoms = new Set(JSON.parse(localStorage.getItem("nassafe_anom_dismissed") || "[]"));

function volNameShort(mount) {
  const m = String(mount).match(/CACHEDEV(\d+)_DATA/i);
  if (m) {
    const hit = state.volumes.find((x) => String(x.mountpoint) === m[1] || String(x.volume_id) === m[1]);
    if (hit) return hit.name;
    return `存储卷 ${m[1]}`;
  }
  return String(mount).split("/").pop() || String(mount);
}

// 从最新指标里收集异常项：{key, sev(1注意/2异常), title, detail, view, q(AI提问)}
function collectAnomalies(m) {
  const list = [];
  const cap = m.capabilities || {};
  const push = (key, sev, title, detail, q) =>
    list.push({ key, sev, title, detail, view: "home", q });

  if (cap.cpu_temp && m.cpu && m.cpu.temp_c != null) {
    if (m.cpu.temp_c >= 90)
      push("cpu-temp", 2, `CPU 温度过高（${m.cpu.temp_c}°C）`, "长时间如此可能降频或损伤硬件", `NAS 的 CPU 温度到了 ${m.cpu.temp_c}°C，请分析可能原因并给出处理步骤`);
    else if (m.cpu.temp_c >= 80)
      push("cpu-temp", 1, `CPU 温度偏高（${m.cpu.temp_c}°C）`, "建议关注散热与负载", `NAS 的 CPU 温度 ${m.cpu.temp_c}°C 偏高，可能原因和处理建议？`);
  }
  if (m.cpu && m.cpu.load1 != null && m.cpu.load1 >= 8) {
    push("cpu-load", m.cpu.load1 >= 16 ? 2 : 1, `系统负载过高（${m.cpu.load1}）`, "可能有任务占满 CPU", `NAS 系统负载到了 ${m.cpu.load1}，请给出排查思路（哪些进程/服务可能占用）`);
  }
  // 磁盘温度（与仪表盘同样的分组命名：固态/硬盘 n）
  const groups = [
    [(m.disks || []).filter((d) => d && d.name && d.name.startsWith("nvme")), "固态"],
    [(m.disks || []).filter((d) => d && d.name && !d.name.startsWith("nvme")), "硬盘"],
  ];
  for (const [arr, cn] of groups) {
    arr.forEach((d, i) => {
      if (d.temp_c == null) return;
      if (d.temp_c >= 60)
        push(`disk-${d.name}`, 2, `${cn} ${i + 1} 过热（${d.temp_c}°C）`, "高温会缩短硬盘寿命，请尽快处理", `NAS 的${cn} ${i + 1}（${d.model || d.name}）温度 ${d.temp_c}°C 过热，可能原因和修复方案？`);
      else if (d.temp_c >= 50)
        push(`disk-${d.name}`, 1, `${cn} ${i + 1} 温度偏高（${d.temp_c}°C）`, "建议改善散热", `NAS 的${cn} ${i + 1} 温度 ${d.temp_c}°C 偏高，有什么改善建议？`);
    });
  }
  // 卷空间：与服务端 metrics.space_alerts 同一套双门槛（比例 + 绝对剩余），避免空盘误报
  (m.volumes || []).forEach((v) => {
    if (!v || v.total_kb <= 0) return;
    if (v.total_kb < 16 * 1024 * 1024) return;                 // 系统内部小卷不参与
    const mt = String(v.mount || "");
    if (["/boot", "/dev", "/proc", "/sys", "/run", "/snap", "/mnt/snapshot"].some((p) => mt === p || mt.startsWith(p + "/"))) return;
    const freeGb = (v.total_kb - v.used_kb) / 1024 / 1024;
    const freeTxt = freeGb.toFixed(1) + "G";
    if (v.percent >= 92 && freeGb < 3)
      push(`vol-${mt}`, 2, `「${volNameShort(mt)}」空间即将用尽（已用 ${v.percent}%，剩 ${freeTxt}）`, "空间满会影响快照与正常使用", `存储卷「${volNameShort(mt)}」已用 ${v.percent}%，请给出清理和扩容建议`);
    else if (v.percent >= 80 && freeGb < 10)
      push(`vol-${mt}`, 1, `「${volNameShort(mt)}」空间偏紧（已用 ${v.percent}%，剩 ${freeTxt}）`, "建议关注增长", `存储卷「${volNameShort(mt)}」已用 ${v.percent}%，有哪些安全的清理建议？`);
  });
  (m.trends || []).forEach((t) => {
    // 只提示「30 天内且已用七成以上」，远的、宽的都不提
    if (t.days_to_full && t.days_to_full <= 30 && t.percent >= 70)
      push(`trend-${t.mount}`, 1, `「${volNameShort(t.mount)}」预计 ${t.days_to_full} 天后存满`, "按最近增长速度推算", `存储卷「${volNameShort(t.mount)}」按当前速度约 ${t.days_to_full} 天后存满，如何处理？`);
  });
  return list;
}

// 当前全部异常 = 指标类 + 快照/勒索告警类
function currentAnomalies() {
  const list = lastMetrics ? collectAnomalies(lastMetrics) : [];
  (state.tamperAlerts || []).concat(state.deepAlerts || []).forEach((a) => {
    if (!a || (a.level !== "critical" && a.level !== "warn")) return;
    list.push({
      key: `alert-${a.title}`, sev: a.level === "critical" ? 2 : 1,
      title: a.title || "快照保护异常", detail: a.detail || "", view: "monitor",
      q: `TS Safe 报告异常：${a.title || ""}${a.detail ? "：" + a.detail : ""}。请分析原因并给出排查与修复步骤`,
    });
  });
  return list;
}

// 每次 15s 轮询渲染后调用：新异常弹 toast，横幅内容随异常集合更新
function checkAnomalies(m) {
  lastMetrics = m;
  const cur = collectAnomalies(m);
  const keys = new Set(cur.map((a) => a.key));
  [...knownAnoms].forEach((k) => { if (!keys.has(k)) knownAnoms.delete(k); }); // 恢复正常后允许复发再提醒
  const fresh = cur.filter((a) => !knownAnoms.has(a.key) && !dismissedAnoms.has(a.key));
  if (fresh.length) {
    fresh.forEach((a) => knownAnoms.add(a.key));
    toast(`⚠ 检测到 ${fresh.length} 项异常，点击顶部提示查看`, "err");
    pushAnomalyAlert(fresh); // 主动提醒：本地模型→电脑弹窗；云端/已配通道→微信等最快通道
  }
  renderBanners(); // 横幅统一由此刷新（勒索告警优先，其次硬件/容量异常）
}

// 主动提醒分发：① 电脑弹窗（系统通知，需网页保持运行）② 远端通道（微信/邮件，按最快自动优选）
async function pushAnomalyAlert(list) {
  const worst = list.slice().sort((a, b) => b.sev - a.sev)[0];
  const summary = list.map((a) => a.title).join("；");
  const wantDesk = localStorage.getItem("nassafe_desk_notify") === "1";
  const wantRemote = true; // 远端推送（微信/邮件）为默认行为，多通道自动选最快

  if (wantDesk && "Notification" in window && Notification.permission === "granted") {
    let body = summary;
    // AI 供应商 = 本地模型时，用本地 AI 把异常写成一句人话提醒（数据不出本机）
    const provider = ($("aiProvider") && $("aiProvider").value) || "";
    if (provider === "ollama") { // 默认规则：本地 AI 时自动生成人话文案
      try {
        const d = await routeAI(
          `请用一句通俗中文（30 字以内）提醒电脑前的用户：${summary}。只输出提醒文案，不要解释。`,
          "/api/ai/ask"
        );
        if (d && d.text) body = d.text.trim().slice(0, 60);
      } catch (e) { /* AI 不可用时退回原始摘要 */ }
    }
    try {
      const n = new Notification("TS Safe 异常提醒", { body, tag: "nassafe-anom", requireInteraction: true });
      n.onclick = () => { window.focus(); openAnomalyModal(); };
    } catch (e) { /* 部分浏览器限制非 HTTPS 通知，忽略 */ }
  }

  if (wantRemote) {
    try {
      await api("/api/notify/alert", {
        method: "POST",
        body: JSON.stringify({
          title: "TS Safe 异常提醒",
          detail: summary,
          level: worst.sev >= 2 ? "critical" : "warn",
        }),
      });
    } catch (e) { /* 推送失败不打断页面 */ }
  }
}

// 无勒索告警时，用硬件/容量类异常横幅顶置（点击打开异常面板）
function showAnomalyBanner() {
  const list = currentAnomalies().filter((a) => !dismissedAnoms.has(a.key));
  if (!list.length) { $("alertBanner").hidden = true; return; }
  const critical = list.some((a) => a.sev >= 2);
  showBanner(critical ? "error" : "warn",
    critical ? "⚠ 检测到异常，建议尽快处理" : "检测到需关注的异常",
    list.slice(0, 3).map((a) => "• " + a.title).join("；") +
    (list.length > 3 ? ` 等 ${list.length} 项` : "") + "（点击查看详情与 AI 修复方案）");
}

// ---------------------------------------------------------------------------
// 告警信息页面：本机 + 每台联机设备的当前告警与历史记录
// ---------------------------------------------------------------------------
let alertsRefreshTimer = null;
let alertsDeviceFilter = "all";   // all | <device id>
let alertsStatusFilter = "all";   // all | active | resolved

function _alertTypeLabel(t) {
  if (t === "tamper") return "快照保护";
  if (t === "integrity") return "内容完整性";
  if (t === "behavior") return "勒索迹象";
  return "硬件/容量";
}

async function loadAlertsPage() {
  try {
    const data = await api("/api/alerts/summary");
    state.alertsData = data;
    renderAlertsPage(data);
  } catch (e) {
    const el = $("alertsList");
    if (el) el.innerHTML = `<p class="muted">加载失败：${escapeHtml(e.message || e)}</p>`;
  }
  // 进入页面后每 30s 自动刷新（离开时在 showView 中清除）
  if (!alertsRefreshTimer) {
    alertsRefreshTimer = setInterval(() => {
      if (localStorage.getItem("nassafe_view") === "alerts") loadAlertsPage();
    }, 30000);
  }
}

function renderAlertsPage(data) {
  const devices = (data && data.devices) || [];
  const filtersEl = $("alertsFilters");
  const listEl = $("alertsList");
  const summaryEl = $("alertsSummary");
  if (!filtersEl || !listEl) return;

  // 汇总：全部设备的活跃/已恢复计数
  let totalActive = 0, totalResolved = 0, onlineDevs = 0;
  devices.forEach((d) => {
    const c = d.counts || {};
    totalActive += c.total || 0;
    totalResolved += c.resolved || 0;
    if (d.status === "online") onlineDevs += 1;
  });

  // 设备筛选 chips
  const devChips =
    `<button class="chip ${alertsDeviceFilter === "all" ? "on" : ""}" data-dev="all">全部（${devices.length} 台）</button>` +
    devices.map((d) => {
      const c = d.counts || {};
      const dot = d.status === "online" ? "🟢" : "⚪";
      const bl = (d.brand_label && d.name.indexOf(d.brand_label) === -1) ? ` · ${escapeHtml(d.brand_label)}` : "";
      return `<button class="chip ${alertsDeviceFilter === d.id ? "on" : ""}" data-dev="${escapeHtml(d.id)}">${dot} ${escapeHtml(d.name)}${bl}${c.total ? `<span class="chip-badge">${c.total}</span>` : ""}</button>`;
    }).join("");

  // 状态筛选 chips
  const statusChips = [
    `<button class="chip ${alertsStatusFilter === "all" ? "on" : ""}" data-st="all">全部</button>`,
    `<button class="chip ${alertsStatusFilter === "active" ? "on" : ""}" data-st="active">活跃（${totalActive}）</button>`,
    `<button class="chip ${alertsStatusFilter === "resolved" ? "on" : ""}" data-st="resolved">已恢复（${totalResolved}）</button>`,
  ].join("");

  filtersEl.innerHTML =
    `<style>
      .alerts-filters .chip-row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
      .alerts-filters .chip{border:1px solid rgba(128,128,128,.35);background:transparent;color:inherit;padding:5px 12px;border-radius:999px;cursor:pointer;font-size:13px;display:inline-flex;align-items:center;gap:6px}
      .alerts-filters .chip:hover{border-color:rgba(128,128,128,.7)}
      .alerts-filters .chip.on{background:#2e7d32;border-color:#2e7d32;color:#fff}
      .alerts-filters .chip-badge{background:rgba(229,57,53,.95);color:#fff;border-radius:999px;padding:0 6px;font-size:11px}
      .alerts-summary{margin:8px 0 2px;color:#9aa;font-size:13px}
      .alerts-summary b{color:#e53935}
      .alerts-list{display:flex;flex-direction:column;gap:10px;margin-top:8px}
      .alert-card .ac-top{display:flex;align-items:center;gap:8px}
      .alert-card .ac-dot{width:10px;height:10px;border-radius:50%;flex:none}
      .alert-card .ac-dot.crit{background:#e53935;box-shadow:0 0 0 3px rgba(229,57,53,.2)}
      .alert-card .ac-dot.warn{background:#f9a825;box-shadow:0 0 0 3px rgba(249,168,37,.2)}
      .alert-card .ac-badge{font-size:11px;padding:1px 8px;border-radius:999px;margin-left:auto}
      .alert-card .ac-badge.active{background:rgba(229,57,53,.15);color:#e53935}
      .alert-card .ac-badge.resolved{background:rgba(46,125,50,.15);color:#2e7d32}
      .alert-card .ac-meta{display:flex;flex-wrap:wrap;gap:14px;margin-top:8px;color:#9aa;font-size:12px}
      .alert-card .ac-actions{margin-top:8px}
    </style>
    <div class="chip-row">${devChips}</div>
    <div class="chip-row">${statusChips}</div>`;

  // 设备 / 状态筛选：点击后就地刷新（不重新请求）
  filtersEl.onclick = (ev) => {
    const devBtn = ev.target.closest("[data-dev]");
    const stBtn = ev.target.closest("[data-st]");
    if (devBtn) {
      alertsDeviceFilter = devBtn.getAttribute("data-dev");
      renderAlertsPage(state.alertsData || { devices: [] });
    } else if (stBtn) {
      alertsStatusFilter = stBtn.getAttribute("data-st");
      renderAlertsPage(state.alertsData || { devices: [] });
    }
  };

  if (summaryEl) {
    summaryEl.innerHTML = `共 ${devices.length} 台设备 · 在线 ${onlineDevs} 台 · 当前活跃告警 <b>${totalActive}</b> 条 · 历史已恢复 ${totalResolved} 条`;
  }

  // 收集告警：每设备的 history 已含 active 与 resolved（current 即 active 历史记录），避免重复
  let items = [];
  devices.forEach((d) => {
    if (alertsDeviceFilter !== "all" && d.id !== alertsDeviceFilter) return;
    (d.history || []).forEach((r) =>
      items.push(Object.assign({ _device: d.name, _dstatus: d.status, _brand: d.brand_label }, r)));
  });
  if (alertsStatusFilter !== "all")
    items = items.filter((r) => r.status === alertsStatusFilter);
  items.sort((a, b) => String(b.last_seen || "").localeCompare(String(a.last_seen || "")));

  if (!items.length) {
    listEl.innerHTML = `<div class="alert-card"><p style="margin:0">✅ 当前筛选下没有告警记录。</p></div>`;
    return;
  }
  listEl.innerHTML = items.map(renderAlertCard).join("");
  listEl.onclick = (ev) => {
    const btn = ev.target.closest("[data-fix]");
    if (!btn) return;
    const id = btn.getAttribute("data-fix");
    const rec = items.find((x) => x.id === id);
    if (rec) {
      askAiFix({
        title: rec.title,
        q: `TS Safe 告警：${rec.title}${rec.detail ? "：" + rec.detail : ""}。请分析原因并给出排查与修复步骤`,
      });
    }
  };
}

function renderAlertCard(r) {
  const crit = r.level === "critical";
  const active = r.status === "active";
  const statusBadge = active
    ? `<span class="ac-badge active">${crit ? "严重" : "待处理"}</span>`
    : `<span class="ac-badge resolved">已恢复</span>`;
  const devName = r.device_name || r._device || "本机";
  const devLine = devName + ((r._brand && devName.indexOf(r._brand) === -1) ? `（${escapeHtml(r._brand)}）` : "");
  const meta = [
    `设备：${escapeHtml(devLine)}`,
    `类型：${_alertTypeLabel(r.type)}`,
    `首次出现：${escapeHtml(r.first_seen || "-")}`,
    `最近出现：${escapeHtml(r.last_seen || "-")}`,
    `出现 ${r.count || 1} 次`,
  ];
  if (!active && r.resolved_at) meta.push(`已恢复：${escapeHtml(r.resolved_at)}`);
  const fixBtn = active
    ? `<button class="btn ghost ac-fix" data-fix="${escapeHtml(r.id)}">🤖 AI 修复</button>`
    : "";
  return `<div class="alert-card ${crit ? "crit" : "warn"}">
    <div class="ac-top">
      <span class="ac-dot ${crit ? "crit" : "warn"}"></span>
      <b>${escapeHtml(r.title || "告警")}</b>
      ${statusBadge}
    </div>
    ${r.detail ? `<p class="muted" style="margin:6px 0 0">${escapeHtml(r.detail)}</p>` : ""}
    <div class="ac-meta">${meta.map((m) => `<span>${m}</span>`).join("")}</div>
    <div class="ac-actions">${fixBtn}</div>
  </div>`;
}

// 异常面板：逐项「查看」定位 + 「AI 修复方案」
function openAnomalyModal() {
  const list = currentAnomalies().filter((a) => !dismissedAnoms.has(a.key));
  if (!list.length) {
    openModal("✅ 一切正常", `<p>当前没有检测到异常，NAS 运行正常。</p>`);
    return;
  }
  const actions = {};
  const body = list.map((a, i) => `
    <div class="anom-item ${a.sev >= 2 ? "sev2" : ""}">
      <div><b>${escapeHtml(a.title)}</b>${a.detail ? `<p class="muted">${escapeHtml(a.detail)}</p>` : ""}</div>
      <div class="anom-actions">
        <button class="btn ghost" data-act="view${i}">查看</button>
        <button class="btn primary" data-act="fix${i}">🤖 修复方案</button>
        ${a.view === "monitor" ? "" : `<button class="btn ghost" data-act="dismiss${i}">忽略</button>`}
      </div>
    </div>`).join("");
  list.forEach((a, i) => {
    actions[`view${i}`] = () => { closeModal(); showView(a.view || "home"); };
    actions[`fix${i}`] = () => askAiFix(a);
    if (a.view !== "monitor") {
      actions[`dismiss${i}`] = () => {
        dismissedAnoms.add(a.key);
        localStorage.setItem("nassafe_anom_dismissed", JSON.stringify([...dismissedAnoms]));
        openAnomalyModal();
      };
    }
  });
  // 快照保护类告警（快照消失/解锁/行为检测）：单条忽略治标不治本——
  // 30s 轮询一拉服务端，横幅又会被顶回来。用「确认清除」把服务端基线对齐现状。
  const tamperCount = (state.tamperAlerts || []).length + (state.deepAlerts || []).length;
  actions.clearalerts = async () => {
    try {
      const r = await api("/api/alerts/clear", { method: "POST", body: JSON.stringify({}) });
      state.deepAlerts = [];
      await pollAlerts();
      toast(`已清除警告：确认消失 ${r.removed || 0} 条、确认状态变化 ${r.updated || 0} 条`, "ok");
    } catch (e) {
      toast("清除失败：" + e.message, "err");
    }
    openAnomalyModal();
  };
  const clearFooter = tamperCount
    ? `<button class="btn primary" data-act="clearalerts">✔ 确认清除警告（以当前快照状态为准）</button>`
    : "";
  openModal("⚠ 检测到异常", body +
    `<p class="muted" style="margin-top:6px">AI 只提供修复建议，不会自动执行任何操作；操作前请自行确认。</p>`,
    clearFooter, actions);
}

// AI 修复方案：带异常上下文提问，展示建议（不执行）
async function askAiFix(a) {
  openModal("🤖 AI 修复方案",
    `<p style="margin-top:0"><b>异常：</b>${escapeHtml(a.title)}</p>
     <p class="muted"><span class="spinner"></span>AI 正在分析…</p>`,
    `<button class="btn ghost" data-act="back">返回异常列表</button>`,
    { back: () => openAnomalyModal() }, { stay: true });
  try {
    const data = await routeAI(a.q, "/api/ai/ask");
    openModal("🤖 AI 修复方案",
      `<p style="margin-top:0"><b>异常：</b>${escapeHtml(a.title)}</p>
       <div style="white-space:pre-wrap; line-height:1.8">${escapeHtml(data.text)}</div>
       <p class="muted" style="margin-top:10px">以上为 AI 建议，仅供参考；执行任何操作前请确认。</p>`,
      `<button class="btn ghost" data-act="back">返回异常列表</button>
       <button class="btn primary" data-act="close">关闭</button>`,
      { back: () => openAnomalyModal() }, { stay: true });
  } catch (e) {
    openModal("🤖 AI 修复方案",
      `<p>分析失败：${escapeHtml(e.message)}</p>
       <p class="muted">如果提示 AI 未配置，请到「设置 → AI 配置」先启用。</p>`,
      `<button class="btn ghost" data-act="back">返回异常列表</button>`,
      { back: () => openAnomalyModal() }, { stay: true });
  }
}

/* ------------------------- 监控路径选择器 ------------------------- */

// 弹窗树形勾选：存储卷（可展开一级子目录）→ 多选 → 写回监控路径输入框。
async function openPathPicker() {
  // 根节点用真实挂载路径（QNAP 的 mountpoint 是卷ID，需从指标里取实际路径）
  let roots = [];
  try {
    const mm = await api("/api/system/metrics");
    if (mm.ok && mm.metrics.volumes) {
      roots = mm.metrics.volumes.map((v) => {
        const known = state.volumes.find((x) => x.mountpoint === v.mount);
        return {
          mount: v.mount,
          name: v.name || (known ? known.name : (v.mount.split(/[\\/]/).pop() || v.mount)),
        };
      });
    }
  } catch (e) { /* 忽略 */ }
  if (!roots.length) {
    roots = state.volumes
      .filter((v) => (v.mountpoint || "").startsWith("/"))
      .map((v) => ({ mount: v.mountpoint, name: v.name }));
  }
  const sel = new Set(($("watchPaths").value || "").split(",").map((s) => s.trim()).filter(Boolean));

  const volRow = (v) => `
    <div class="pick-vol" data-vol="${escapeHtml(v.mount)}">
      <label class="pick-row">
        <input type="checkbox" class="pick-cb" data-vol="1"
               data-path="${escapeHtml(v.mount)}" ${sel.has(v.mount) ? "checked" : ""}>
        <b>${escapeHtml(v.name)}</b>
        <span class="muted">${escapeHtml(v.mount)}</span>
      </label>
      <button class="btn ghost pick-expand" data-path="${escapeHtml(v.mount)}">展开子目录 ▾</button>
      <div class="pick-children" hidden></div>
    </div>`;

  openModal(
    "选择监控路径",
    `<p class="muted" style="margin-top:0">勾选要实时监控的存储卷或其中的文件夹；可多选。留空 = 监控当前选中卷。</p>
     ${roots.map(volRow).join("") || `<p class="muted">未发现存储卷</p>`}`,
    `<button class="btn ghost" data-act="all">全选卷</button>
     <button class="btn ghost" data-act="clear">清空</button>
     <button class="btn ghost" data-act="close">取消</button>
     <button class="btn primary" data-act="ok">确定</button>`,
    {
      all: () => {
        document.querySelectorAll("#modalBody .pick-cb[data-vol]").forEach((cb) => (cb.checked = true));
      },
      clear: () => {
        document.querySelectorAll("#modalBody .pick-cb").forEach((cb) => (cb.checked = false));
      },
      ok: () => {
        const picked = [...document.querySelectorAll("#modalBody .pick-cb:checked")]
          .map((cb) => cb.dataset.path).filter(Boolean);
        // 父目录已选中时其子路径冗余，去重
        const uniq = picked.filter((p) => !picked.some((o) => o !== p && p.startsWith(o + "/")));
        $("watchPaths").value = uniq.join(", ");
        localStorage.setItem("nassafe_watchpaths", $("watchPaths").value);
        closeModal();
        toast(uniq.length ? `已选择 ${uniq.length} 个监控路径` : "已清空监控路径（留空=监控当前选中卷）", "ok");
      },
    }
  );

  // 子目录懒加载（只列一级，够用且不重）
  $("modalBody").onclick = async (ev) => {
    const btn = ev.target.closest(".pick-expand");
    if (!btn) return;
    const wrap = btn.parentElement.querySelector(".pick-children");
    if (!wrap.hidden) { wrap.hidden = true; btn.textContent = "展开子目录 ▾"; return; }
    if (!wrap.dataset.loaded) {
      btn.textContent = "读取中…";
      try {
        const base = btn.dataset.path.replace(/\/+$/, "");
        const data = await api(`/api/list_dir?path=${encodeURIComponent(base)}`);
        wrap.innerHTML = (data.dirs || []).map((d) => {
          const sep = base.includes("\\") ? "\\" : "/";
          const p = base + sep + d;
          return `<label class="pick-row sub">
            <input type="checkbox" class="pick-cb" data-path="${escapeHtml(p)}" ${sel.has(p) ? "checked" : ""}>
            📁 ${escapeHtml(d)}
          </label>`;
        }).join("") || `<p class="muted">没有子目录</p>`;
        wrap.dataset.loaded = "1";
      } catch (e) {
        wrap.innerHTML = `<p class="muted">读取失败：${escapeHtml(e.message)}</p>`;
      }
    }
    wrap.hidden = false;
    btn.textContent = "收起 ▴";
  };
}

/* ------------------------- 本地 AI：浏览器直连（数据不出本机） ------------------------- */

// 本地 AI 优先从浏览器直接调用用户电脑上的 Ollama / LM Studio（localhost = 用户本机），
// 不再绕 NAS 容器后端 —— 容器里的 localhost 不是用户电脑，旧方案永远连不上。
async function callLocalAI(question, context, history) {
  const base = (($("aiBase") && $("aiBase").value) || "http://localhost:11434/v1").trim().replace(/\/+$/, "");
  const model = (($("aiModel") && $("aiModel").value) || "").trim() || "qwen2.5:7b";
  const messages = [];
  if (context) messages.push({ role: "system", content: "你是 NAS 数据安全助手，用通俗中文回答。背景：" + context });
  for (const m of history || []) {
    if (m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string" && m.content.trim()) {
      messages.push({ role: m.role, content: m.content }); // 多轮上下文
    }
  }
  messages.push({ role: "user", content: question });
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 120000);
  let res;
  try {
    res = await fetch(base + "/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, messages, temperature: 0.3, max_tokens: 1200 }),
      signal: ctl.signal,
    });
  } finally { clearTimeout(t); }
  if (!res.ok) {
    let msg = `本地 AI 返回 HTTP ${res.status}`;
    try { const e = await res.json(); if (e && e.error) msg = typeof e.error === "string" ? e.error : JSON.stringify(e.error); } catch (e) {}
    throw new Error(msg + "（确认 Ollama 已启动，且启动时设置了 OLLAMA_ORIGINS=* 允许本站点访问）");
  }
  const data = await res.json();
  const text = data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content;
  if (!text) throw new Error("本地 AI 返回内容为空");
  return { ok: true, text: String(text).trim() };
}

// 统一入口：ollama 走浏览器直连；其它供应商走后端（保护密钥）。
// 本地 AI 兜底通道：浏览器直连被 CORS 拦时，改由 NAS 后端中转调用用户电脑的 Ollama
async function callLocalAIViaNAS(question, context, history) {
  const base = (($("aiBase") && $("aiBase").value) || "").trim();
  const model = (($("aiModel") && $("aiModel").value) || "").trim();
  const body = { question, base_url: base, model };
  if (context) body.context = context;
  if (history && history.length) body.history = history; // 多轮上下文
  const data = await api("/api/ai/local", { method: "POST", body: JSON.stringify(body) }, 120000);
  return data.text;
}

async function routeAI(question, cloudEndpoint, context, history, extra) {
  const prov = ($("aiProvider") && $("aiProvider").value) || "";
  if (prov === "ollama") {
    // 统一返回 {text} 对象——调用方（问AI/解读/异常文案）都按 data.text 取答案；
    // 本地路径此前返回纯字符串，曾致回答渲染为空（答案"丢失"）。
    try {
      return await callLocalAI(question, context || "", history);
    } catch (directErr) {
      // 直连失败（最常见 = 未设系统级 OLLAMA_ORIGINS 被 CORS 拦），自动改走 NAS 中转
      try {
        return { text: await callLocalAIViaNAS(question, context || "", history) };
      } catch (relayErr) {
        throw new Error(directErr.message + "\n[NAS 中转也失败] " + relayErr.message);
      }
    }
  }
  let body;
  if (cloudEndpoint === "/api/ai/interpret") body = { text: question };
  else {
    body = context ? { question, context } : { question };
    if (history && history.length) body.history = history; // 多轮上下文
    if (extra && typeof extra === "object") Object.assign(body, extra);
  }
  return await api(cloudEndpoint, { method: "POST", body: JSON.stringify(body) }, 120000);
}

// 从浏览器探测本机 Ollama（localhost:11434）——最常见的本地 AI 部署位置。
async function probeLocalOllama() {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 3000);
  try {
    const res = await fetch("http://localhost:11434/api/tags", { signal: ctl.signal });
    if (!res.ok) return null;
    const body = await res.json();
    const models = (body.models || []).map((m) => m.name).filter(Boolean);
    return { base_url: "http://localhost:11434/v1", models: models.map((n) => ({ name: n })), recommended: models[0] || "", via: "browser" };
  } catch (e) {
    return null;
  } finally { clearTimeout(t); }
}

function applyDiscovery(f) {
  $("aiProvider").value = "ollama";
  aiProviderChanged();
  $("aiBase").value = f.base_url;
  const best = f.recommended || (f.models && f.models.length ? (f.models[0].name || f.models[0]) : "");
  if (best) $("aiModel").value = best;
  saveAI();
  toast(`已自动配置：${f.base_url}（模型：${f.models && f.models.length ? f.models[0] : "默认"}）`, "ok");
  if (f.hint) setTimeout(() => toast(f.hint, "warn"), 800);
}

async function aiDiscover() {
  const btn = $("aiDiscoverBtn");
  btn.disabled = true;
  btn.textContent = "搜索中…";
  try {
    // 优先从浏览器探测本机 Ollama（localhost = 你的电脑）
    const local = await probeLocalOllama();
    if (local) {
      applyDiscovery(local);
      toast("已发现本机 Ollama，已自动配置（数据不出你的电脑）", "ok");
      return;
    }
    // 退回后端 LAN 扫描（找 NAS 本机或局域网其它机器上的 Ollama / LM Studio）
    const data = await api("/api/ai/discover", { method: "POST", body: "{}" });
    const found = data.found || [];
    if (!found.length) {
      toast("没找到本地 AI。请先在本机安装 Ollama（启动前设 OLLAMA_ORIGINS=*），再点「自动搜索」", "err");
    } else if (found.length === 1) {
      applyDiscovery(found[0]);
    } else {
      openModal(
        "找到多个本地 AI 服务",
        found.map((f, i) => `
          <label class="pick-row">
            <input type="radio" name="aiDisc" value="${i}" ${i === 0 ? "checked" : ""}>
            <b>${escapeHtml(f.base_url)}</b>
            <span class="muted">${(f.models || []).length} 个模型${f.hint ? " · ⚠ 需设置监听" : ""}</span>
          </label>
          ${f.models && f.models.length ? `<p class="muted" style="margin:0 0 8px 24px">模型：${f.models.map(escapeHtml).join("、")}</p>` : ""}
        `).join(""),
        `<button class="btn ghost" data-act="close">取消</button>
         <button class="btn primary" data-act="ok">使用选中的</button>`,
        {
          ok: () => {
            const r = document.querySelector("input[name='aiDisc']:checked");
            if (r) applyDiscovery(found[Number(r.value)]);
            closeModal();
          },
        }
      );
    }
  } catch (e) {
    toast("搜索失败：" + e.message, "err");
  }
  btn.disabled = false;
  btn.textContent = "🔍 自动搜索本地 AI";
}



// 顶栏标签页：设备控制台 / 本机总览 / 快照时间轴 为免登录数据查看；
// 实时监控 / 重复文件 / 磁盘清理 / 换机迁移 / 设置 为受保护视图，只能从「设置与管理」登录后进入。
const VIEWS = ["console", "home", "snapshots", "monitor", "alerts", "dups", "junk", "migrate", "settings"];

/* ===================== 跨品牌多设备总控制台 ===================== */

const HEALTH_LABEL = { 0: "正常", 1: "注意", 2: "警告", 3: "异常" };

function healthClass(h) {
  h = Number(h) || 0;
  return h >= 3 ? "bad" : h === 2 ? "danger" : h === 1 ? "warn" : "ok";
}

async function loadConsole(force) {
  const box = $("consoleGroups");
  // 无感刷新：页面已经渲染过就不再清空内容转圈，后台拉到新数据后原位更新；
  // 只有第一次进入（还没内容）才显示加载动画，之后刷新用户基本无感知。
  const firstLoad = !box || !box.querySelector(".net-tile, .topo-stage");
  if (box && firstLoad) box.innerHTML = `<p class="muted"><span class="spinner"></span>正在汇总各品牌设备状态…</p>`;
  try {
    const data = await api("/api/devices" + (force ? "?force=1" : ""), {}, 20000);
    renderConsole(data);
  } catch (e) {
    if (box && firstLoad) box.innerHTML = `<p class="muted">汇总失败：${escapeHtml(e.message)}</p>`;
  }
}

/* 演示模式：在地址后面加 ?demo=8，可临时虚拟出 N 台设备，用来预览多台设备时的拓扑效果。
   只存在于当前页面的内存里，不会写进任何配置，去掉参数刷新即恢复。 */
const DEMO_TPL = [
  ["威联通 TS-873A", "nas", "威联通 QNAP"],
  ["群晖 DS920+", "nas", "群晖 Synology"],
  ["办公室台式机", "pc", "Windows"],
  ["MacBook Pro", "laptop", "macOS"],
  ["应用服务器", "server", "Linux"],
  ["阿里云 ECS", "server", "阿里云 ECS"],
  ["核心交换机", "router", "网络设备"],
  ["门口摄像头", "camera", "监控"],
  ["飞牛 fnOS", "nas", "飞牛 fnOS"],
  ["绿联 NAS", "nas", "绿联 UGREEN"],
];

function demoDevices(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const t = DEMO_TPL[i % DEMO_TPL.length];
    const h = [0, 0, 1, 0, 2, 0, 0, 3][i % 8];
    const off = (i % 7 === 5);
    out.push({
      id: "demo-" + (i + 1), name: t[0], kind: t[1], brand: t[2], brand_label: t[2],
      type: "remote", host: "", port: 0, https: false,
      status: off ? "offline" : "online", health: h,
      health_label: ["正常", "注意", "警告", "异常"][h],
      note: off ? "演示设备（离线）" : "演示设备",
      smart: { available: (t[1] === "nas" || t[1] === "server"), disk_count: 2 + (i % 5), bad: 0, warn: 0 },
      snapshot: { protected_units: 1 + (i % 3), total_units: 3, snap_count: 2 + i, unprotected_units: (i % 3) ? 1 : 0 },
      guard: { level: h >= 2 ? "bad" : (h === 1 ? "warn" : "ok"), label: h >= 2 ? "有风险" : (h === 1 ? "需留意" : "正常") },
      __demo: true,
    });
  }
  return out;
}

function withDemoDevices(data) {
  const n = parseInt((new URLSearchParams(location.search).get("demo") || "0"), 10);
  if (!n) return data;
  return Object.assign({}, data, { devices: (data.devices || []).concat(demoDevices(n)) });
}


/* 无感刷新：记录上次渲染的「设备指纹」，新数据进来先对比——
   指纹没变（设备没增减/没改名/状态字段没变）就只原位改文字与颜色，
   不重建 DOM：卡片不闪、拓扑粒子动画不中断、滚动位置不丢。 */
let _consoleSig = "";
let _selectedDevId = "";
function consoleSig(devs) {
  return devs.map((d) => [
    d.id, deviceDisplayName(d), d.status, d.health || 0,
    (d.agent && d.agent.status) || "",
    d.smart && d.smart.available ? `${d.smart.disk_count}-${d.smart.bad || 0}-${d.smart.warn || 0}` : "na",
    `${(d.snapshot && d.snapshot.protected_units) || 0}/${(d.snapshot && d.snapshot.total_units) || 0}`,
  ].join(":")).join("|");
}

function renderConsole(data) {
  const totals = data.totals || {};
  const set = (id, v) => { const el = $(id); if (el) el.textContent = (v == null ? "—" : v); };
  set("covDevices", totals.devices);
  set("covOnline", totals.online);
  set("covDisks", totals.smart_disks);
  set("covRisk", (totals.smart_bad || 0) + (totals.smart_warn || 0));
  set("covSnaps", totals.snap_count);
  set("covUnprot", totals.unprotected_units);

  const wrap = $("consoleGroups");
  if (!wrap) return;
  const all = (withDemoDevices(data).devices) || [];
  if (!all.length) {
    wrap.innerHTML = `<p class="muted">还没有登记任何设备。</p>`;
    return;
  }
  const remotes = all.filter((d) => d.type !== "local");
  // 无感刷新路径：设备列表结构没变，只原位更新卡片与拓扑状态
  const sig = consoleSig(all);
  if (sig === _consoleSig && wrap.querySelector(".net-tile")) {
    for (const d of remotes) updateNetTile(d);
    const cnt = wrap.querySelector(".dev-group-count");
    if (cnt) cnt.textContent = `${remotes.length} 台`;
    _consoleData = withDemoDevices(data);
    updateTopoStatus(_consoleData.devices || []);
    const localNow = (_consoleData.devices || []).find((d) => d.type === "local") || (_consoleData.devices || [])[0];
    const selDev = findDev(_consoleData, _selectedDevId) || localNow;
    if (selDev) selectDevice(selDev.id, { quiet: true });
    return;
  }
  _consoleSig = sig;
  // 本机不再在这里重复出卡片：顶部详情区已展示本机状态并带「进入本机」按钮
  let html = "";
  // 联网设备：图标框排列，一眼看到每台的监控状态
  html += `<div class="dev-group">
      <div class="dev-group-head"><span class="dev-group-ico">🌐</span><span>联网设备</span>
        <span class="dev-group-count">${remotes.length} 台</span></div>
      <div class="net-grid">`;
  if (!remotes.length) {
    html += `<p class="muted net-empty">还没有添加联网设备，到左侧「功能设置 → 添加设备」里添加。</p>`;
  }
  for (const d of remotes) html += netTile(d);
  html += `</div></div>`;
  wrap.innerHTML = html;

  // 绑定设备卡上的按钮
  wrap.querySelectorAll("[data-enter]").forEach((b) => {
    b.onclick = () => {
      const id = b.dataset.enter;
      const dev = findDev(data, id);
      if (!dev) return;
      openDeviceControl(dev);
    };
  });
  wrap.querySelectorAll("[data-net-open]").forEach((b) => {
    b.onclick = (ev) => {
      ev.stopPropagation();
      const dev = findDev(_consoleData, b.dataset.netOpen);
      openDeviceControl(dev);
    };
  });
  wrap.querySelectorAll("[data-net]").forEach((t) => {
    t.onclick = (ev) => {
      if (ev.target.closest("[data-net-open]") || ev.target.closest("[data-remove]")) return;
      selectDevice(t.dataset.net);
      const box = $("deviceDetail");
      if (box) box.scrollIntoView({ behavior: "smooth", block: "center" });
    };
  });
  wrap.querySelectorAll("[data-rename]").forEach((b) => {
    b.onclick = (ev) => { ev.stopPropagation(); openRenameModal(b.dataset.rename); };
  });
  wrap.querySelectorAll("[data-remove]").forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.remove;
      if (!confirm("确定要移除这台设备吗？")) return;
      try {
        await api("/api/devices/remove", { method: "POST", body: JSON.stringify({ id }) });
        toast("已移除设备", "ok");
        loadConsole(true);
      } catch (e) { toast("移除失败：" + e.message, "err"); }
    };
  });

  const dataEff = withDemoDevices(data);
  _consoleData = dataEff;
  renderTopology(dataEff);
  const localDev = (data.devices || []).find((d) => d.type === "local") || (data.devices || [])[0];
  const selDev = (_selectedDevId && findDev(dataEff, _selectedDevId)) || localDev;
  if (selDev) selectDevice(selDev.id);
}

/* 原位更新一张联网设备卡片（不重建 DOM）：只改健康色、在线状态、硬盘/快照两行 */
function updateNetTile(d) {
  const t = document.querySelector(`.net-tile[data-net="${CSS.escape(d.id)}"]`);
  if (!t) return;
  const hc = healthClass(d.health);
  const offline = d.status === "offline";
  const hLabel = HEALTH_LABEL[d.health] || "正常";
  const sm = d.smart || {}, sn = d.snapshot || {};
  t.className = `net-tile ${hc} ${offline ? "offline" : ""}`;
  t.style.setProperty("--col", topoHealthColor(d));
  const st = t.querySelector(".nt-state");
  if (st) st.innerHTML = `<span class="nt-dot"></span>${offline ? "离线" : escapeHtml(hLabel)}`;
  const mt = t.querySelector(".nt-metrics");
  if (mt) mt.innerHTML = `<span>${netDiskText(sm)}</span><span>⛨ ${sn.protected_units || 0}/${sn.total_units || 0} 卷受保护</span>`;
}

/* 原位更新拓扑节点/连线的颜色与状态文字（不重建画面，粒子动画不中断） */
function updateTopoStatus(devs) {
  for (const d of devs) {
    const id = CSS.escape(d.id);
    const col = topoHealthColor(d);
    const off = d.status === "offline";
    const node = document.querySelector(`.topo-node[data-id="${id}"]`);
    if (node) {
      const st = off ? "off" : ((d.health || 0) >= 2 ? "warn" : "ok");
      node.className = `topo-node ${st}${off ? " off" : ""}`;
      node.style.setProperty("--col", col);
      const s = node.querySelector(".tn-status");
      if (s) s.textContent = off ? "离线" : (d.health_label || "正常");
      const ico = node.querySelector(".tn-badge");
      if (ico) ico.style.background = col;
    }
    const link = document.querySelector(`.topo-link[data-id="${id}"]`);
    if (link) { link.setAttribute("stroke", col); link.classList.toggle("off", off); }
  }
}

/* ---------- 设备拓扑思维导图（中心总控台 + 环绕设备节点） ---------- */
let _consoleData = null;

function topoHealthColor(d) {
  if (d.status === "offline") return "#5a6678";
  const HL = { 0: "#2fe0a0", 1: "#f5c518", 2: "#ff9f43", 3: "#ff5c5c" };
  return HL[Math.max(0, Math.min(3, d.health || 0))];
}

/* 中心→节点的弯曲细线（三次贝塞尔弧），整体同向弯曲呈思维导图的柔顺弧线 */
/* 弯曲细线（三次贝塞尔），bow 控制弯曲方向与幅度（可正可负 => 任意方向弯曲），
   wob 控制 S 形扭转（任意形状扭曲）。每条连接线独立随机，整体呈有机的思维导图曲线。 */
function curvedPath(cx, cy, x, y, bow, wob, gapA, gapB) {
  const dx = x - cx, dy = y - cy;
  const dist = Math.hypot(dx, dy) || 1;
  const ux = dx / dist, uy = dy / dist;          // 切线单位向量
  const px = -uy, py = ux;                        // 垂直单位向量
  // 两端留白：连线不进入中心面板 / 设备图标的区域，既不压在图标上，也不透过玻璃被雾化
  const gA = Math.min(gapA || 0, dist * 0.34);
  const gB = Math.min(gapB || 0, dist * 0.34);
  const ax = cx + ux * gA, ay = cy + uy * gA;
  const bx = x - ux * gB,  by = y - uy * gB;
  const sx = bx - ax, sy = by - ay;
  const d2 = Math.hypot(sx, sy) || 1;
  const b = d2 * (bow || 0);
  const w = d2 * (wob || 0) * 0.14;
  // 两个控制点各自带垂直弯曲(b) + 反向切线扭转(w)，形成任意扭曲的曲线
  const c1x = ax + sx * 0.33 + px * b + ux * w;
  const c1y = ay + sy * 0.33 + py * b + uy * w;
  const c2x = ax + sx * 0.66 + px * b - ux * w;
  const c2y = ay + sy * 0.66 + py * b - uy * w;
  return `M ${ax.toFixed(1)} ${ay.toFixed(1)} C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${bx.toFixed(1)} ${by.toFixed(1)}`;
}

/* ------------------------------------------------------------------
   设备机型图标体系：按设备类型给「彩色圆底 + 白色矢量图形」徽标
   （风格对齐扁平彩色圆形图标参考稿），并给每种机型一种专属色。
   ------------------------------------------------------------------ */
const TOPO_GLYPHS = {
  nas: '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3V6"/><path d="M4.5 12c0 1.66 3.36 3 7.5 3s7.5-1.34 7.5-3"/>',
  pc: '<rect x="3" y="4.5" width="18" height="12" rx="2"/><path d="M9.5 20h5M12 16.5V20"/>',
  laptop: '<rect x="5" y="5" width="14" height="9.5" rx="1.6"/><path d="M3 18.5h18l-1.8-3H4.8z"/>',
  server: '<rect x="4" y="3.5" width="16" height="7" rx="1.6"/><rect x="4" y="13.5" width="16" height="7" rx="1.6"/><path d="M7.5 7h.01M7.5 17h.01"/>',
  router: '<rect x="3.5" y="13" width="17" height="6.5" rx="2"/><path d="M7 16.2h.01M10.5 16.2h.01"/><path d="M8.5 9.5a6 6 0 0 1 7 0"/><path d="M6.2 6.8a9.5 9.5 0 0 1 11.6 0"/>',
  camera: '<rect x="2.5" y="7" width="13" height="10" rx="2.2"/><path d="M15.5 10.5 21 8v8l-5.5-2.5z"/>',
  phone: '<rect x="7.5" y="2.5" width="9" height="19" rx="2.2"/><path d="M11 18.5h2"/>',
  chip: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 2.5v3M14 2.5v3M10 18.5v3M14 18.5v3M2.5 10h3M2.5 14h3M18.5 10h3M18.5 14h3"/>',
};

const TOPO_KIND_COLORS = {
  nas: "#0d9488", pc: "#3b82f6", laptop: "#8b5cf6", server: "#6366f1",
  router: "#f59e0b", camera: "#ef4444", phone: "#ec4899", chip: "#64748b",
};

function topoDeviceKind(d) {
  if (d.kind && TOPO_GLYPHS[d.kind]) return d.kind;
  const s = (d.name || "") + " " + (d.brand_label || "") + " " + (d.brand || "") + " " + (d.os_id || "");
  if (/路由|网关|gateway|router|\bap\b/i.test(s)) return "router";
  if (/摄像|cam|探头|ipcam/i.test(s)) return "camera";
  if (/笔记本|laptop|macbook|\bmac\b/i.test(s)) return "laptop";
  if (/win|windows|电脑|台式|工作站|\bpc\b/i.test(s)) return "pc";
  if (/服务器|server|主机/i.test(s)) return "server";
  if (/手机|phone|安卓|android/i.test(s)) return "phone";
  if (d.type === "local") return "nas";
  if (/nas|存储|群晖|威联通|synology|qnap|fnos|绿联|ugreen/i.test(s)) return "nas";
  return "chip";
}

function topoDeviceGlyph(kind) {
  const p = TOPO_GLYPHS[kind] || TOPO_GLYPHS.chip;
  return '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="#fff" '
       + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + p + '</svg>';
}

// 兼容旧调用：返回彩色圆底徽标的 HTML
function topoDeviceIcon(d, healthCol) {
  const kind = topoDeviceKind(d);
  // 徽标底色跟「连接线」一致（设备健康色），故障机自然跟着线变红/变橙；
  // 机型仍然靠矢量图形 + 一圈机型色外环区分
  const bg = healthCol || TOPO_KIND_COLORS[kind] || TOPO_KIND_COLORS.chip;
  const ring = TOPO_KIND_COLORS[kind] || TOPO_KIND_COLORS.chip;
  return '<div class="tn-badge" style="background:' + bg + ';--ring:' + ring + '">'
       + topoDeviceGlyph(kind) + '</div>';
}

/* 动态粒子背景（底层），操作时自动降帧以保证响应速度、不影响 7×24 稳定 */
let _topoParticles = null;
function stopTopoParticles() {
  if (_topoParticles) { cancelAnimationFrame(_topoParticles.raf); _topoParticles = null; }
}
function startTopoParticles(canvas, stage) {
  stopTopoParticles();
  const ctx = canvas.getContext("2d");
  let w = 0, h = 0, dpr = Math.min(2, window.devicePixelRatio || 1);
  function resize() {
    const r = stage.getBoundingClientRect();
    w = Math.max(1, r.width); h = Math.max(1, r.height);
    canvas.width = w * dpr; canvas.height = h * dpr;
    canvas.style.width = w + "px"; canvas.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  resize();
  window.addEventListener("resize", resize);
  const N = Math.max(30, Math.min(72, Math.floor(w * h / 13000)));
  const ps = Array.from({ length: N }, () => ({
    x: Math.random() * w, y: Math.random() * h,
    vx: (Math.random() - .5) * .22, vy: (Math.random() - .5) * .22,
    r: Math.random() * 1.6 + .5
  }));
  let last = 0, lowPower = false, lpTimer = 0;
  function setLP(on) {
    lowPower = on; clearTimeout(lpTimer);
    if (on) lpTimer = setTimeout(() => { lowPower = false; }, 2600);
  }
  stage.addEventListener("pointermove", () => setLP(true), true);
  stage.addEventListener("pointerdown", () => setLP(true), true);
  function frame(t) {
    _topoParticles.raf = requestAnimationFrame(frame);
    const interval = lowPower ? 80 : 16;      // 操作时降帧 ~12fps，待机 ~60fps
    if (t - last < interval) return;
    last = t;
    const _light = document.body.classList.contains('light');
    const _pc = _light
      ? { dot: 'rgba(70,130,200,.45)', line: 'rgba(70,130,200,' }
      : { dot: 'rgba(120,200,255,.5)', line: 'rgba(80,160,220,' };
    ctx.clearRect(0, 0, w, h);
    for (const p of ps) {
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0) p.x += w; if (p.x > w) p.x -= w;
      if (p.y < 0) p.y += h; if (p.y > h) p.y -= h;
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.2832);
      ctx.fillStyle = _pc.dot; ctx.fill();
    }
    for (let i = 0; i < ps.length; i++) for (let j = i + 1; j < ps.length; j++) {
      const dx = ps[i].x - ps[j].x, dy = ps[i].y - ps[j].y, dd = dx * dx + dy * dy;
      if (dd < 8200) {
        ctx.strokeStyle = _pc.line + ((1 - dd / 8200) * .16).toFixed(3) + ")";
        ctx.lineWidth = .6;
        ctx.beginPath(); ctx.moveTo(ps[i].x, ps[i].y); ctx.lineTo(ps[j].x, ps[j].y); ctx.stroke();
      }
    }
  }
  _topoParticles = { raf: requestAnimationFrame(frame) };
}

/* ------------------------------------------------------------------
   粒子流式随机漂移：待机时节点像底层粒子一样随机缓动；
   鼠标进入画面后停止漂移并归位（中心始终固定在正中，不参与漂移）
   ------------------------------------------------------------------ */
let _topoDrift = null;

function stopTopoDrift() {
  if (_topoDrift && _topoDrift.raf) cancelAnimationFrame(_topoDrift.raf);
  _topoDrift = null;
}

function startTopoDrift(stage, items, geo) {
  stopTopoDrift();
  const W = geo.W, H = geo.H, cx = geo.cx, cy = geo.cy;
  const RX = geo.rx || 400, RY = geo.ry || 205;
  const ky = W / H;            // 让纵向与横向的「像素速度」一致
  const AMP = 0.060;           // 监控中心等「自由粒子」的漂浮半径（占画面宽度比例）
  // 角向漂浮幅度按设备数自适应：设备越多扇区越窄，幅度越小，避免节点互相撞在一起
  const AA = Math.min(0.52, (2 * Math.PI / Math.max(1, geo.n || 1)) * 0.42);
  const AR = 0.22;             // 径向漂浮幅度（占基准半径的比例）
  // 漂移总半径限幅：任何方向都不允许把节点推出画面（留 46px 安全边）
  const RMAX = Math.min((cx - 46) / RX, (W - 46 - cx) / RX, (cy - 46) / RY, (H - 46 - cy) / RY);
  let last = 0;
  function step(t) {
    _topoDrift.raf = requestAnimationFrame(step);
    if (t - last < 33) return;                 // ~30fps，够顺且省电
    const dt = Math.min(80, t - last); last = t;
    const k = dt / 33;                         // 与实际帧率无关
    const live = stage.classList.contains("live");
    let ccx = cx, ccy = cy;                  // 连线起点 = 监控中心当前实际位置
    for (const it of items) {
      if (live) {
        // 鼠标有动作：平滑回到「默认状态位置」，放大就发生在默认位置上；
        // 连线弧度也平滑收回默认形状（配合 CSS 的渐隐显现与缓慢放大）
        if (it.mode === "polar") {
          it.aOff += (0 - it.aOff) * 0.16 * k;
          it.rOff += (0 - it.rOff) * 0.16 * k;
          it.aV *= (1 - 0.12 * k); it.rV *= (1 - 0.12 * k);
        } else {
          it.dx += (0 - it.dx) * 0.16 * k;
          it.dy += (0 - it.dy) * 0.16 * k;
        }
        it.bow += (it.bow0 - it.bow) * 0.10 * k;
        it.wob += (it.wob0 - it.wob) * 0.10 * k;
      } else {
        // —— 三维粒子流漂浮 ——
        // ① 景深 z：极缓慢地前后漂移。越近 => 越快、越大、越亮；越远 => 越慢、越小、越淡
        if (t > it.zN) { it.zT = 0.22 + Math.random() * 0.78; it.zN = t + 7000 + Math.random() * 11000; }
        it.z += (it.zT - it.z) * 0.0035 * k;
        const zf = 0.55 + it.z * 0.85;              // 速度/尺寸系数
        if (it.mode === "polar") {
          // ② 绕中心缓慢滑行 + 径向浮动：位置在整幅画面里持续变化，而不是原地小抖动。
          //    运动形式照搬背景粒子：恒定慢速前进 + 方向连续缓变 + 软边界平缓转弯（绝不硬反弹）
          it.aV += (Math.random() - .5) * 0.00062 * k;
          it.rV += (Math.random() - .5) * 0.00046 * k;
          if (Math.abs(it.aOff) > AA * 0.62) it.aV -= Math.sign(it.aOff) * 0.00120 * k;
          if (Math.abs(it.rOff) > AR * 0.62) it.rV -= Math.sign(it.rOff) * 0.00090 * k;
          it.aV = Math.max(-0.0028, Math.min(0.0028, it.aV));
          it.rV = Math.max(-0.0022, Math.min(0.0022, it.rV));
          it.aOff += it.aV * zf * k;
          it.rOff += it.rV * zf * k;
          it.aOff = Math.max(-AA * 1.1, Math.min(AA * 1.1, it.aOff));
          it.rOff = Math.max(-AR * 1.1, Math.min(AR * 1.1, it.rOff));
        } else {
          // ② 恒定慢速前进 + 方向连续缓变：绝不硬反弹，像背景粒子一样飘过去
          it.ang += (Math.random() - .5) * 0.030 * k;
          const rx = it.dx * W, ry = it.dy * H;
          const rr = Math.hypot(rx, ry), A = AMP * W * (it.ampK || 1);
          if (rr > A * 0.70) {                        // 软边界：越靠外越平缓地向内转弯
            const inward = Math.atan2(-ry, -rx);
            let diff = inward - it.ang;
            while (diff >  Math.PI) diff -= 2 * Math.PI;
            while (diff < -Math.PI) diff += 2 * Math.PI;
            it.ang += diff * Math.min(0.09, (rr / A - 0.70) * 0.26) * k;
          }
          const v = it.spd * zf * k;
          it.dx += Math.cos(it.ang) * v;
          it.dy += Math.sin(it.ang) * v * ky;
        }
        // ③ 连线弧度/扭转：目标值缓慢随机游走 —— 任意流动，不再是固定周期的“呼吸”
        if (t > it.bN) {
          it.bowT = it.bow0 * (Math.random() * 1.9 - 0.45);
          it.wobT = it.wob0 * (Math.random() * 1.7 - 0.60);
          it.bN = t + 2400 + Math.random() * 3200;
        }
        it.bow += (it.bowT - it.bow) * 0.010 * k;
        it.wob += (it.wobT - it.wob) * 0.008 * k;
      }
      // 位置：极坐标（绕中心滑行 + 径向浮动）或笛卡尔（监控中心自由漂浮）
      let pxv, pyv;
      if (it.mode === "polar") {
        // 景深带来的层次：远的略微内缩、近的略微外扩 —— 与背景粒子一样的三维纵深
        const zrT = live ? 0 : (it.z - 0.5) * 0.10;
        it.zr += (zrT - it.zr) * 0.05 * k;
        const ang = it.a0 + it.aOff, rr = Math.min((it.rBase || 1) + it.rOff + it.zr, RMAX);
        pxv = cx + RX * rr * Math.cos(ang);
        pyv = cy + RY * rr * Math.sin(ang);
      } else {
        pxv = (it.bx + it.dx) * W;
        pyv = (it.by + it.dy) * H;
      }
      if (it.isCenter) { ccx = pxv; ccy = pyv; }   // 连线起点跟随监控中心，不留空洞
      if (it.el) {
        it.el.style.left = (pxv / W * 100).toFixed(3) + "%";
        it.el.style.top  = (pyv / H * 100).toFixed(3) + "%";
        // 景深：近处更亮更实，远处更淡更虚（一点点模糊，像远景）
        const z = it.z;
        if (it.isCenter) {
          // 监控中心：固定大小、固定清晰度，不做景深缩放与模糊，文字不会发虚失真
          it.el.style.setProperty("--zs", "1");
          it.el.style.opacity = "1";
          it.el.style.filter = "none";
        } else {
          // 放大态把景深系数锁定成 1：不再让文字被持续的微小缩放反复重栅格化
          it.el.style.setProperty("--zs", live ? "1" : (0.86 + z * 0.28).toFixed(3));
          if (live) {
            it.el.style.opacity = "1";
            it.el.style.filter = "none";
          } else {
            it.el.style.opacity = (0.52 + z * 0.48).toFixed(3);
            it.el.style.filter = "blur(" + ((1 - z) * 0.6).toFixed(2) + "px)";
          }
        }
      }
      // 连线端点与弧度随节点一起流动；hover 时也不额外扭动/加粗（保持细线）
      if (it.link) {
        // 两端留白：待机/放大两套目标值平滑插值，端点始终贴在图标边缘上
        const tgC = live ? geo.gaps.cLive : geo.gaps.cIdle;
        const tgN = live ? geo.gaps.nLive : geo.gaps.nIdle;
        it.gc += (tgC - it.gc) * 0.14 * k;
        it.gn += (tgN - it.gn) * 0.14 * k;
        it.link.setAttribute("d", curvedPath(ccx, ccy, pxv, pyv, it.bow, it.wob, it.gc, it.gn));
        it.link.style.opacity = live ? "" : (0.55 + it.z * 0.35).toFixed(3);
      }
    }
  }
  _topoDrift = { raf: requestAnimationFrame(step) };
}

// 让节点连线有长有短、错落有致：按索引分配到内/中/外三层，加小抖动但保持可预测
function radiusBias(i, n) {
  if (n <= 4) return 1;
  const tiers = [1.10, 0.86, 1.00];
  const jitter = (((i * 13) % 7) - 3) * 0.018;   // ±0.054，确定性抖动
  const v = tiers[i % 3] + jitter;
  return Math.max(0.78, Math.min(1.14, v));
}

function renderTopology(data) {
  const host = $("consoleTopology");
  if (!host) return;
  const devs = data.devices || [];
  if (!devs.length) {
    host.innerHTML = `<p class="muted">还没有设备，到「功能设置 → 添加设备」里添加后这里会显示。</p>`;
    return;
  }
  const W = 1000, H = 600, cx = 500, cy = 300;
  // 画面实际像素尺寸 -> viewBox 单位换算：连线留白按「像素半径」算，才不会压到图标上
  const _hr = host.getBoundingClientRect ? host.getBoundingClientRect() : null;
  const _sw = (_hr && _hr.width) ? _hr.width : 900;
  const _savg = ((_sw / W) + (560 / H)) / 2 || 0.92;
  const n = devs.length;
  const rx = 400, ry = n > 8 ? 238 : 205;
  // 图标大小随设备数量自适应：设备越多，默认（待机）尺寸越小
  const liveS = Math.max(0.60, Math.min(1, 1 - Math.max(0, n - 6) * 0.05));
  const idleS = (liveS * 0.50).toFixed(3);
  const CICO_R = 17, CPANEL_R = 59;   // 待机盾牌半径 / 放大后中心面板半径（px，与 CSS 尺寸对应）
  // 连线两端留白（viewBox 单位）= 中心面板半径 / 设备徽标半径 + 一点余量；
  // 待机图标小则留白小，放大态按放大后的图标留白，两套值之间平滑过渡
  // 连线两端留白（viewBox 单位）：精确等于「中心面板 / 设备徽标」的半径，
  // 让线端正好贴在图标边缘（略微探入不透明徽标下沿），视觉上就是严丝合缝地连上。
  const gaps = {
    cIdle: (CICO_R + 1) / _savg, cLive: (CPANEL_R + 1) / _savg,
    nIdle: (27 * parseFloat(idleS) - 2) / _savg,
    nLive: (27 * liveS * 1.16 - 2) / _savg,
  };
  // 基准位置归一化到 0..1，便于漂移时同步更新连线端点
  // 设备多时半径长短错落，避免所有连线等长、节点挤在同一圆环上
  const base = devs.map((d, i) => {
    const rb = radiusBias(i, n);
    const ang = (-90 + i * (360 / n)) * Math.PI / 180;
    return { x: (cx + rx * rb * Math.cos(ang)) / W, y: (cy + ry * rb * Math.sin(ang)) / H, rb };
  });
  let normal = 0, abnormal = 0;
  devs.forEach((d) => {
    if (d.status === "offline" || (d.health || 0) >= 2) abnormal++;
    else normal++;
  });

  let links = "", dots = "";
  devs.forEach((d, i) => {
    const b = base[i];
    const col = topoHealthColor(d);
    const off = d.status === "offline" ? " off" : "";
    const id = escapeHtml(d.id);
    const bow = (0.10 + Math.random() * 0.16) * (Math.random() < 0.5 ? -1 : 1);  // 任意方向弯曲
    const wob = Math.random() * 2 - 1;                                            // S 形扭转
    const dpath = curvedPath(cx, cy, b.x * W, b.y * H, bow, wob, gaps.cIdle, gaps.nIdle);
    links += `<path id="tl-${id}" class="topo-link${off}" data-id="${id}" data-bow="${bow.toFixed(3)}" data-wob="${wob.toFixed(3)}" d="${dpath}" stroke="${col}" />`;
    if (!off) {
      const dur = (3.4 + (i % 5) * 0.34).toFixed(2);
      const begin = (i * 0.32).toFixed(2);
      dots += `<circle class="topo-dot" r="2.4" style="color:${col}" data-id="${id}">`
        + `<animateMotion dur="${dur}s" begin="${begin}s" repeatCount="indefinite"><mpath xlink:href="#tl-${id}"></mpath></animateMotion></circle>`;
    }
  });

  let nodesHtml = "";
  devs.forEach((d, i) => {
    const b = base[i];
    const col = topoHealthColor(d);
    const off = d.status === "offline" ? " off" : "";
    const icon = topoDeviceIcon(d, col);
    const name = escapeHtml(deviceDisplayName(d).slice(0, 10));
    const statusTxt = d.status === "offline" ? "离线" : (d.health_label || "正常");
    const st = d.status === "offline" ? "off" : ((d.health || 0) >= 2 ? "warn" : "ok");
    nodesHtml += `<div class="topo-node ${st}${off}" data-id="${escapeHtml(d.id)}" style="left:${(b.x * 100).toFixed(3)}%;top:${(b.y * 100).toFixed(3)}%;--col:${col};--d:${(i * 0.06).toFixed(2)}s;--i:${i}">
        <div class="tn-outer"><div class="tn-inner">
          <div class="tn-ico">${icon}</div>
          <div class="tn-name">${name}</div>
          <div class="tn-status">${escapeHtml(statusTxt)}</div>
        </div></div>
      </div>`;
  });

  host.innerHTML = `
    <div class="topo-stage" id="topoStage"
         style="--s-idle:${idleS};--s-live:${liveS}">
      <canvas class="topo-bg" id="topoBg"></canvas>
      <svg class="topo-links-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" xmlns:xlink="http://www.w3.org/1999/xlink">
        <g class="topo-links">${links}</g>
        <g class="topo-dots">${dots}</g>
      </svg>
      <div class="topo-nodes-layer">${nodesHtml}</div>
      <div class="topo-center-panel">
        <div class="tc-pulse"></div>
        <div class="tc-shine"></div>
        <div class="tc-ico"><svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="#2fe0a0" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2.5 20 6v5.5c0 5-3.4 8.6-8 10-4.6-1.4-8-5-8-10V6z"/><path d="M9.5 12l2 2 3.5-4"/></svg></div>
        <div class="tc-mini"><b>${devs.length}</b></div>
        <div class="tc-title">监控中心</div>
        <div class="tc-stats">
          <div class="tc-stat"><b>${devs.length}</b><span>连接</span></div>
          <div class="tc-stat ok"><b>${normal}</b><span>正常</span></div>
          <div class="tc-stat bad"><b>${abnormal}</b><span>异常</span></div>
        </div>
      </div>
    </div>
    ${devs.some((d) => String(d.id || "").indexOf("demo-") === 0) ? '<p class="topo-hint">演示模式：下面含虚拟设备</p>' : ""}`;

  const stage = host.querySelector("#topoStage");
  startTopoParticles(host.querySelector("#topoBg"), stage);

  const items = [];
  // 监控中心固定在正中（不再绕小圈漂移），但要排在 items 首位：
  // 每帧先算出它的实际位置，所有连接线的起点都跟随它，不会再出现「圆形空心洞」
  const centerEl = stage.querySelector(".topo-center-panel");
  if (centerEl) {
    items.push({
      el: centerEl, link: null, isCenter: true,
      bx: 0.5, by: 0.5, dx: 0, dy: 0, ang: 0, spd: 0, ampK: 0,
      z: 1, zT: 1, zN: 9e15, mode: "cart",
      aOff: 0, rOff: 0, zr: 0, aV: 0, rV: 0,
      bow0: 0, wob0: 0, bow: 0, wob: 0, bowT: 0, wobT: 0, bN: 9e15, phase: 0,
    });
  }
  stage.querySelectorAll(".topo-node").forEach((g, i) => {
    const id = g.dataset.id;
    const link = stage.querySelector(`.topo-link[data-id="${id}"]`);
    const bow0 = link ? +link.dataset.bow : 0.14;
    const wob0 = link ? +link.dataset.wob : 0;
    items.push({
      el: g, link,
      bx: base[i].x, by: base[i].y,
      dx: 0, dy: 0,
      // 粒子流三要素：方向（连续缓变）、慢速（约 3~7 px/秒，与背景粒子同量级）、景深
      ang: Math.random() * Math.PI * 2,
      spd: 0.00008 + Math.random() * 0.00010,             // 约 3~7 px/秒，与背景粒子同速
      z: 0.35 + Math.random() * 0.65,
      zT: 0.35 + Math.random() * 0.65, zN: 0,
      ampK: 1,
      bow0: bow0, wob0: wob0, bow: bow0, wob: wob0, gc: gaps.cIdle, gn: gaps.nIdle,
      // 极坐标粒子漂流：绕中心缓慢滑行 + 径向浮动（位置在整幅画面里持续变化）
      mode: "polar", a0: Math.atan2(base[i].y * H - cy, base[i].x * W - cx),
      rBase: base[i].rb,
      aOff: 0, rOff: 0, zr: 0,
      aV: (Math.random() - .5) * 0.0024, rV: (Math.random() - .5) * 0.0018,
      bowT: bow0, wobT: wob0, bN: 0,
      phase: Math.random() * Math.PI * 2,                 // 每条线独立相位
    });
    // 首帧就把景深系数写进样式，避免一次缩放突跳（位置始终不动）
    g.style.setProperty("--zs", (0.86 + items[items.length - 1].z * 0.28).toFixed(3));

    g.addEventListener("mouseenter", () => {
      // 只让节点内容发光放大；连线保持粒子流那种细线，不再扭动也不加粗
      g.classList.add("hot");
    });
    g.addEventListener("mouseleave", () => {
      g.classList.remove("hot");
    });
    g.addEventListener("click", () => {
      const outer = g.querySelector(".tn-outer");
      if (outer) { outer.classList.remove("flash"); void outer.offsetWidth; outer.classList.add("flash"); }
      g.classList.remove("pop"); void g.offsetWidth; g.classList.add("pop");
      selectDevice(id);
    });
  });

  // 整幅画面：进入 -> 全部变大；离开 -> 回到粒子流缩小态
  stage.addEventListener("mouseenter", () => stage.classList.add("live"));
  stage.addEventListener("mouseleave", () => {
    stage.classList.remove("live");
    items.forEach((it) => { it.el.classList.remove("hot"); });
  });

  startTopoDrift(stage, items, { W, H, cx, cy, rx, ry, n, gaps });
}

function selectDevice(id, opts) {
  const dev = findDev(_consoleData, id);
  if (!dev) return;
  _selectedDevId = id;
  document.querySelectorAll(".topo-node").forEach((n) => {
    const sel = n.dataset.id === id;
    n.classList.toggle("selected", sel);
    if (sel && !(opts && opts.quiet)) { n.classList.remove("flip"); void n.offsetWidth; n.classList.add("flip"); }  // 立体翻转
  });
  renderDeviceDetail(dev);
  // 选中后把设备详情滚动进视口并高亮，避免被上方动画/长页面推到视口外（点了却看不到内容）
  const box = $("deviceDetail");
  if (box) {
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    box.classList.remove("flash"); void box.offsetWidth; box.classList.add("flash");
  }
}

function renderDeviceDetail(dev, target) {
  const box = target ? $(target) : $("deviceDetail");
  if (!box) return;
  box.style.display = "block";
  const offline = dev.status === "offline";
  const hc = healthClass(dev.health);
  const hLabel = HEALTH_LABEL[dev.health] || "正常";
  const brand = dev.brand_label || dev.brand || "未知设备";
  const sm = dev.smart || {};
  const sn = dev.snapshot || {};
  const guard = dev.guard || {};
  const typeLabel = dev.type === "local" ? "本机" : "远程设备";

  const smStat = sm.available
    ? `<div class="dd-stat ${sm.bad ? "bad" : sm.warn ? "warn" : "ok"}">
         <div class="k">硬盘健康</div>
         <div class="v">${sm.disk_count} <small>块</small></div>
         <div class="k">${sm.bad ? sm.bad + " 异常" : sm.warn ? sm.warn + " 注意" : "全部良好"}</div>
       </div>`
    : `<div class="dd-stat"><div class="k">硬盘健康</div><div class="v">—</div><div class="k">本机不适用 / 不可用</div></div>`;

  const snapStat = `<div class="dd-stat ${sn.unprotected_units > 0 ? "warn" : "ok"}">
      <div class="k">快照防勒索</div>
      <div class="v">${sn.protected_units}<small>/${sn.total_units || 0} 卷</small></div>
      <div class="k">${sn.snap_count ? sn.snap_count + " 张快照" : "暂无快照"}${sn.unprotected_units > 0 ? " · " + sn.unprotected_units + " 卷未保护" : ""}</div>
    </div>`;

  const guardStat = `<div class="dd-stat ${guard.level === "bad" ? "bad" : guard.level === "warn" ? "warn" : "ok"}">
      <div class="k">防护状态</div>
      <div class="v" style="font-size:15px">${escapeHtml(guard.label || "正常")}</div>
    </div>`;

  const healthStat = `<div class="dd-stat ${hc}">
      <div class="k">综合健康</div>
      <div class="v" style="font-size:15px">${hLabel}</div>
    </div>`;

  let actions = "";
  if (!dev.__demo) {
    actions += `<button class="btn ghost sm" id="ddRenameBtn">✎ 改名</button>`;
  }
  if (dev.type !== "local" && !dev.__demo) {
    actions += `<button class="btn ghost sm" id="ddRemoveBtn">移除设备</button>`;
  }
  if (offline) {
    actions = `<span class="muted">设备离线，无法连接（${escapeHtml(dev.note || "")}）</span>` + actions;
  }

  // 轻量代理状态：本机即监控中心（已装）；远程设备显示 未装/待装/已装，未装可补装
  const ag = dev.agent || {};
  const agState = ag.status === "installed" ? "installed" : ag.status === "pending" ? "pending" : "none";
  const agChip = dev.type === "local" ? "" :
    `<span class="dd-agent ${agState}" title="轻量代理：装好后中控可帮它做快照防勒索、换机迁移">${agState === "installed" ? "代理 已装" : agState === "pending" ? "代理 待装" : "代理 未装"}</span>`;
  if (dev.type !== "local" && !dev.__demo && agState !== "installed") {
    actions += `<button class="btn ghost sm" id="ddAgentBtn">${agState === "pending" ? "查看安装命令" : "安装轻量代理"}</button>`;
  }

  // 进入按钮放在标题行右侧（dd-side）；联机设备点它是打开「设备管控」页，
  // 离线时也要能点（正是离线才需要去检查地址/代理），所以不再置灰。
  const canEnter = dev.type === "local" || !!dev.host;
  const enterLabel = dev.type === "local" ? "进入本机" : "设备管控";
  const enterBtnHtml = canEnter
    ? `<button class="btn primary sm" id="ddSideBtn">${enterLabel}</button>`
    : "";

  // 远程且还不是完整服务端：提示可升级为完整版（装成开机自启服务，全功能原生可用）。
  // 不限 Windows —— 任何被监控 / 只装代理的联机设备都可装成独立控制台。
  const canFull = dev.type !== "local" && !dev.full_server;
  let fullCard = "";
  if (canFull) {
    fullCard = `<div class="dd-full-install">
      <div class="ddfi-title">🪟 想在这台设备上用全部功能？</div>
      <p class="muted" style="margin:6px 0 10px">现在这台设备只是被监控（或只装了轻量代理）：快照只能看、迁移/清理/日报都跑在主机上，它自己用不了。
        把它装成<strong>完整的 TS Safe（开机自启服务）</strong>后，这台设备就是一台独立控制台，
        迁移、快照、重复文件清理、磁盘清理、日报……全都原生可用，不再依赖别的设备来代管。</p>
      <button class="btn primary sm" id="ddFullBtn">⬇ 下载完整版安装包（含安装引导）</button>
      <span class="muted" style="margin-left:8px">安装包里内置了 Windows / Linux / macOS 的安装脚本与图文引导，下载后按提示装即可</span>
    </div>`;
  }

  box.innerHTML = `
    <div class="dd-head">
      <div class="dd-title">
        <span class="dd-name">${escapeHtml(deviceDisplayName(dev))}</span>
        <span class="dd-meta">${escapeHtml(brand)} · ${typeLabel}${dev.group ? " · " + escapeHtml(dev.group) : ""}</span>
      </div>
      <div class="dd-side">
        ${enterBtnHtml}
        <span class="dd-status ${offline ? "off" : "on"}">${offline ? "● 离线" : "● 在线"}</span>
        ${agChip}
      </div>
    </div>
    <div class="dd-grid">
      ${healthStat}
      ${smStat}
      ${snapStat}
      ${guardStat}
    </div>
    <div class="dd-actions">${actions}</div>
    ${fullCard}`;

  const sideBtn = box.querySelector("#ddSideBtn");
  if (sideBtn) sideBtn.onclick = () => {
    openDeviceControl(dev);
  };
  const fullBtn = box.querySelector("#ddFullBtn");
  if (fullBtn) fullBtn.onclick = () => {
    downloadFullBundle(dev.name || "");
  };
  const agBtn = box.querySelector("#ddAgentBtn");
  if (agBtn) agBtn.onclick = () => openAgentModal(dev);
  const rn2 = box.querySelector("#ddRenameBtn");
  if (rn2) rn2.onclick = () => openRenameModal(dev.id);
  const rm = box.querySelector("#ddRemoveBtn");
  if (rm) rm.onclick = async () => {
    if (!confirm("确定要移除这台设备吗？")) return;
    try {
      await api("/api/devices/remove", { method: "POST", body: JSON.stringify({ id: dev.id }) });
      toast("已移除设备", "ok");
      loadConsole(true);
    } catch (e) { toast("移除失败：" + e.message, "err"); }
  };
}

/* 补装/查看轻量代理：给该设备生成专属一键安装命令，代理跑起来会自动回连中控变「已装」 */
async function openAgentModal(dev) {
  let ag = dev.agent || {};
  try {
    const r = await api("/api/devices/agent", { method: "POST", body: JSON.stringify({ id: dev.id }) });
    if (r && r.agent) { ag = r.agent; dev.agent = r.agent; }
  } catch (e) { /* 令牌获取失败时下面降级提示 */ }
  const token = ag.token || "";
  const base = `${location.protocol}//${location.host}`;
  const cmd = token ? `curl -sS "${base}/api/agent/install.sh?t=${token}" | sh` : "";
  const cmdPs = token ? `powershell -NoProfile -ExecutionPolicy Bypass -Command "irm '${base}/api/agent/install.ps1?t=${token}' | iex"` : "";
  // 不想登到目标设备上去敲命令时：在自己这台电脑上用 SSH 远程执行（Linux/NAS/云服务器可用）
  const devHost = (dev.host || "").trim();
  const cmdSsh = token && devHost
    ? `ssh 用户名@${devHost} "curl -sS '${base}/api/agent/install.sh?t=${token}' | sh"`
    : "";
  const cmdRow = (label, c, id) => c
    ? `<div class="set-row" style="align-items:flex-start"><span class="set-label" style="min-width:56px">${label}</span>
       <div class="agent-cmd" style="flex:1"><code id="${id}">${escapeHtml(c)}</code><button class="btn ghost sm" data-act="copy_${id}">复制</button></div></div>`
    : "";
  openModal(
    "📡 安装轻量代理",
    `<p class="muted" style="margin:0 0 8px">给 <b>${escapeHtml(deviceDisplayName(dev))}</b> 装上轻量代理后，中控就能帮它做快照防勒索、换机迁移等完整控制。<br>
     装法：登到那台设备（Windows 用 PowerShell，NAS/群晖/云服务器用 SSH），粘贴运行对应的命令。代理只回连报到（每分钟一次），不会改对端的任何配置。<br>Windows 运行后会自动下载「桌面助手」（右下角蓝紫色盾牌图标，有界面能看到提醒），代理心跳由它一并负责。</p>
     ${cmdRow("Linux/Mac", cmd, "agentCmdSh") || `<p class="muted">安装命令生成失败（令牌没拿到），请关掉重试。</p>`}
     ${cmdRow("Windows", cmdPs, "agentCmdPs")}
     ${cmdRow("远程装", cmdSsh, "agentCmdSsh")}
     <p class="muted" style="margin:10px 0 0;line-height:1.8">
       <b>能不能由这台电脑帮别的设备装？</b><br>
       · Linux / 群晖 / 威联通 / 云服务器：能。用上面「远程装」那条，把「用户名」换成对方的 SSH 账号，在<b>这台电脑</b>上执行一次就行，不用登到对方机器。<br>
       · Windows：不行。Windows 没有对外开放的远程执行通道，必须<b>在那台电脑的 PowerShell 里</b>跑一次「Windows」那条命令（建议管理员身份）。<br>
       这是有意的安全设计：中控不能凭空拿到别人设备的控制权，总得有一次「本机同意」。
     </p>`,
    `<button class="btn ghost" data-act="close">关闭</button>
     ${cmd ? `<button class="btn primary" data-act="checkagent">我已运行，检测一下</button>` : ""}`,
    {
      copy_agentCmdSh: async () => {
        try { await navigator.clipboard.writeText(cmd); toast("Linux 命令已复制，去那台设备上粘贴运行", "ok"); }
        catch (e) { toast("自动复制失败，请手动选中命令复制", "warn"); }
      },
      copy_agentCmdPs: async () => {
        try { await navigator.clipboard.writeText(cmdPs); toast("Windows 命令已复制，到 PowerShell 里粘贴运行（建议管理员身份）", "ok"); }
        catch (e) { toast("自动复制失败，请手动选中命令复制", "warn"); }
      },
      copy_agentCmdSsh: async () => {
        try { await navigator.clipboard.writeText(cmdSsh); toast("已复制：把「用户名」换成对方的 SSH 账号后，在这台电脑上执行", "ok"); }
        catch (e) { toast("自动复制失败，请手动选中命令复制", "warn"); }
      },
      checkagent: async () => {
        try {
          const data = await api("/api/devices/refresh", { method: "POST" });
          const nd = findDev(data, dev.id);
          if (nd && nd.agent && nd.agent.status === "installed") {
            toast(`「${deviceDisplayName(nd)}」代理已装好`, "ok");
            closeModal();
          } else {
            toast("还没检测到代理回连：确认命令已在那台设备上运行成功，且能访问中控地址", "warn");
          }
          loadConsole(true);
        } catch (e) { toast("检测失败：" + e.message, "err"); }
      },
    }
  );
}

function findDev(data, id) {
  for (const ds of (data.devices || [])) if (ds.id === id) return ds;
  return null;
}

function deviceDisplayName(d) {
  if (d && d.type === "local") {
    if (d.custom_name && d.name) return d.name;          // 用户改过名就用他起的名字
    const b = (d.brand_label || d.brand || "").trim();
    return b ? b + " · 本机" : "本机";
  }
  return (d && d.name) || "设备";
}

/* 扫描结果里按品牌挑个像样的机型图标 */
function topoKindFromBrand(brand) {
  const b = String(brand || "").toLowerCase();
  if (b.includes("qnap")) return "nas";
  if (b.includes("synology")) return "nas";
  if (b.includes("ugreen") || b.includes("feiniu") || b.includes("truenas") || b.includes("omv") || b.includes("unraid")) return "nas";
  if (b.includes("windows")) return "pc";
  if (b.includes("macos") || b.includes("darwin")) return "laptop";
  if (b.includes("aliyun") || b.includes("ecs")) return "server";
  return "chip";
}

/* 硬盘监测一行文案：netTile 与无感刷新的原位更新共用，保证两边永远一致 */
function netDiskText(sm) {
  return sm.available
    ? `💽 ${sm.disk_count} 块${sm.bad ? " · " + sm.bad + " 块异常" : sm.warn ? " · " + sm.warn + " 块注意" : " · 全部良好"}`
    : "💽 硬盘监测不可用";
}

/* 联网设备用「图标框」排列：一眼看到每台的机型、名字与监控状态 */
function netTile(d) {
  const hc = healthClass(d.health);
  const hLabel = HEALTH_LABEL[d.health] || "正常";
  const col = topoHealthColor(d);
  const kind = topoDeviceKind(d);
  const ring = TOPO_KIND_COLORS[kind] || TOPO_KIND_COLORS.chip;
  const offline = d.status === "offline";
  const sm = d.smart || {}, sn = d.snapshot || {};
  const diskTxt = netDiskText(sm);
  const snapTxt = `⛨ ${sn.protected_units || 0}/${sn.total_units || 0} 卷受保护`;
  const gname = (d.group && d.group !== "本地设备" && d.group !== "远程设备") ? d.group : "";
  return `<div class="net-tile ${hc} ${offline ? "offline" : ""}" data-net="${escapeHtml(d.id)}" style="--col:${col};--ring:${ring}">
      ${gname ? `<span class="nt-tag">${escapeHtml(gname)}</span>` : ""}
      <div class="nt-badge">${topoDeviceGlyph(kind)}</div>
      <div class="nt-name" title="${escapeHtml(deviceDisplayName(d))}">${escapeHtml(deviceDisplayName(d))}<button class="nm-edit" data-rename="${escapeHtml(d.id)}" title="给这台改名">✎</button></div>
      <div class="nt-state"><span class="nt-dot"></span>${offline ? "离线" : escapeHtml(hLabel)}</div>
      <div class="nt-metrics"><span>${diskTxt}</span><span>${snapTxt}</span></div>
      <div class="nt-foot"><button class="btn ghost sm" data-net-open="${escapeHtml(d.id)}">设备管控</button></div>
    </div>`;
}


/* ---------- 联机设备状态总览（「联机设备」页） ---------- */

// 打开一台设备的「管控页」。
// 以前是 window.open 对端的局域网地址（http://192.168.x.x:8848），从外网/云端的
// 控制台点开根本连不上。现在统一改成：在本控制台里打开这台设备的管控页面，
// 功能设置都在当前控制台完成，不依赖对端地址能不能访问。
function openDeviceControl(dev) {
  if (!dev) return;
  if (dev.type === "local") { showView("home"); return; }
  focusManage(dev.id);
}

// 从某一台具体设备（拓扑节点 / 总览卡片 / 详情）进入管控页：只渲染该设备，不显示本机。
// _enteringFromDevice 防止 showView 内部首次渲染与本次二次渲染重复清空焦点（旧版 bug：两次
// renderManage 消费同一临时变量，第二次已空 → 又显示全部设备）。
function focusManage(id) {
  _manageFocusId = id;
  _enteringFromDevice = true;
  showView("settings");
  showSettingsTab("manage");
  _enteringFromDevice = false;
}

// 联机设备总览：复用 /api/devices 的聚合结果，按卡片网格呈现所有已联机设备
async function loadDeviceOverview(force) {
  const box = $("deviceOverview");
  // 无感刷新：已渲染过就不清空转圈，拉到新数据原位更新
  const firstLoad = !box || !box.querySelector(".ov-card");
  if (box && firstLoad) box.innerHTML = `<p class="muted"><span class="spinner"></span>正在汇总联机设备…</p>`;
  try {
    const data = await api("/api/devices" + (force ? "?force=1" : ""), {}, 20000);
    _overviewData = data;
    renderDeviceOverview(data);
  } catch (e) {
    if (box && firstLoad) box.innerHTML = `<p class="muted">加载失败：${escapeHtml(e.message || "")}</p>`;
  }
}

let _overviewData = null;
let _overviewSig = "";
let _manageFocusId = "";   // 从具体设备入口进入管控页时，只渲染该设备（持久，不自动清空）
let _enteringFromDevice = false; // focusManage 进入时为 true，侧栏直接进 manage 为 false

function renderDeviceOverview(data) {
  const box = $("deviceOverview");
  if (!box) return;
  const all = (data.devices || []);
  const cur = all.find((d) => d.type === "local") || all[0];
  const online = all.filter((d) => d.status === "online").length;
  if (!all.length) {
    box.innerHTML = `<p class="muted">还没有登记任何设备。到「功能设置 → 添加设备」里添加后这里会显示。</p>`;
    _overviewSig = "";
    return;
  }
  const ovSig = all.map((d) => [d.id, deviceDisplayName(d), d.status, d.health || 0,
    (d.agent && d.agent.status) || "", d.brand_label || d.brand || ""].join(":")).join("|");
  if (ovSig === _overviewSig && box.querySelector(".ov-card")) {
    // 无感刷新：卡片没增减，只原位更新状态与代理徽标
    for (const d of all) updateOverviewCard(d);
    const barCnt = box.querySelector(".ov-bar span");
    if (barCnt) barCnt.innerHTML = `共 <b>${all.length}</b> 台 · 在线 <b>${online}</b> 台`;
    return;
  }
  _overviewSig = ovSig;
  let html = `<div class="ov-bar">
      <span>共 <b>${all.length}</b> 台 · 在线 <b>${online}</b> 台</span>
      <span class="ov-hint">点击任一台看详情 · 功能设置到「功能设置」里单独配</span>
    </div><div class="ov-grid">`;
  for (const d of all) {
    const offline = d.status === "offline";
    const hc = healthClass(d.health);
    const hLabel = HEALTH_LABEL[d.health] || "正常";
    const brand = d.brand_label || d.brand || "未知设备";
    const ag = d.agent || {};
    const agState = ag.status === "installed" ? "installed" : ag.status === "pending" ? "pending" : "none";
    const agChip = d.type === "local" ? `<span class="ov-agent ok">代理 已装</span>`
      : `<span class="ov-agent ${agState}">${agState === "installed" ? "代理 已装" : agState === "pending" ? "代理 待装" : "代理 未装"}</span>`;
    const canEnter = d.type === "local" || !!d.host;
    html += `<div class="ov-card ${hc} ${offline ? "offline" : ""}" data-ov="${escapeHtml(d.id)}">
        <div class="ov-card-head">
          <span class="brand-badge">${escapeHtml(brand)}</span>
          <span class="dev-status ${offline ? "off" : "on"}">${offline ? "离线" : "在线"}</span>
        </div>
        <div class="ov-card-name">${escapeHtml(deviceDisplayName(d))}</div>
        <div class="ov-card-health"><span class="dev-health-dot ${hc}"></span><span>${hLabel}</span></div>
        <div class="ov-card-foot">${agChip}</div>
        <div class="ov-card-actions">
          <button class="btn ghost sm" data-ov-detail="${escapeHtml(d.id)}">查看详情</button>
          <button class="btn primary sm" data-ov-set="${escapeHtml(d.id)}">功能设置</button>
          ${canEnter ? `<button class="btn ghost sm" data-ov-enter="${escapeHtml(d.id)}">${d.type === "local" ? "进入本机" : "设备管控"}</button>` : ""}
        </div>
      </div>`;
  }
  html += `</div>`;
  box.innerHTML = html;
  box.querySelectorAll("[data-ov-detail]").forEach((b) => {
    b.onclick = () => { const d = findDev(_overviewData, b.dataset.ovDetail); if (d) openDeviceDetailModal(d); };
  });
  box.querySelectorAll("[data-ov-set]").forEach((b) => {
    b.onclick = () => { focusManage(b.dataset.ovSet); };
  });
  box.querySelectorAll("[data-ov-enter]").forEach((b) => {
    b.onclick = () => {
      const d = findDev(_overviewData, b.dataset.ovEnter);
      if (!d) return;
      openDeviceControl(d);
    };
  });
}

/* 原位更新一张总览卡片：健康色 / 在线状态 / 代理徽标（不动按钮，绑定不丢） */
function updateOverviewCard(d) {
  const c = document.querySelector(`.ov-card[data-ov="${CSS.escape(d.id)}"]`);
  if (!c) return;
  const offline = d.status === "offline";
  const hc = healthClass(d.health);
  const hLabel = HEALTH_LABEL[d.health] || "正常";
  const ag = d.agent || {};
  const agState = ag.status === "installed" ? "installed" : ag.status === "pending" ? "pending" : "none";
  const agChip = d.type === "local" ? `<span class="ov-agent ok">代理 已装</span>`
    : `<span class="ov-agent ${agState}">${agState === "installed" ? "代理 已装" : agState === "pending" ? "代理 待装" : "代理 未装"}</span>`;
  c.className = `ov-card ${hc} ${offline ? "offline" : ""}`;
  const stEl = c.querySelector(".ov-card-head .dev-status");
  if (stEl) { stEl.className = `dev-status ${offline ? "off" : "on"}`; stEl.textContent = offline ? "离线" : "在线"; }
  const hEl = c.querySelector(".ov-card-health");
  if (hEl) hEl.innerHTML = `<span class="dev-health-dot ${hc}"></span><span>${hLabel}</span>`;
  const fEl = c.querySelector(".ov-card-foot");
  if (fEl) fEl.innerHTML = agChip;
}

// 单台设备详情：复用 renderDeviceDetail，装进弹窗方便从总览直接看具体数据
function openDeviceDetailModal(dev) {
  openModal(
    "设备详情 · " + deviceDisplayName(dev),
    `<div id="deviceDetailModal" style="display:block"></div>`,
    `<button class="btn ghost" data-act="close">关闭</button>`,
    {}
  );
  renderDeviceDetail(dev, "deviceDetailModal");
}

/* ---------- 添加设备：先自动扫一遍（局域网 + 异地组网），扫不到再手填 ---------- */
const SCAN_KIND_LABEL = { lan: "局域网", vpn: "异地组网", manual: "手填网段" };

// 从「联机设备管控」进入某个功能页面，并预选中目标设备。
// local -> 本机操作；完整服务端 -> 远程代理操作；轻量代理 -> 提示需装完整版。
function openToolForDevice(tool, devId) {
  const d = (state.remoteDevices || []).find((x) => x.id === devId);
  if (devId === "local") {
    if (tool === "dups") state.dupTarget = "local";
    if (tool === "junk") state.junkTarget = "local";
  } else if (d && d.full_server) {
    if (tool === "dups") state.dupTarget = devId;
    if (tool === "junk") state.junkTarget = devId;
  } else {
    toast("该设备为轻量代理，需先「装完整版」才能远程执行清理/迁移功能", "warn");
    return;
  }
  const tabMap = { dups: "dups", junk: "junk", migrate: "migrate", autosnap: "autosnap" };
  const tab = tabMap[tool] || "dups";
  showView("settings");
  showSettingsTab(tab);
}

// 联机设备管控（设置后台）：在线统计 + 每台设备的状态/代理/功能入口；
// 功能不再在这里勾选开关，而是点功能名进入对应页面操作。
async function renderManage() {
  const sumEl = $("manageSummary");
  const listEl = $("manageList");
  if (!listEl) return;
  listEl.innerHTML = `<p class="muted"><span class="spinner"></span>正在加载联机设备…</p>`;
  let data;
  try { data = await api("/api/devices/manage", {}, 15000); }
  catch (e) { listEl.innerHTML = `<p class="muted">加载失败：${escapeHtml(e.message || "")}</p>`; return; }
  // 从具体设备入口进入时（_manageFocusId 非空），只显示该设备，不显示本机。
  // 该变量持久保留、不消费清空；从左侧菜单「联机设备管控」进入时由 showSettingsTab 清除。
  let devices = data.devices || [];
  if (_manageFocusId) {
    const filtered = devices.filter((d) => d.id === _manageFocusId);
    if (filtered.length) {
      devices = filtered;
      if (sumEl) sumEl.innerHTML = `设备管控 · <b>${escapeHtml(devices[0].name || "")}</b> <a class="mg-back" data-m-back style="margin-left:8px;cursor:pointer;color:#1565c0">← 返回全部设备</a>`;
    } else if (sumEl) {
      sumEl.innerHTML = `设备管控 · 设备已移除 <a class="mg-back" data-m-back style="margin-left:8px;cursor:pointer;color:#1565c0">← 返回全部设备</a>`;
    }
  } else if (sumEl) {
    sumEl.innerHTML = `联机设备 <b>${data.total}</b> 台 · 在线 <b>${data.online}</b> 台`;
  }
  // 把管控页拿到的设备列表缓存起来，供「点击功能按钮跳转时」预选中目标设备
  state.remoteDevices = devices.filter((d) => d.type !== "local");
  if (!data.devices || !data.devices.length) {
    listEl.innerHTML = `<p class="muted">还没有登记任何设备。到「＋ 添加设备」里添加，并给设备装上轻量代理后即可在此集中管控。</p>`;
    return;
  }
  const FEAT_CFG = {
    dups: { label: "重复文件清理", tab: "dups", remote: true },
    junk: { label: "磁盘清理", tab: "junk", remote: true },
    migrate: { label: "换机迁移", tab: "migrate", remote: false },
    autosnap: { label: "自动快照", tab: "autosnap", remote: false },
  };
  const FEAT_KEYS = ["dups", "junk", "migrate", "autosnap"];
  let html = "";
  for (const d of devices) {
    const offline = d.status !== "online";
    const agState = d.agent_status === "installed" ? "installed" : d.agent_status === "pending" ? "pending" : "none";
    const noAgent = agState !== "installed" && !d.full_server;
    const canRemote = d.type === "local" || d.full_server;
    const monOnly = !!d.monitor_only;
    let feats = "";
    if (monOnly) {
      feats = `<p class="muted" style="margin:0;font-size:12px">📡 仅监控设备：这台设备上没有 TS Safe，只探测在线状态；清理 / 迁移 / 快照等功能不适用。</p>`;
    } else {
      for (const k of FEAT_KEYS) {
        const cfg = FEAT_CFG[k];
        const disabled = !canRemote || offline;
        const title = offline ? "设备离线" : (!canRemote ? "需先安装完整版才能远程操作" : "");
        feats += `<button class="btn ghost sm mg-feat-btn" style="margin:0 6px 4px 0" data-m-tool="${k}" data-m-dev="${escapeHtml(d.id)}"${disabled ? ` disabled title="${title}"` : ""}>${cfg.label}</button>`;
      }
    }
    // 本机：进本机总览；联机设备：这里已经是管控页了，按钮改为去看它的快照
    const enterBtn = d.type === "local"
      ? `<button class="btn ghost sm" data-m-enter="${escapeHtml(d.id)}">进入本机</button>`
      : (monOnly ? "" : `<button class="btn ghost sm" data-m-snap="${escapeHtml(d.id)}">查看快照</button>`);
    const agentBtn = monOnly
      ? `<span class="mg-agent" style="opacity:.7">📡 仅监控</span>`
      : (d.full_server
        ? `<span class="mg-agent ok">完整服务端</span>`
        : (noAgent
          ? `<button class="btn primary sm" data-m-agent="${escapeHtml(d.id)}">安装轻量代理</button>`
          : `<span class="mg-agent ok">代理 已装</span>`));
    // 删除设备：本机不能被删（后端同样拒绝），其余设备（在线/离线都行）随时可移除
    const delBtn = d.type === "local"
      ? ""
      : `<button class="btn ghost sm mg-del" data-m-del="${escapeHtml(d.id)}" title="把这台设备从列表里移除，以后可以重新添加">删除设备</button>`;
    // 远程且还不是完整服务端：提示装完整版，才能远程使用清理功能
    const fullBtn = (d.type === "remote" && !d.full_server && !monOnly)
      ? `<button class="btn ghost sm" data-m-full="${escapeHtml(d.id)}" data-m-name="${escapeAttr(d.name || '')}" title="装成开机自启的 TS Safe 服务（Windows/Linux/macOS 皆可），迁移/快照/清理/日报等全部原生可用；装完会自动回到本总控台登记并保持在线">🪟 装完整版</button>`
      : "";
    if (fullBtn) {
      feats = `<p class="muted" style="margin:0 0 6px;font-size:12px">轻量代理只能查看预览；点「🪟 装完整版」让这台设备自己跑引擎，就能远程清理/迁移了。</p>` + feats;
    }
    html += `<div class="mg-device" data-mid="${escapeHtml(d.id)}">
        <div class="mg-head">
          <span class="mg-name">${escapeHtml(d.name)}</span>
          <span class="dev-status ${offline ? "off" : "on"}">${offline ? "离线" : "在线"}</span>
          <span class="mg-agent ${monOnly ? "" : d.full_server ? "full" : agState}">${monOnly ? "📡 仅监控" : d.full_server ? "完整服务端" : agState === "installed" ? "代理 已装" : agState === "pending" ? "代理 待装" : "代理 未装"}</span>
        </div>
        <div class="mg-body">
          <div class="mg-feats">${feats}</div>
          <div class="mg-actions">${agentBtn}${enterBtn}${delBtn}${fullBtn}</div>
        </div>
      </div>`;
  }
  listEl.innerHTML = html;
  if (sumEl) {
    const back = sumEl.querySelector("[data-m-back]");
    if (back) back.onclick = () => { _manageFocusId = ""; renderManage(); };
  }
  listEl.querySelectorAll("[data-m-tool]").forEach((b) => {
    b.onclick = () => openToolForDevice(b.dataset.mTool, b.dataset.mDev);
  });
  listEl.querySelectorAll("[data-m-agent]").forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.mAgent;
      try {
        const r = await api("/api/devices/agent", { method: "POST", body: JSON.stringify({ id }) });
        const name = b.closest(".mg-device").querySelector(".mg-name").textContent;
        openAgentModal({ id, name, agent: (r && r.agent) || {} });
        renderManage();
      } catch (e) { toast("操作失败：" + e.message, "err"); }
    };
  });
  listEl.querySelectorAll("[data-m-enter]").forEach((b) => {
    b.onclick = () => showView("home");
  });
  listEl.querySelectorAll("[data-m-snap]").forEach((b) => {
    b.onclick = () => {
      showView("snapshots");
      selectSnapDevice(b.dataset.mSnap);
    };
  });
  listEl.querySelectorAll("[data-m-del]").forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.mDel;
      const box = b.closest(".mg-device");
      const nmEl = box && box.querySelector(".mg-name");
      const name = (nmEl && nmEl.textContent) || "这台设备";
      if (!confirm(`确定要从设备列表里删除「${name}」吗？\n\n删除后控制台不再纳管它，需要时可以重新添加。`)) return;
      b.disabled = true;
      try {
        await api("/api/devices/remove", { method: "POST", body: JSON.stringify({ id }) });
        toast("已删除设备", "ok");
        // 快照页若正看这台设备，回落到本机，避免指向已删设备
        if (state.snapDevice === id) { state.snapDevice = "local"; _snapDevList = []; }
        try { await loadDeviceOverview(true); } catch (e) { /* 刷新总览失败不影响删除结果 */ }
        renderManage();
      } catch (e) {
        b.disabled = false;
        toast("删除失败：" + (e.message || ""), "err");
      }
    };
  });
  listEl.querySelectorAll("[data-m-full]").forEach((b) => {
    b.onclick = () => downloadFullBundle(b.getAttribute("data-m-name") || "");
  });
}

function addDevice() { openAddDeviceModal(); }

function openAddDeviceModal() {
  openModal(
    "＋ 添加设备",
    `<div class="pair-card" id="pairCard" style="border:1px solid #cbd5e1;background:#f1f5f9;border-radius:12px;padding:14px 16px;margin-bottom:14px">
      <div style="font-weight:700;font-size:14px;margin-bottom:4px">🪟 Windows 电脑？一键配对，不用敲命令</div>
      <div id="pairBody">
        <p class="muted" style="margin:0 0 8px">点下方「Windows 配对」生成一个 6 位码；在电脑上打开「NAS Safe 桌面助手」并输入这个码，设备会自动出现并标记为在线——全程不用复制令牌、不用跑 PowerShell。</p>
        <p class="muted" style="margin:0"><b>已经有完整版 TS Safe 的电脑不需要配对</b>：那台电脑装完完整版就会自动回到本总控台登记并保持在线（不需要桌面助手、不需要配对码）。<b>配对只用于「还没装完整版、只装了轻量代理」的电脑。</b></p>
      </div>
    </div>
    <div class="scan-form">
      <p class="muted scan-tip">先自动扫一遍：会找出同网络里的所有设备。已装 TS Safe 的可以直接接管；没装的也能先加为监控设备，或者给那台设备安装轻量代理后再实现快照、迁移等完整控制。只读探测，不会改对端任何东西。<br>注：TS Safe 默认服务端口为 <b>8848</b>（本机控制台也在用）；若某台设备已占用该端口，添加时请在「端口」里填它的实际端口。</p>
      <div class="scan-grid">
        <label class="scan-row"><span>端口</span><input id="scanPort" class="text-input" value="8848" inputmode="numeric"></label>
        <label class="scan-row"><span>分组</span><input id="scanGroup" class="text-input" value="联网设备" placeholder="家里 / 公司 / 机房"></label>
        <label class="scan-row"><span>账号（选填）</span><input id="scanUser" class="text-input" placeholder="对端 TS Safe 的登录账号"></label>
        <label class="scan-row"><span>密码（选填）</span><input id="scanPwd" class="text-input" type="password" placeholder="对端登录密码"></label>
        <label class="scan-row wide"><span>额外网段</span><input id="scanCidrs" class="text-input" placeholder="如 100.64.0.0/10，多个用空格隔开"></label>
      </div>
      <div class="scan-opts">
        <label><input type="checkbox" id="scanLan" checked> 本机局域网</label>
        <label><input type="checkbox" id="scanVpn" checked> 异地组网（Tailscale / WireGuard / VPN 等）</label>
        <label title="认不出是什么设备时，交给 AI 看端口和网页标题判断"><input type="checkbox" id="scanAi" checked> 🤖 AI 辅助识别</label>
        <label title="给勾选要添加的设备装轻量代理，装好可做快照防勒索、换机迁移；之后也能在中控补装"><input type="checkbox" id="scanAgent"> 📡 安装轻量代理</label>
      </div>
    </div>
    <div class="scan-result" id="scanResult"></div>`,
    `<button class="btn ghost" data-act="close">关闭</button>
     <button class="btn ghost" data-act="manual">✍ 手动添加</button>
     <button class="btn ghost" data-act="pairing">🔑 Windows 配对</button>
     <button class="btn primary" data-act="scan">🔍 开始扫描</button>`,
    { scan: runNetScan, manual: openManualAdd, pairing: generatePairingCode }
  );
}

let _pairTimer = null, _pairPoll = null;

async function generatePairingCode() {
  if (_pairTimer) { clearInterval(_pairTimer); _pairTimer = null; }
  if (_pairPoll) { clearInterval(_pairPoll); _pairPoll = null; }
  const body = $("pairBody");
  if (body) body.innerHTML = `<p class="muted" style="margin:0">正在生成配对码…</p>`;
  try {
    const r = await api("/api/devices/pairing", { method: "POST", body: JSON.stringify({ name: "Windows 电脑" }) });
    if (r && r.ok) { renderPairingCode(r.code, r.id, r.expires_in); }
    else { toast((r && r.error) || "生成失败", "warn"); if (body) body.innerHTML = ""; }
  } catch (e) {
    toast("生成配对码失败：" + (e.message || ""), "warn");
    if (body) body.innerHTML = "";
  }
}

function renderPairingCode(code, id, ttl) {
  const body = $("pairBody");
  if (!body) return;
  let remain = ttl || 600;
  body.innerHTML = `<div style="font-size:40px;font-weight:800;letter-spacing:8px;color:#1d4ed8;text-align:center">${code}</div>
    <p class="muted" style="text-align:center;margin:6px 0">在电脑上打开「NAS Safe 桌面助手」，输入这 6 位数字即可配对</p>
    <div id="pairTimer" style="text-align:center;color:#64748b;font-size:13px">有效剩余 <b>${remain}</b> 秒</div>
    <div id="pairState" style="text-align:center;margin-top:6px;color:#64748b">等待配对…</div>`;
  _pairTimer = setInterval(() => {
    remain--;
    const t = $("pairTimer");
    if (t) t.innerHTML = `有效剩余 <b>${remain}</b> 秒`;
    if (remain <= 0) {
      clearInterval(_pairTimer); _pairTimer = null;
      const s = $("pairState");
      if (s) s.innerHTML = "⌛ 配对码已过期，点击下方「Windows 配对」重新生成";
    }
  }, 1000);
  _pairPoll = setInterval(async () => {
    try {
      const d = await api("/api/devices");
      const w = (d.devices || []).find((x) => x.id === id);
      const s = $("pairState");
      if (w && w.agent && w.agent.status === "installed") {
        clearInterval(_pairPoll); _pairPoll = null;
        if (_pairTimer) { clearInterval(_pairTimer); _pairTimer = null; }
        if (s) s.innerHTML = "✅ 已配对，设备已上线";
        toast("配对成功，设备已上线", "ok");
        loadConsole(true);
      } else if (w) {
        if (s) s.innerHTML = "⏳ 设备已登记，等待在电脑上输入配对码…";
      }
    } catch (e) {}
  }, 3000);
}

async function runNetScan() {
  const box = $("scanResult");
  if (!box) return;
  box.innerHTML = `<p class="muted"><span class="spinner"></span> 正在扫描网段，稍等几秒…</p>`;
  const g = (id) => $(id) || {};
  const body = {
    ports: [Number(g("scanPort").value || 8848) || 8848],
    include_lan: !!g("scanLan").checked,
    include_vpn: !!g("scanVpn").checked,
    user: g("scanUser").value || "",
    pwd: g("scanPwd").value || "",
    cidrs: g("scanCidrs").value || "",
    ai: !!g("scanAi").checked,
    discover: true,
  };
  try {
    const data = await api("/api/devices/scan", { method: "POST", body: JSON.stringify(body) }, 180000);
    renderScanResult(data);
  } catch (e) {
    box.innerHTML = `<p class="muted">扫描失败：${escapeHtml(e.message || "")}<br>也可以点「手动添加」直接填地址。</p>`;
  }
}

function renderScanResult(data) {
  const box = $("scanResult");
  if (!box) return;
  const found = data.found || [];
  if (!found.length) {
    box.innerHTML = `<p class="muted">扫了 ${data.scanned || 0} 个地址，没找到已装 TS Safe 的设备。<br>
      可能是对端还没装、端口不一样，或者被防火墙挡了。可以先点「手动添加」直接填地址；未装 TS Safe 的设备也能作为监控设备加入。</p>
    <p class="muted scan-note">没扫到你的电脑？先确认它和这台设备在<b>同一个网段</b>；Windows 默认防火墙会挡掉探测，
      放行「文件和打印机共享（回显请求 - ICMPv4-In）」后再扫一次。也可以直接在「额外网段」里填它的地址，例如 <code>192.168.8.100/32</code>。</p>`;
    return;
  }
  let html = `<div class="scan-sum">扫了 ${data.scanned} 个地址，找到 ${found.length} 台（用时 ${data.seconds}s）。给每台起个好认的名字，之后随时能改：</div>`;
  for (const f of found) {
    const kindTxt = SCAN_KIND_LABEL[f.kind] || "联网";
    const netTxt = f.iface && f.iface !== "手动填写" ? `${kindTxt} · ${escapeHtml(f.iface)}` : kindTxt;
    const brandTxt = f.brand_label || f.brand || "TS Safe";
    const defName = `${brandTxt} ${f.ip}`;
    const kind = (f.kind === "vpn") ? "chip" : topoKindFromBrand(f.brand);
    const isSelf = !!f.is_self;
    const isAdded = !!f.added;                       // 已经加过：不再默认勾选，避免加出重复设备
    const skip = isSelf || isAdded;
    html += `<div class="scan-item${isSelf ? " self" : ""}${isAdded ? " added" : ""}">
      <label class="si-pick"><input type="checkbox"${skip ? "" : " checked"}
        data-ip="${escapeHtml(f.ip)}" data-port="${f.port}" data-brand="${escapeHtml(f.brand || "generic_linux")}" data-kind="${escapeHtml(f.kind || "")}" data-nassafe="${f.nassafe ? 1 : 0}"></label>
      <div class="si-ico">${topoDeviceGlyph(kind)}</div>
      <div class="si-main">
        <input class="text-input si-name" value="${escapeHtml(defName)}" maxlength="24">
        <div class="si-meta">${escapeHtml(f.ip)}:${f.port} · ${escapeHtml(brandTxt)} · ${escapeHtml(netTxt)}${f.need_auth ? " · 需要账号" : ""}${isSelf ? " · 就是这台（已在控制台里，不用再加）" : ""}${isAdded ? ` · <span class="si-added">已经在设备列表里${f.added_name ? `（${escapeHtml(f.added_name)}）` : ""}，别再加了</span>` : ""}</div>
      </div>
    </div>`;
  }
  if (data.truncated) html += `<p class="muted scan-note">网段太大，只扫了一部分；可以在「额外网段」里填更小的网段（如 192.168.8.0/24）缩小范围。</p>`;
  const others = data.others || [];
  if (others.length) {
    // 只有「品牌可识别且不是未知类型」才算 AI/规则真的认出来了；否则老实标「仅在线·未识别」
    const recognized = (o) => (o.brand_label || "").trim() && o.device_type !== "unknown";
    const idCount = others.filter(recognized).length;
    // 未纳管设备较多（>5 台）时默认折叠，避免满屏「未知设备」刷屏
    const collapse = others.length > 5;
    html += `<details class="scan-others"${collapse ? "" : " open"}>
      <summary>网络里还发现 ${others.length} 台设备（未纳管，${idCount} 台已识别，点击${collapse ? "展开" : "收起"}）</summary>
      <div class="scan-sum muted" style="font-weight:400">路由器、交换机、没装 TS Safe 的电脑等，点「加入监控」一键登记，之后在设备列表里显示在线状态（仅监控，不能远程清理/迁移）；如果其中某台已运行 TS Safe 但用了其它端口，点「完整登记」填地址纳入管控。</div>`;
    for (const o of others) {
      const portTxt = (o.open_ports || []).slice(0, 6).join(",") || "无开放端口";
      const isRec = recognized(o);
      const tag = isRec
        ? (o.by === "ai" ? `<span class="ai-tag">AI 判断</span>` : `<span class="ai-tag rule">规则判断</span>`)
        : `<span class="ai-tag" style="opacity:.6;background:#eee;color:#888">仅在线·未识别</span>`;
      const addBtn = `<button class="btn ghost sm si-add" data-mon-ip="${escapeHtml(o.ip)}" data-mon-name="${escapeHtml(o.suggest_name || o.brand_label || o.ip)}" data-mon-type="${escapeHtml(o.device_type || "")}" data-mon-brand="${escapeHtml(o.brand_label || "")}" title="作为仅监控设备登记：只显示在线状态，不需要对方装 TS Safe">➕ 加入监控</button>
        <button class="btn ghost sm si-add" data-manual-ip="${escapeHtml(o.ip)}" data-manual-name="${escapeHtml(o.suggest_name || o.brand_label || "")}" data-manual-type="${escapeHtml(o.device_type || "")}" data-manual-brand="${escapeHtml(o.brand_label || "")}" title="如果这台设备其实装了 TS Safe（端口不是 8848），用这个填地址完整登记">完整登记</button>`;
      html += `<div class="scan-item static">
        <div class="si-ico">${topoDeviceGlyph(o.device_type === "pc" ? (o.brand_label || "").includes("Mac") ? "laptop" : "pc" : o.device_type === "camera" ? "camera" : o.device_type === "router" ? "router" : o.device_type === "server" ? "server" : o.device_type === "nas" || o.device_type === "nas_safe" ? "nas" : "chip")}</div>
        <div class="si-main">
          <div class="si-name2">${escapeHtml(o.suggest_name || o.brand_label || o.ip)} ${tag}</div>
          <div class="si-meta">${escapeHtml(o.ip)} · ${escapeHtml(o.brand_label || "未知设备")} · 端口 ${escapeHtml(portTxt)}${o.hostname ? " · " + escapeHtml(o.hostname) : ""}${o.alive_only ? " · 在线（没开可识别的端口）" : ""}${o.added ? ` · <span class="si-added">已在设备列表里</span>` : ""}</div>
        </div>
        ${addBtn}
      </div>`;
    }
    html += `</details>`;
  }
  if (data.ai_note) html += `<p class="muted scan-note">${escapeHtml(data.ai_note)}</p>`;
  box.innerHTML = html;
  // 未纳管设备按钮：
  // 1)「➕ 加入监控」一键登记为仅监控设备（不需要对方装 TS Safe，ping 判在线）
  // 2)「完整登记」打开手动添加弹窗（对方其实装了 TS Safe 但端口不同）
  const guessBrand = (dtype, blabel) => {
    const b = (blabel || "").toLowerCase();
    if (dtype === "pc" && b.includes("mac")) return "macos";
    if (dtype === "pc" && b.includes("windows")) return "windows";
    if (dtype === "nas" && b.includes("qnap")) return "qnap";
    if (dtype === "nas" && b.includes("群晖")) return "synology";
    if (dtype === "nas" && b.includes("绿联")) return "ugreen";
    return "generic_linux";
  };
  box.querySelectorAll(".si-add[data-mon-ip]").forEach((b) => {
    b.onclick = async (e) => {
      e.stopPropagation();
      const ip = b.dataset.monIp;
      const payload = {
        name: b.dataset.monName || ip,
        host: ip,
        port: 0,
        group: "联网设备",
        brand: guessBrand(b.dataset.monType, b.dataset.monBrand),
        brand_label: b.dataset.monBrand || "",
        monitor_only: true,
      };
      b.disabled = true;
      try {
        await api("/api/devices/add", { method: "POST", body: JSON.stringify(payload) });
        toast(`已把 ${ip} 加入监控（仅在线状态）`, "ok");
        loadConsole(true);
      } catch (err) {
        toast("加入监控失败：" + (err.message || ""), "err");
        b.disabled = false;
      }
    };
  });
  box.querySelectorAll(".si-add[data-manual-ip]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      const dtype = b.dataset.manualType || "";
      const blabel = (b.dataset.manualBrand || "").toLowerCase();
      let brand = "generic_linux";
      if (dtype === "pc" && blabel.includes("mac")) brand = "macos";
      else if (dtype === "pc" && blabel.includes("windows")) brand = "windows";
      else if (dtype === "nas" && blabel.includes("qnap")) brand = "qnap";
      else if (dtype === "nas" && blabel.includes("群晖")) brand = "synology";
      else if (dtype === "nas" && blabel.includes("绿联")) brand = "ugreen";
      openManualAdd({ host: b.dataset.manualIp, name: b.dataset.manualName || "", port: 8848, brand });
    };
  });
  const foot = $("modalFoot");
  if (foot) {
    foot.innerHTML = `<button class="btn ghost" data-act="close">关闭</button>
      <button class="btn ghost" data-act="scan">重新扫描</button>
      <button class="btn primary" data-act="addsel">＋ 添加勾选的设备</button>`;
  }
  modalActions.addsel = addSelectedDevices;
}

async function addSelectedDevices() {
  const rows = [...document.querySelectorAll("#scanResult .scan-item")];
  const picked = rows.filter((r) => { const c = r.querySelector("input[type=checkbox]"); return c && c.checked; });
  if (!picked.length) { toast("先勾选要添加的设备", "warn"); return; }
  const group = (($(("scanGroup") || {}).value) || "联网设备").trim();
  const wantAgent = !!($(("scanAgent") || {}).checked);
  let ok = 0, fail = 0, pending = 0;
  for (const r of picked) {
    const cb = r.querySelector("input[type=checkbox]");
    const nm = r.querySelector(".si-name");
    const payload = {
      name: ((nm && nm.value) || "").trim() || cb.dataset.ip,
      host: cb.dataset.ip,
      port: Number(cb.dataset.port) || 8848,
      brand: cb.dataset.brand || "generic_linux",
      group,
      net_kind: cb.dataset.kind || "",
      install_agent: wantAgent,
      // 扫描探测到这台本身就是 TS Safe 服务端（不是只装了轻量代理）→ 标记为完整服务端
      full_server: cb.dataset.nassafe === "1",
    };
    try { await api("/api/devices/add", { method: "POST", body: JSON.stringify(payload) }); ok++; if (wantAgent) pending++; }
    catch (e) { fail++; }
  }
  toast(fail ? `已添加 ${ok} 台，${fail} 台失败` : `已添加 ${ok} 台设备`, fail ? "warn" : "ok");
  closeModal();
  loadConsole(true);
  if (pending) setTimeout(() => toast(`已登记 ${pending} 台待装轻量代理：点设备卡片里的「安装轻量代理」拿安装命令`, "info"), 800);
}

function openManualAdd(defaults = {}) {
  const defName = defaults.name || "";
  const defHost = defaults.host || "";
  const defPort = String(defaults.port || 8848);
  const defBrand = defaults.brand || "generic_linux";
  openModal(
    "✍ 手动添加设备",
    `<div class="scan-grid">
      <label class="scan-row"><span>名称</span><input id="maName" class="text-input" value="${escapeHtml(defName)}" placeholder="如：办公室群晖"></label>
      <label class="scan-row"><span>地址</span><input id="maHost" class="text-input" value="${escapeHtml(defHost)}" placeholder="IP 或域名，不含 http"></label>
      <label class="scan-row"><span>端口</span><input id="maPort" class="text-input" value="${escapeHtml(defPort)}" inputmode="numeric" placeholder="仅监控可留空"></label>
      <label class="scan-row"><span>分组</span><input id="maGroup" class="text-input" value="联网设备" placeholder="家里 / 公司 / 机房"></label>
      <label class="scan-row wide"><span>品牌</span><select id="maBrand" class="text-input">
        <option value="generic_linux">自动 / 通用 Linux</option>
        <option value="qnap">威联通 QNAP</option>
        <option value="synology">群晖 Synology</option>
        <option value="ugreen">绿联 UGREEN</option>
        <option value="feiniu">飞牛 FnOS</option>
        <option value="truenas">TrueNAS</option>
        <option value="omv">OpenMediaVault</option>
        <option value="unraid">UnRAID</option>
        <option value="windows">Windows 电脑</option>
        <option value="macos">macOS 电脑</option>
        <option value="generic_linux">路由器 / 交换机 / 其它</option>
      </select></label>
    </div>
    <div class="scan-opts" style="margin-top:8px">
      <label title="适合路由器、交换机、没装 TS Safe 的电脑：只登记地址并探测在线状态，不出现在远程清理/迁移等功能里"><input type="checkbox" id="maMonOnly"> 📡 仅监控（这台设备没装 TS Safe，端口可留空）</label>
      <label style="margin-left:16px" title="装好可做快照防勒索、换机迁移；之后也能在中控补装"><input type="checkbox" id="maAgent"> 📡 添加后给这台设备安装轻量代理</label>
    </div>
    <p class="muted" style="margin:10px 0 0">异地组网的设备（Tailscale / WireGuard 等）填它的组网 IP 就行，和填局域网地址一样。</p>`,
    `<button class="btn ghost" data-act="close">取消</button>
     <button class="btn primary" data-act="save">添加</button>`,
    {
      save: async () => {
        const g = (id) => $(id) || {};
        const host = (g("maHost").value || "").trim();
        if (!host) { toast("请填写设备地址", "warn"); return; }
        const monOnly = !!(g("maMonOnly") && g("maMonOnly").checked);
        const portRaw = (g("maPort").value || "").trim();
        const port = Number(portRaw || 0) || 0;
        if (!port && !monOnly) { toast("请填写端口（勾选「仅监控」可留空）", "warn"); return; }
        const payload = {
          name: (g("maName").value || "").trim() || host,
          host,
          port,
          group: (g("maGroup").value || "联网设备").trim(),
          brand: g("maBrand").value || "generic_linux",
          monitor_only: monOnly,
          install_agent: monOnly ? false : !!g("maAgent").checked,
        };
        try {
          await api("/api/devices/add", { method: "POST", body: JSON.stringify(payload) });
          toast("已添加设备", "ok");
          closeModal();
          loadConsole(true);
        } catch (e) { toast("添加失败：" + e.message, "err"); }
      },
    }
  );
  const s = $("maBrand");
  if (s) s.value = defBrand;
}

/* 给任意设备（含本机）改名 —— 改完控制台和总控动画里都用新名字 */
function openRenameModal(id) {
  const dev = findDev(_consoleData || {}, id);
  if (!dev) { toast("没找到这台设备", "err"); return; }
  const cur = deviceDisplayName(dev);
  const isLocal = dev.type === "local";
  openModal(
    "✎ 给设备改名",
    `<p class="muted" style="margin:0 0 10px">改完在设备列表和总控动画里都显示这个名字，一眼就能认出来。</p>
     <label class="scan-row wide"><span>名称</span><input id="rnName" class="text-input" maxlength="24"
        value="${escapeHtml(isLocal && !dev.custom_name ? "" : (dev.name || ""))}" placeholder="${escapeHtml(cur)}"></label>
     <p class="muted" style="margin:10px 0 0">${isLocal
        ? "这台是本机（当前这台设备）。"
        : "地址：" + escapeHtml(dev.host || "") + ":" + (dev.port || 0)}</p>`,
    `<button class="btn ghost" data-act="close">取消</button>
     <button class="btn primary" data-act="save">保存</button>`,
    {
      save: async () => {
        const v = (($(("rnName") || {}).value) || "").trim();
        if (!v) { toast("名字不能为空", "warn"); return; }
        try {
          await api("/api/devices/rename", { method: "POST", body: JSON.stringify({ id, name: v }) });
          toast("已改名", "ok");
          closeModal();
          loadConsole(true);
        } catch (e) { toast("改名失败：" + e.message, "err"); }
      },
    }
  );
}

// ---- 换机迁移 ----
let _migBundle = null;

/* 迁移目标：把控制台里登记的设备列出来，选了就自动带出品牌 */
async function loadMigrateTargets() {
  const sel = $("migTargetDevice");
  if (!sel) return;
  try {
    const data = await api("/api/devices", {}, 15000);
    const devs = data.devices || [];
    sel.innerHTML = '<option value="">— 请选择目标设备 —</option>' +
      devs.map((d) => `<option value="${escapeHtml(d.brand || "")}" data-id="${escapeHtml(d.id)}" data-name="${escapeHtml(deviceDisplayName(d))}">${escapeHtml(deviceDisplayName(d))}${d.type === "local" ? "（这台）" : ""}</option>`).join("");
  } catch (e) { /* 拉不到就保留空列表，不影响导出 */ }
}

function onPickMigTarget() {
  const sel = $("migTargetDevice");
  if (!sel) return;
  const brand = (sel.value || "").trim();
  if (!brand) return;
  const bs = $("migTargetBrand");
  if (bs && [...bs.options].some((o) => o.value === brand)) bs.value = brand;
}

async function exportConfig() {
  try {
    const data = await api("/api/migrate/export", {}, 20000);
    const blob = new Blob([JSON.stringify(data.bundle, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    a.href = URL.createObjectURL(blob);
    a.download = `nassafe-migrate-${stamp}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("配置包已导出", "ok");
  } catch (e) { toast("导出失败：" + e.message, "err"); }
}

function onPickMigFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      _migBundle = JSON.parse(reader.result);
      $("migPathMap").hidden = false;
      $("migPreview").hidden = true;
      toast("已读取迁移包（来源：" + escapeHtml(_migBundle.source_brand || "?") + "）", "ok");
    } catch (e) { toast("迁移包解析失败：" + e.message, "err"); }
  };
  reader.readAsText(file);
}

async function previewMig() {
  if (!_migBundle) { toast("请先选择迁移包", "err"); return; }
  const oldR = ($("migOldRoot").value || "").trim();
  const newR = ($("migNewRoot").value || "").trim();
  const path_map = (oldR && newR) ? { [oldR]: newR } : {};
  const target_brand = ($("migTargetBrand").value || "").trim() || null;
  try {
    const data = await api("/api/migrate/preview", { method: "POST", body: JSON.stringify({ bundle: _migBundle, path_map, target_brand }) });
    const r = data.report || {};
    const applied = (r.applied || []).map((x) => `✔ ${escapeHtml(x.label)}`).join("<br>");
    const skipped = (r.skipped || []).map((x) => `⚠ ${escapeHtml(x.label)}：${escapeHtml(x.reason || "")}`).join("<br>");
    const remapped = (r.remapped || []).length ? `<br>🔁 路径已改写：${escapeHtml((r.remapped || []).join("、"))}` : "";
    const box = $("migPreview");
    box.hidden = false;
    box.innerHTML = `<div class="mig-preview-title">目标设备：${escapeHtml(r.target_brand_label || r.target_brand || "?")}</div>
      <div>${applied || "无"}</div>${skipped ? `<div>${skipped}</div>` : ""}${remapped}
      <div class="muted" style="margin-top:6px">以上为预览，不会真正写入。确认无误点「应用导入」。</div>`;
  } catch (e) { toast("预览失败：" + e.message, "err"); }
}

async function applyMig() {
  if (!_migBundle) { toast("请先选择迁移包", "err"); return; }
  if (!confirm("导入会覆盖本机当前的通知、快照、日报、AI 等配置，确定继续？")) return;
  const oldR = ($("migOldRoot").value || "").trim();
  const newR = ($("migNewRoot").value || "").trim();
  const path_map = (oldR && newR) ? { [oldR]: newR } : {};
  const target_brand = ($("migTargetBrand").value || "").trim() || null;
  try {
    const data = await api("/api/migrate/import", { method: "POST", body: JSON.stringify({ bundle: _migBundle, path_map, target_brand, confirm: true }) });
    const r = data.report || {};
    toast(`导入完成：已应用 ${((r.applied || []).length)} 项配置`, "ok");
    $("migPreview").hidden = true;
  } catch (e) { toast("导入失败：" + e.message, "err"); }
}



// 首屏把当前页写进地址栏（replaceState，不额外压一条历史），
// 这样第一次按「返回」是回上一页而不是退出站点。
function _syncInitialRoute() {
  try {
    // 首次进入时若地址栏带了 #/view（例如桌面小助手点气泡后直接打开总控台告警页），
    // 优先按地址栏走 —— 否则会被 localStorage 里的旧视图覆盖掉，深链就白给了。
    const r0 = _routeFromHash();
    if (r0 && VIEWS.includes(r0.view)) {
      _applyRoute(r0);
      return;
    }
    const v = localStorage.getItem("nassafe_view") || "home";
    const t = v === "settings" ? (localStorage.getItem("nassafe_settings_tab") || "notify") : "";
    history.replaceState({ view: v, tab: t }, "", "#/" + v + (t ? "/" + t : ""));
  } catch (e) { /* 忽略 */ }
}

function applyView() {
  const view = localStorage.getItem("nassafe_view") || "home";
  if (!VIEWS.includes(view)) return;
  document.querySelectorAll("#mainTabs .tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.view === view);
  });
  document.querySelectorAll("main [data-view]").forEach((sec) => {
    sec.hidden = sec.dataset.view !== view;
  });
}

/* ---------- 浏览器前进/后退 ----------
   之前切页只改 DOM、不动 history，于是「返回」直接退出整个站点（连按两下就没了）。
   现在把「视图 / 设置子菜单」写进地址栏 hash，返回键就能一页一页往回退。 */
function _routeFromHash() {
  const h = String(location.hash || "").replace(/^#\/?/, "");
  const p = h.split("/").filter(Boolean);
  return p.length ? { view: p[0], tab: p[1] || "" } : null;
}
function _pushRoute(view, tab) {
  const want = "#/" + view + (tab ? "/" + tab : "");
  if (location.hash === want) return;          // 同一目标不重复压栈
  try { history.pushState({ view, tab }, "", want); } catch (e) { /* 忽略 */ }
}
function _applyRoute(r) {
  if (!r || !VIEWS.includes(r.view)) return;
  if (r.view === "settings") {
    localStorage.setItem("nassafe_view", "settings");
    applyView();
    showSettingsTab(r.tab || "notify");
  } else {
    showView(r.view);  // 此时 hash 已是目标值，showView 里的 push 会被去重跳过
  }
}
window.addEventListener("popstate", () => { _applyRoute(_routeFromHash()); });

function showView(name) {
  if (!VIEWS.includes(name)) name = "home";
  localStorage.setItem("nassafe_view", name);
  applyView();
  // 离开告警页时停掉自动刷新，避免多余请求
  if (name !== "alerts" && alertsRefreshTimer) {
    clearInterval(alertsRefreshTimer);
    alertsRefreshTimer = null;
  }
  // 设置页的子菜单由 showSettingsTab 压栈（能记住具体菜单），这里不重复压
  if (name !== "settings") _pushRoute(name, "");
  onEnterView(name);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// 进入视图时恢复该页数据：showView 与启动恢复（强刷后 boot）共用，
// 否则强刷后时间轴/重复文件/垃圾页只切了显示、不拉数据，页面一片空白。
function onEnterView(name) {
  if (name === "console") {
    loadConsole();
  } else if (name === "snapshots") {
    loadSnapDevices();  // 顶部设备条：本机 + 联机设备
    if (state.snapDevice && state.snapDevice !== "local") { loadRemoteSnapshots(); return; }
    if (!state.activeVolume) autoSelectVolume(); // 没选过卷：自动选第一个
    else if (state.tlLoadedVol !== (state.activeVolume.mountpoint ?? String(state.activeVolume.id)))
      loadSnapshots(); // 卷已恢复但时间轴还没拉过：补拉（幂等，重复调用不会双载）
  } else if (name === "home") {
    loadMetrics();
  } else if (name === "monitor") {
    loadDeviceOverview();
  } else if (name === "alerts") {
    loadAlertsPage();
  } else if (name === "settings") {
    let tab = localStorage.getItem("nassafe_settings_tab") || "notify";
    showSettingsTab(tab);
  }
}

function showSettingsTab(tab) {
  localStorage.setItem("nassafe_settings_tab", tab);
  _pushRoute("settings", tab);
  if (tab === "account") initAccountPane();
  if (tab === "kb") initKbPane();
  if (tab === "photo") initPhotoPane();
  if (tab === "dups") { refreshDupStatus(); loadDupReport(); refreshDeviceOptions(); }
  else if (tab === "junk") { refreshJunkStatus(); loadJunkReport(); refreshDeviceOptions(); }
  else if (tab === "migrate") { loadMigrateTargets(); }
  else if (tab === "manage") { if (!_enteringFromDevice) _manageFocusId = ""; renderManage(); }
  document.querySelectorAll(".settings-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  document.querySelectorAll(".settings-pane").forEach((pane) => {
    pane.classList.toggle("active", pane.dataset.pane === tab);
  });
}

// 账号安全面板：本人改密码 / 改邮箱（后端按当前会话身份操作）
function initAccountPane() {
  const u = (window.currentUser && window.currentUser.username) || "";
  const eu = $("accUser"); if (eu) eu.textContent = u || "(未知)";
  const pwBtn = $("accChangePwBtn");
  if (pwBtn && !pwBtn.dataset.bound) {
    pwBtn.dataset.bound = "1";
    pwBtn.onclick = async () => {
      const oldPw = $("accOldPw").value, n1 = $("accNewPw").value, n2 = $("accNewPw2").value;
      const msg = $("accPwMsg");
      msg.style.display = "block"; msg.className = "notice";
      if (!oldPw || !n1) { msg.style.color = "#c62828"; msg.textContent = "请填写原密码和新密码"; return; }
      if (n1.length < 6) { msg.style.color = "#c62828"; msg.textContent = "新密码至少 6 位"; return; }
      if (n1 !== n2) { msg.style.color = "#c62828"; msg.textContent = "两次新密码不一致"; return; }
      try {
        await api("/api/auth/change_password", { method: "POST", body: JSON.stringify({ old_password: oldPw, new_password: n1 }) });
        msg.style.color = "#2e7d32"; msg.textContent = "密码已修改，下次登录请用新密码";
        $("accOldPw").value = $("accNewPw").value = $("accNewPw2").value = "";
      } catch (e) { msg.style.color = "#c62828"; msg.textContent = (e && e.message) || "修改失败"; }
    };
  }
  const mailBtn = $("accSaveEmailBtn");
  if (mailBtn && !mailBtn.dataset.bound) {
    mailBtn.dataset.bound = "1";
    mailBtn.onclick = async () => {
      const email = ($("accEmail").value || "").trim();
      const msg = $("accEmailMsg");
      msg.style.display = "block"; msg.className = "notice";
      if (!email) { msg.style.color = "#c62828"; msg.textContent = "请填写邮箱"; return; }
      try {
        await api("/api/auth/set_email", { method: "POST", body: JSON.stringify({ email }) });
        msg.style.color = "#2e7d32"; msg.textContent = "邮箱已保存，找回密码时可用";
      } catch (e) { msg.style.color = "#c62828"; msg.textContent = (e && e.message) || "保存失败"; }
    };
  }
}

async function autoSelectVolume() {
  let vol = null;
  const saved = localStorage.getItem("nassafe_volume");
  if (saved) vol = state.volumes.find((v) => v.mountpoint === saved || String(v.id) === saved);
  if (!vol && state.volumes.length) vol = state.volumes[0];
  if (vol) await selectVolume(vol, true);
}

/* ------------------------- AI 体检 / 问 AI ------------------------- */

// 一键体检：后端聚合全机状态交 AI 出报告，弹窗展示
async function aiDiagnose() {
  const btn = $("aiDiagnoseBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>体检中`;
  openModal(
    "AI 健康体检",
    `<p class="muted"><span class="spinner"></span>正在汇总硬件、存储、快照保护和告警数据，交给 AI 分析…（约 10-30 秒）</p>`,
    `<button class="btn ghost" data-act="close">取消</button>`
  );
  try {
    const data = await api("/api/ai/diagnose", { method: "POST", body: "{}" });
    openModal(
      "🤖 AI 健康体检报告",
      `<div style="white-space:pre-wrap; line-height:1.8">${escapeHtml(data.text)}</div>`,
      `<button class="btn primary" data-act="close">知道了</button>`
    );
  } catch (e) {
    openModal(
      "AI 健康体检",
      `<p>体检失败：${escapeHtml(e.message)}</p>
       <p class="muted">如果提示 AI 未配置，请到「设置 → AI 配置」选择云端供应商填密钥，或用「🔍 自动搜索」一键接入本地 Ollama。</p>`,
      `<button class="btn primary" data-act="close">去设置</button>`,
      { goSettings: () => { closeModal(); showView("settings"); } }
    );
  }
  btn.disabled = false;
  btn.textContent = "🤖 AI 体检";
}

// 问 AI / AI 管家：多轮对话——可连续追问 / 随时补充信息，AI 记住本次会话上下文
let aiChatHistory = []; // [{role:"user"|"assistant", content}]
let aiChatMode = "ask"; // "ask"=纯问答 / "butler"=工具调用（建快照/列卷/查改动/搜文件/看健康）

function renderAiChat() {
  const log = $("aiChatLog");
  if (!log) return;
  if (!aiChatHistory.length) {
    log.innerHTML = `<div class="muted" style="text-align:center;padding:4px 10px 18px;margin-top:0">💬 用大白话问 NAS 相关问题<br><span style="font-size:12px">支持连续追问、随时补充信息，AI 记得本次对话内容</span></div>`;
    return;
  }
  log.innerHTML = aiChatHistory.map((m) => {
    const mine = m.role === "user";
    const bg = mine ? "var(--z-storage)" : "var(--surface-2)";
    const fg = mine ? "#fff" : "var(--text)";
    const corner = mine ? "border-bottom-right-radius:3px" : "border-bottom-left-radius:3px";
    return `<div style="display:flex;justify-content:${mine ? "flex-end" : "flex-start"};margin:6px 0">
      <div style="max-width:86%;white-space:pre-wrap;line-height:1.7;font-size:13px;padding:8px 12px;border-radius:12px;${corner};background:${bg};color:${fg}">${escapeHtml(m.content)}</div>
    </div>`;
  }).join("");
  log.scrollTop = log.scrollHeight;
}

function setAiChatMode(mode) {
  aiChatMode = mode === "butler" ? "butler" : "ask";
  const askBtn = $("aiModeAsk");
  const butlerBtn = $("aiModeButler");
  const inp = $("aiChatInput");
  const hint = $("aiModeHint");
  const kbRow = $("aiKbRow");
  const isFree = window.__tssafeEdition === "free";
  if (askBtn) askBtn.classList.toggle("primary", aiChatMode === "ask");
  if (askBtn) askBtn.classList.toggle("ghost", aiChatMode !== "ask");
  if (butlerBtn) butlerBtn.classList.toggle("primary", aiChatMode === "butler");
  if (butlerBtn) butlerBtn.classList.toggle("ghost", aiChatMode !== "butler");
  if (inp) {
    inp.placeholder = aiChatMode === "butler"
      ? "直接下指令或提问，例如：帮我建一张快照 / 看看有没有异常改动 / 搜一下简历 / 硬盘健康吗？"
      : "输入问题，回车发送（Shift+回车换行）。AI 答完可继续追问或补充信息。";
  }
  if (hint) {
    hint.textContent = aiChatMode === "butler"
      ? "管家模式：AI 可调用本地工具执行建快照、列存储单元、查异常改动、搜文件、看健康。"
      : (isFree ? "问答模式：AI 根据当前系统状态和告警回答。" : "问答模式：AI 根据当前系统状态和告警回答，可勾选下方「结合知识库」。");
  }
  if (kbRow) {
    kbRow.style.display = (aiChatMode === "butler" || isFree) ? "none" : "";
  }
}

async function aiAsk(mode = "ask") {
  aiChatHistory = []; // 每次打开开新会话；会话内多轮共享上下文
  aiChatMode = mode === "butler" ? "butler" : "ask";
  const title = aiChatMode === "butler" ? "🤖 AI 管家" : "🤖 问 AI";
  const edition = await ensureEditionCache().catch(() => "free");
  const isFree = edition === "free";
  openModal(
    title,
    `<div style="display:flex;gap:8px;margin-bottom:10px;align-items:center">
       <button id="aiModeAsk" class="btn ${aiChatMode === "ask" ? "primary" : "ghost"}" onclick="setAiChatMode('ask')">问答模式</button>
       <button id="aiModeButler" class="btn ${aiChatMode === "butler" ? "primary" : "ghost"}" onclick="setAiChatMode('butler')">管家模式</button>
       <span id="aiModeHint" class="muted" style="font-size:12px;flex:1">${aiChatMode === "butler" ? "管家模式：AI 可调用本地工具执行建快照、列存储单元、查异常改动、搜文件、看健康。" : (isFree ? "问答模式：AI 根据当前系统状态和告警回答。" : "问答模式：AI 根据当前系统状态和告警回答，可勾选下方「结合知识库」。")}</span>
     </div>
     <div id="aiChatLog" style="flex:1 1 auto;min-height:0;overflow-y:auto;padding:8px 2px 10px;margin-bottom:10px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;justify-content:flex-start"></div>
     <label id="aiKbRow" class="chk-inline" style="display:${isFree || aiChatMode === "butler" ? "none" : ""};margin:8px 0 0"><input type="checkbox" id="aiKbChk"> 结合知识库回答（文档在「设置 → 知识库」管理）</label>
     <textarea id="aiChatInput" class="text-input" rows="3" style="display:block;width:100%;box-sizing:border-box;resize:vertical;min-height:86px;line-height:1.6;flex:none"
       placeholder="${aiChatMode === "butler" ? "直接下指令或提问，例如：帮我建一张快照 / 看看有没有异常改动 / 搜一下简历 / 硬盘健康吗？" : "输入问题，回车发送（Shift+回车换行）。AI 答完可继续追问或补充信息。"}"></textarea>
     <p class="muted" style="margin:8px 0 0">回答基于当前系统状态与告警，仅供参考；关键操作请以人工判断为准。</p>`,
    `<button class="btn ghost" data-act="newchat">新话题</button>
     <button class="btn ghost" data-act="close">关闭</button>
     <button class="btn primary" data-act="send">发送</button>`,
    {
      newchat: () => { aiChatHistory = []; renderAiChat(); const i = $("aiChatInput"); if (i) i.focus(); },
      send: sendAiChat,
    },
    { stay: true }
  );
  // 聊天弹窗加宽加高（inline 只在此弹窗设置，closeModal 统一还原，不影响其它弹窗）
  const box = $("modalBox");
  if (box) { box.style.maxWidth = "880px"; box.style.width = "94vw"; box.style.maxHeight = "88vh"; box.style.height = "88vh"; }
  const mbody = $("modalBody");
  if (mbody) { mbody.style.display = "flex"; mbody.style.flexDirection = "column"; mbody.style.overflowY = "hidden"; }
  renderAiChat();
  const ta = $("aiChatInput");
  if (ta) {
    ta.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendAiChat(); }
    });
    setTimeout(() => ta.focus(), 50);
  }
}

async function sendAiChat() {
  const inp = $("aiChatInput");
  const btn = document.querySelector("#modalFoot button[data-act='send']");
  if (!inp) return;
  const q = (inp.value || "").trim();
  if (!q) { toast("请先输入内容", "warn"); return; }
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  inp.value = "";
  aiChatHistory.push({ role: "user", content: q });
  renderAiChat();
  const log = $("aiChatLog");
  if (log) {
    const think = document.createElement("div");
    think.innerHTML = `<div style="display:flex;justify-content:flex-start;margin:6px 0"><div class="muted" style="padding:8px 12px"><span class="spinner"></span> ${aiChatMode === "butler" ? "调用工具中…" : "思考中…"}</div></div>`;
    log.appendChild(think);
    log.scrollTop = log.scrollHeight;
  }
  try {
    const endpoint = aiChatMode === "butler" ? "/api/ai/butler" : "/api/ai/ask";
    // history 不含本条（本条作为 question 单传）；最多带最近 20 条防爆
    const kbChk = $("aiKbChk");
    const extra = (aiChatMode === "ask" && kbChk && kbChk.checked) ? { use_kb: true } : undefined;
    const data = await routeAI(q, endpoint, "", aiChatHistory.slice(0, -1).slice(-20), extra);
    let ans = "";
    if (data && typeof data === "object") {
      ans = data.text;
      if (typeof ans !== "string" && ans && typeof ans === "object") ans = ans.text || ans.content || JSON.stringify(ans);
    } else if (typeof data === "string") {
      ans = data;
    }
    ans = (typeof ans === "string" ? ans : String(ans || "")).trim();
    aiChatHistory.push({ role: "assistant", content: ans || "（AI 没有返回内容，请换个问法重试，或点「新话题」重开）" });
  } catch (e) {
    let msg = "❌ 回答失败：" + e.message;
    if (/次数已用完|升级家庭版/.test(e.message)) {
      msg += `\n<button class="btn primary" style="margin-top:8px" onclick="openUpgradeModal();document.querySelector('#modalFoot button[data-act=\\'close\\']')?.click();">升级家庭版，AI 不限量</button>`;
    } else {
      msg += "\n若提示 AI 未配置，请到「设置 → AI 配置」先启用。";
    }
    aiChatHistory.push({ role: "assistant", content: msg });
  } finally {
    renderAiChat();
    if (btn) btn.disabled = false;
    const i2 = $("aiChatInput");
    if (i2) i2.focus();
  }
}

/* ------------------------- 时间轴 ------------------------- */

async function selectVolume(vol, keepSnapshot) {
  state.activeVolume = vol;
  localStorage.setItem("nassafe_volume", vol.mountpoint ?? String(vol.id));
  $("tlTitle").textContent = `快照时间轴 — ${vol.name}`;
  $("tlSubtitle").textContent = `显示「${vol.name}」这一个存储卷的快照（其他卷的快照不在本时间轴内）`;
  const sel = $("tlVolumeSel");
  if (sel && sel.value !== (vol.mountpoint ?? String(vol.id))) sel.value = vol.mountpoint ?? String(vol.id);
  toggleVssCleanBtn(vol);
  $("browseBtn").disabled = true;
  // 默认把当前卷的挂载点填入勒索行为监控路径
  if ($("watchPaths").value.trim() === "") {
    $("watchPaths").value = vol.mountpoint;
  }

  document.querySelectorAll(".volume-card").forEach((c) => c.classList.remove("active"));
  await loadSnapshots();
  // 选中卷后跳到时间轴页
  showView("snapshots");
}

/* ---------- 快照页：设备切换（本机 + 联机设备都能看快照） ---------- */

let _snapDevList = [];   // 快照页设备条用的设备列表（含本机）

// 渲染顶部设备条：本机 + 所有已登记的联机设备
async function loadSnapDevices() {
  const bar = $("tlDeviceBar");
  if (!bar) return;
  let devs = _snapDevList;
  try {
    const data = await api("/api/devices", {}, 20000);
    _overviewData = data;
    devs = data.devices || [];
    _snapDevList = devs;
  } catch (e) { /* 读不到就沿用上次列表 */ }
  const local = devs.find((d) => d.type === "local");
  let html = `<button class="tl-dev${state.snapDevice === "local" ? " active" : ""}" data-sdev="local">🖥 ${escapeHtml((local && local.name) || "本机")}（本机）</button>`;
  for (const d of devs) {
    if (d.type === "local") continue;
    const off = d.status === "offline";
    html += `<button class="tl-dev${state.snapDevice === d.id ? " active" : ""}${off ? " off" : ""}" data-sdev="${escapeHtml(d.id)}" title="${off ? "这台设备当前离线" : "看这台设备的快照"}">${escapeHtml(deviceDisplayName(d))}${off ? " · 离线" : ""}</button>`;
  }
  bar.innerHTML = html;
  bar.querySelectorAll("[data-sdev]").forEach((b) => {
    b.onclick = () => selectSnapDevice(b.dataset.sdev);
  });
}

// 切换快照页当前查看的设备
async function selectSnapDevice(id) {
  state.snapDevice = id || "local";
  loadSnapDevices();                       // 重绘高亮（不等待）
  if (state.snapDevice === "local") {
    state.snapReadonly = false;
    state.snapVolumes = state.volumes || [];
    fillVolumeSel(state.snapVolumes);
    setSnapRemoteUI(false);
    if (!state.activeVolume) autoSelectVolume();
    else loadSnapshots();
    return;
  }
  state.snapReadonly = true;
  setSnapRemoteUI(true);
  const tl = $("timeline");
  tl.innerHTML = `<p class="muted"><span class="spinner"></span>正在读取这台设备的快照…</p>`;
  try {
    const data = await api(`/api/devices/snapshots?device=${encodeURIComponent(state.snapDevice)}`, {}, 25000);
    if (!data.ok) throw new Error(data.error || "读取失败");
    state.snapVolumes = data.volumes || [];
    fillVolumeSel(state.snapVolumes, data.volume);
    state.snapshots = data.snapshots || [];
    renderTimeline((data.device && data.device.name) || "这台设备", data.warning);
  } catch (e) {
    tl.innerHTML = `<p class="muted">没读到这台设备的快照：${escapeHtml(e.message || "")}<br>
      可能它关机了、地址变了，或者还没登记访问地址。可到「功能设置 → 联机设备管控」里检查这台设备。</p>`;
  }
}

// 联机设备模式：换卷时重新向本控制台代理拉取
async function loadRemoteSnapshots() {
  const tl = $("timeline");
  const volKey = ($("tlVolumeSel") || {}).value || "";
  tl.innerHTML = `<p class="muted"><span class="spinner"></span>读取快照列表…</p>`;
  try {
    const data = await api(`/api/devices/snapshots?device=${encodeURIComponent(state.snapDevice)}&volume=${encodeURIComponent(volKey)}`, {}, 25000);
    if (!data.ok) throw new Error(data.error || "读取失败");
    state.snapVolumes = data.volumes || [];
    state.snapshots = data.snapshots || [];
    const volName = ((data.volumes || []).find((v) => String(v.mountpoint ?? v.id) === String(data.volume)) || {}).name || data.volume || "该存储单元";
    renderTimeline(`${(data.device && data.device.name) || "这台设备"} · ${volName}`, data.warning);
  } catch (err) {
    tl.innerHTML = `<p class="muted">读取失败：${escapeHtml(err.message || "")}</p>`;
  }
}

// 填充卷下拉框（本机 / 联机设备共用）
function fillVolumeSel(vols, current) {
  const sel = $("tlVolumeSel");
  if (!sel) return;
  sel.innerHTML = (vols || [])
    .map((v) => `<option value="${escapeHtml(v.mountpoint ?? String(v.id))}">${escapeHtml(v.name)}</option>`)
    .join("");
  if (current) sel.value = current;
}

// 联机设备模式：拍快照/浏览文件都只在自己机器上做，这里禁用并说明
function setSnapRemoteUI(remote) {
  const sb = $("snapBtn");
  const bb = $("browseBtn");
  if (sb) {
    sb.disabled = !!remote;
    sb.title = remote ? "要给这台设备拍快照，请在它自己的控制台上操作" : "";
  }
  if (bb) bb.disabled = true;
}

// 时间轴渲染（本机 / 联机设备共用）
function renderTimeline(whoLabel, warning) {
  const tl = $("timeline");
  const n = (state.snapshots || []).length;
  if ($("tlSubtitle")) {
    $("tlSubtitle").textContent = `当前显示「${whoLabel}」的快照，共 ${n} 张 · 🔒 = 受 TS Safe 保护`;
  }
  if (!n) {
    tl.innerHTML = `<p class="muted">
      ${warning ? escapeHtml(warning) + "<br>" : ""}
      ${state.snapReadonly ? "这台设备的这块存储单元还没有快照。" : "该存储单元还没有快照。点击右上角「立即拍一张快照」开始保护。"}
    </p>`;
    return;
  }
  if (warning) {
    const w = document.createElement("p");
    w.className = "muted";
    w.textContent = warning;
    tl.innerHTML = "";
    tl.appendChild(w);
  } else {
    tl.innerHTML = "";
  }
  const sorted = [...state.snapshots].sort((a, b) => {
    const ta = a.created_at || a.name;
    const tb = b.created_at || b.name;
    return String(ta).localeCompare(String(tb));
  });
  sorted.forEach((snap, idx) => {
    const isLatest = idx === sorted.length - 1;
    const node = document.createElement("div");
    node.className = "tl-node" + (isLatest ? " latest" : "");
    node.innerHTML = `
      <div class="tl-label">${formatShort(snap.created_at || snap.name)}</div>
      <div class="tl-dot"></div>
      <div class="tl-size">${snap.protected ? '<span class="lock" title="受 TS Safe 保护">🔒</span>' : ""}</div>
    `;
    node.onclick = () => openSnapshotDetail(snap, state.snapReadonly);
    tl.appendChild(node);
  });
}

async function loadSnapshots() {
  // 联机设备模式：走本控制台代理，不碰本机的卷
  if (state.snapDevice && state.snapDevice !== "local") {
    await loadRemoteSnapshots();
    return;
  }
  const vol = state.activeVolume;
  if (!vol) return;

  const tl = $("timeline");
  tl.innerHTML = `<p class="muted"><span class="spinner"></span>读取快照列表…</p>`;

  try {
    const data = await api(`/api/snapshots?volume=${encodeURIComponent(vol.mountpoint)}`);
    state.snapshots = data.snapshots;
    state.tlLoadedVol = vol.mountpoint ?? String(vol.id); // 记录已加载的卷，防止启动恢复与 selectVolume 双重加载
    renderTimeline(vol.name);
  } catch (err) {
    tl.innerHTML = `<p class="muted">读取失败：${escapeHtml(err.message)}</p>`;
  }
}

/* ------------------------- 快照详情 ------------------------- */

function openSnapshotDetail(snap, remote) {
  const qnap = isQnapSnap(snap);
  const pathLabel = qnap
    ? (snap.mount_path || "NAS 内部快照")
    : (snap.path || "-");
  const body = `
    <div class="kv">
      <div class="kv-row"><span class="kv-k">快照名</span><span class="kv-v">${escapeHtml(snap.name)}</span></div>
      <div class="kv-row"><span class="kv-k">创建时间</span><span class="kv-v">${escapeHtml(snap.created_at || "未知")}</span></div>
      <div class="kv-row"><span class="kv-k">占用空间</span><span class="kv-v">${escapeHtml(snap.size_human || "计算中")}</span></div>
      <div class="kv-row"><span class="kv-k">只读保护</span><span class="kv-v">${snap.readonly ? "已启用（无法被修改）" : "未启用"}</span></div>
      <div class="kv-row"><span class="kv-k">${qnap ? "快照定位" : "实体路径"}</span><span class="kv-v">${escapeHtml(pathLabel)}</span></div>
      ${qnap ? `<div class="kv-row"><span class="kv-k">防勒索锁</span><span class="kv-v">${snap.vital ? "已永久锁定（不会被自动清理）" : "未锁定"}</span></div>` : ""}
    </div>
    ${remote ? `<div class="notice">
        这是<b>联机设备</b>上的快照，这里<b>只能查看</b>：要取文件、要回滚，都请到那台设备自己的控制台上操作
        （本控制台不跨机改动别人的数据，避免误操作）。
      </div>` : `<div class="notice">
        这份快照是只读的，勒索软件无法修改其中的数据。恢复数据有两种方式，按情况选：<br>
        ✅ <b>浏览并取回文件（推荐）</b> —— 非破坏性：从快照里挑文件/目录拷回来，<b>不影响当前任何数据</b>。适合「误删 / 误改了部分文件」。<br>
        ⚠️ <b>整卷回滚（谨慎）</b> —— 破坏性：整卷回退到快照那一刻，<b>之后新增 / 修改的内容全部丢失</b>，且该快照之后的快照会被一并删除。仅在勒索攻击等大面积中招时才用。
      </div>`}
  `;

  // 联机设备快照一律只读预览：浏览/回滚都只能在那台设备自己的控制台上做
  const canBrowse = !remote && (qnap || (snap.path && snap.path.startsWith("/")));
  // 仅 TS Safe 托管的 QNAP 快照允许整卷回滚（防误操作系统/无关快照）
  const canRevert = !remote && qnap && /^(auto-|nassafe_|snap-)/.test(snap.name || "");
  const foot = `
    <button class="btn ghost" data-act="close">关闭</button>
    ${canRevert ? `<button class="btn danger" data-act="revert">⚠ 整卷回滚到此快照</button>` : ""}
    <button class="btn primary" data-act="browse" ${canBrowse ? "" : "disabled"}>
      ${remote ? "联机设备快照仅可查看" : canBrowse ? "浏览并取回文件" : "该系统不支持直接浏览"}
    </button>
  `;

  openModal(`快照详情`, body, foot, {
    browse: () => {
      closeModal();
      // QNAP 快照无本地实体路径，从快照根（subpath 为空）开始浏览
      openBrowser(snap, qnap ? "" : snap.path);
    },
    revert: () => {
      closeModal();
      confirmRevert(snap);
    },
  });
}

/* ------------------------- 整卷回滚 ------------------------- */

async function confirmRevert(snap) {
  const qnap = isQnapSnap(snap);
  const vid = (state.activeVolume && state.activeVolume.volume_id) || snap.volume_id;
  const sid = snap.snapshot_id;
  const when = formatWhen(snap.created_at || snap.name);
  const volName = (state.activeVolume && state.activeVolume.name) || "该存储卷";
  const body = `
    <div class="notice danger">
      <b>⚠ 这是破坏性操作，请务必确认！</b><br>
      整卷回滚会把整卷「${escapeHtml(volName)}」的数据<strong>整体回退到「${escapeHtml(when)}」这一刻的状态</strong>。<br>
      回滚之后、此快照<strong>之后新增或修改过的所有文件都会丢失</strong>，且无法直接撤销。<br>
      如果只是想找回个别文件，请改用「浏览并取回文件」，更安全。
    </div>
    <p class="muted">如确要认真回滚，请在下方输入 <code>回滚</code> 两个字，再点确认：</p>
    <input id="revertConfirmInput" class="text-input" placeholder="在此输入：回滚" />
  `;
  const foot = `
    <button class="btn ghost" data-act="cancel">取消</button>
    <button class="btn danger" data-act="dorevert" disabled>确认回滚</button>
  `;
  openModal("确认整卷回滚", body, foot, {
    cancel: () => closeModal(),
  });
  const input = $("revertConfirmInput");
  const btn = $("modalFoot").querySelector('[data-act="dorevert"]');
  input.oninput = () => { btn.disabled = input.value.trim() !== "回滚"; };
  btn.onclick = async () => {
    if (input.value.trim() !== "回滚") return;
    closeModal();
    await doRevert(vid, sid, when);
  };
}

async function doRevert(volumeId, snapshotId, when) {
  try {
    toast("正在提交整卷回滚，请稍候（卷可能短暂不可用）…");
    await api("/api/snapshot/revert", {
      method: "POST",
      body: JSON.stringify({ volume_id: volumeId, snapshot_id: snapshotId, confirm: true }),
    });
    toast(`已提交整卷回滚：回退到「${when}」。NAS 正在后台执行，稍后到「存储单元」查看状态。`, "ok");
  } catch (err) {
    toast("回滚失败：" + err.message, "err");
  }
}

/* ------------------------- 文件浏览 ------------------------- */

async function openBrowser(snap, path, pushStack = true) {
  const qnap = isQnapSnap(snap);
  state.browseSnapshot = snap;
  if (pushStack) state.browseStack.push(path);
  state.browsePath = path;

  const box = $("modalBox");
  if (box.dataset.mode !== "browse") {
    openModal("浏览快照中的文件", `<p class="muted"><span class="spinner"></span>读取目录…</p>`, "", {});
    box.dataset.mode = "browse";
  }
  $("modalBody").innerHTML = `<p class="muted"><span class="spinner"></span>读取目录…</p>`;

  try {
    // QNAP 后端：按 snapshot_id + volume_id + subpath 浏览（path 为相对子路径）
    // 本地 btrfs/zfs：按实体路径浏览
    let data;
    if (qnap) {
      const vid = (state.activeVolume && state.activeVolume.volume_id) || snap.volume_id;
      const sid = snap.snapshot_id;
      const sub = path || "";
      data = await api(
        `/api/browse?snapshot_id=${encodeURIComponent(sid)}` +
        `&volume_id=${encodeURIComponent(vid)}` +
        `&subpath=${encodeURIComponent(sub)}`
      );
    } else {
      data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    }

    // 面包屑 + 上级目录：QNAP 用累积的 subpath；本地用真实父目录
    const crumb = qnap
      ? (path ? "/" + path : "/ （快照根目录）")
      : path;
    let parentRow = "";
    if (qnap) {
      if (path) {
        const up = path.split("/").slice(0, -1).join("/");
        parentRow = `<div class="file-row" data-path="${escapeAttr(up)}" data-dir="true"><span class="file-icon dir"></span><span class="file-name">.. 返回上级</span><span class="file-size"></span></div>`;
      }
    } else if (data.parent && data.parent.includes(".nassafe")) {
      parentRow = `<div class="file-row" data-path="${escapeAttr(data.parent)}" data-dir="true"><span class="file-icon dir"></span><span class="file-name">.. 返回上级</span><span class="file-size"></span></div>`;
    }

    const rows = (data.entries || []).map((e) => `
      <div class="file-row" data-path="${escapeAttr(e.path)}" data-dir="${e.is_dir}">
        <span class="file-icon ${e.is_dir ? "dir" : ""}"></span>
        <span class="file-name">${escapeHtml(e.name)}</span>
        <span class="file-time">${escapeHtml(formatDT(e.mtime))}</span>
        <span class="file-size">${e.is_dir ? "" : escapeHtml(e.size_human || "—")}</span>
      </div>
    `).join("");

    $("modalBody").innerHTML = `
      <div class="crumb">${escapeHtml(crumb)}</div>
      <div id="fileList">
        ${parentRow}
        ${rows || `<p class="muted">这个目录是空的。</p>`}
      </div>
      <div class="notice">
        这里是快照的只读视图。点击文件名可以取回到「恢复目录」，不会覆盖你当前的数据。
      </div>
    `;

    $("fileList").onclick = (ev) => {
      const row = ev.target.closest(".file-row");
      if (!row) return;
      const p = row.dataset.path;
      if (row.dataset.dir === "true") {
        openBrowser(snap, p, true);
      } else {
        restoreFile(snap, p);
      }
    };
  } catch (err) {
    $("modalBody").innerHTML = `<p class="muted">读取失败：${escapeHtml(err.message)}</p>`;
  }
}

async function restoreFile(snap, fullPath) {
  const qnap = isQnapSnap(snap);
  let rel;
  if (qnap) {
    // QNAP 浏览返回的 entry.path 已是相对快照根的路径
    rel = (fullPath || "").replace(/^\/+/, "");
  } else {
    const base = (snap.path || "").replace(/\/+$/, "");
    rel = fullPath && fullPath.startsWith(base) ? fullPath.slice(base.length) : fullPath;
    rel = (rel || "").replace(/^\/+/, "");
  }

  if (!rel) {
    toast("无法确定要取回的文件", "err");
    return;
  }

  // 解析恢复目录（首次会让用户确认/修改，并记忆）
  const destination = await resolveRestoreDir(snap);
  if (!destination) return;  // 用户取消

  const body = qnap
    ? {
        snapshot_id: snap.snapshot_id,
        volume_id: (state.activeVolume && state.activeVolume.volume_id) || snap.volume_id,
        relative_file: rel,
        destination,
        confirm: true,
      }
    : {
        snapshot_path: snap.path,
        relative_file: rel,
        destination,
        confirm: true,
      };

  try {
    toast("正在恢复…");
    const data = await api("/api/snapshot/restore", {
      method: "POST",
      body: JSON.stringify(body),
    });
    toast(`已恢复到：${data.restored_to}`, "ok");
  } catch (err) {
    toast("恢复失败：" + err.message, "err");
  }
}

// 恢复目录：首次取回时让用户确认/修改，之后记忆到 localStorage，不再询问。
async function resolveRestoreDir(snap) {
  const qnap = isQnapSnap(snap);
  const fallback = qnap
    ? "/share/我的文件/nassafe_restored"
    : (state.activeVolume
        ? state.activeVolume.mountpoint.replace(/\/+$/, "") + "/_restored"
        : "/tmp/nassafe_restored");
  if (state.restoreDir) return state.restoreDir;
  return await askRestoreDestination(fallback);
}

function askRestoreDestination(suggest) {
  return new Promise((resolve) => {
    const body = `
      <p class="muted">取回的文件会复制到这里（不会覆盖你当前的数据）。
      该目录位于「运行 TS Safe 的这台机器」上：若本服务装在 NAS 本机，就是 NAS 共享目录；
      若装在其他电脑上远程管理 NAS，就是那台电脑上的目录。</p>
      <input id="destInput" class="text-input"
             value="${escapeAttr(suggest)}"
             style="width:100%;box-sizing:border-box;padding:9px 10px;margin-top:10px;
                    border:1px solid #e2e8f0;border-radius:8px;font-size:14px;">
    `;
    const foot = `
      <button class="btn ghost" data-act="cancel">取消</button>
      <button class="btn primary" data-act="ok">确定取回</button>
    `;
    openModal("选择恢复目录", body, foot, {
      ok: () => {
        const v = ($("destInput").value || "").trim();
        if (v) {
          state.restoreDir = v;
          localStorage.setItem("nassafe.restoreDir", v);
        }
        closeModal();
        resolve(v || null);
      },
      cancel: () => { closeModal(); resolve(null); },
    });
    // 点遮罩等同于取消，避免 Promise 泄漏
    $("modalMask").onclick = () => { closeModal(); resolve(null); };
  });
}

/* ------------------------- 创建快照 ------------------------- */

function ensureEditionCache() {
  if (window.__tssafeEditionPromise) return window.__tssafeEditionPromise;
  window.__tssafeEditionPromise = api("/api/license").then((d) => {
    window.__tssafeEdition = d.edition || "free";
    applyEditionUI();
    return window.__tssafeEdition;
  }).catch(() => "free");
  return window.__tssafeEditionPromise;
}

function applyEditionUI() {
  const ed = window.__tssafeEdition || "free";
  const photoBtn = document.querySelector('.settings-tab[data-tab="photo"]');
  if (photoBtn) photoBtn.style.display = ed === "business" ? "" : "none";
  const kbRow = $("aiKbRow");
  if (kbRow) {
    const isFree = ed === "free";
    const inButler = typeof aiChatMode !== "undefined" && aiChatMode === "butler";
    kbRow.style.display = (isFree || inButler) ? "none" : "";
  }
}

function toggleVssCleanBtn(vol) {
  const vb = $("vssCleanBtn");
  if (!vb) return;
  vb.style.display = (vol && vol.fs_type === "vss") ? "" : "none";
}

async function vssCleanupRun(confirm) {
  const btn = $("vssCleanBtn");
  try {
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>${confirm ? "清理中…" : "检查中…"}`; }
    const d = await api("/api/snapshots/vss_cleanup", { method: "POST", body: JSON.stringify({ confirm }) });
    if (!d || !d.ok) { toast((d && d.error) || "检查失败", "err"); return; }
    if (confirm) {
      toast(`已清理 ${d.deleted.length} 个孤儿影子副本`, "ok");
      await loadSnapshots();
      return;
    }
    if (!d.orphans.length) { toast("没有发现孤儿影子副本，很干净", "ok"); return; }
    if (window.confirm(`发现 ${d.orphans.length} 个系统里残留的孤儿影子副本（本产品已不管理它们，但还占着磁盘空间）。确定全部删除吗？`)) {
      vssCleanupRun(true);
    }
  } catch (e) {
    toast("清理失败：" + e.message + (/(管理员|admin)/i.test(e.message) ? "（需要以管理员身份运行本程序）" : ""), "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "清理孤儿影子副本"; }
  }
}

async function createSnapshot() {
  const vol = state.activeVolume;
  if (!vol) {
    toast("请先选择一个存储单元", "err");
    return;
  }

  const btn = $("snapBtn");
  btn.disabled = true;
  const original = btn.textContent;
  btn.innerHTML = `<span class="spinner"></span>正在创建…`;

  try {
    const data = await api("/api/snapshot/create", {
      method: "POST",
      body: JSON.stringify({ volume: vol.mountpoint, confirm: true }),
    });
    toast(data.message, "ok");
    await loadSnapshots();
    await loadVolumes();
  } catch (err) {
    toast("创建失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

/* ------------------------- 模态框 ------------------------- */

let modalActions = {};
let modalHasInput = false;
let modalSticky = false; // 粘性弹窗：AI 问答/解读等结果弹窗，切窗口回来点遮罩也不许关

function openModal(title, body, foot, actions, opts) {
  modalActions = actions || {};
  modalSticky = !!(opts && opts.stay);
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = body;
  $("modalFoot").innerHTML = foot || "";
  $("modalRoot").hidden = false;

  // 监听整个弹窗：按钮可能放在 body（如异常列表的「查看/修复方案/忽略」）
  $("modalRoot").onclick = (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "close") closeModal();
    else if (modalActions[act]) modalActions[act]();
  };

  if (!foot) $("modalFoot").innerHTML = `<button class="btn ghost" data-act="close">关闭</button>`;

  // 含可编辑输入框（textarea/input）的弹窗：点遮罩 / 按 Esc 都不关闭，
  // 避免误触把已输入的内容弄丢（如「问 AI」写了一大段突然没了）。
  // 这类弹窗必须点「取消 / ✕ / 关闭」才关。
  modalHasInput = !!$("modalBody").querySelector("textarea, input, [contenteditable='true']");
  $("modalMask").onclick = (modalHasInput || modalSticky) ? null : closeModal;
}

function closeModal() {
  modalSticky = false;
  const box = $("modalBox");
  if (box) { box.style.maxWidth = ""; box.style.width = ""; box.style.maxHeight = ""; box.style.height = ""; } // 还原「问AI」聊天弹窗的加宽
  const mbody2 = $("modalBody");
  if (mbody2) { mbody2.style.display = ""; mbody2.style.flexDirection = ""; mbody2.style.overflowY = ""; }
  $("modalRoot").hidden = true;
  $("modalBox").dataset.mode = "";
  $("modalBody").innerHTML = "";
}

/* ------------------------- 格式化工具 ------------------------- */

function formatDT(raw) {
  if (!raw || typeof raw !== "string") return "";
  // 兼容 isoformat（2026-09-29T18:30:00）与 "2026-09-29 18:30"（SSH ls 解析）
  const m = raw.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : "";
}

function formatWhen(raw) {
  const d = parseDate(raw);
  if (!d) return String(raw);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 604800) return `${Math.floor(diff / 86400)} 天前`;
  return d.toLocaleDateString("zh-CN");
}

function formatShort(raw) {
  const d = parseDate(raw);
  if (!d) return String(raw).slice(0, 16);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}<br>${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function parseDate(raw) {
  if (!raw) return null;
  // 快照名格式 snap-YYYYMMDD-HHMMSS
  const m = String(raw).match(/snap-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
  if (m) {
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  }
  const d = new Date(raw);
  return isNaN(d.getTime()) ? null : d;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;");
}

/* ------------------------- 告警聚合与轮询 ------------------------- */

// 顶栏横幅聚合三类告警：
//   v1 基线对比法（state.tamperAlerts，30s 轮询，廉价）
//   v2 内容完整性（state.deepAlerts 中 source=integrity）
//   v3 勒索行为检测（state.deepAlerts 中 source=behavior）
// 任一命中即顶栏展示；critical 用红色，其余用黄色。
function renderBanners() {
  const all = [...state.tamperAlerts, ...state.deepAlerts].filter(
    (a) => a && (a.level === "critical" || a.level === "warn")
  );
  if (!all.length) {
    const wasActive = state.tamperActive || state.deepActive;
    state.tamperActive = false;
    state.deepActive = false;
    showAnomalyBanner(); // 无勒索告警时展示硬件/容量类异常横幅（没有异常则收起横幅）
    return;
  }
  const critical = all.some((a) => a.level === "critical");
  const title = critical ? "⚠ 检测到勒索风险" : "快照保护状态异常";
  const body = all
    .map((a) => "• " + (a.title || "风险") + (a.detail ? "：" + a.detail : ""))
    .join("；");
  showBanner(critical ? "error" : "warn", title, body);
  state.tamperActive = state.tamperAlerts.length > 0;
  state.deepActive = state.deepAlerts.length > 0;
}

// 受保护快照（被 TS Safe 锁定的）一旦消失或被解锁，后端 /api/alerts 会告警。
// 这里定时拉取并顶栏展示，命中「快照被删的那一刻立刻告警」的核心卖点。
async function pollAlerts() {
  try {
    const data = await api("/api/alerts");
    state.tamperAlerts = (data.alerts || []).filter(
      (a) => a.level === "critical" || a.level === "warn"
    );
    renderBanners();
  } catch (e) {
    // 服务不可达时静默，不打扰用户
  }
}

/* ------------------------- v2 内容完整性深度校验 ------------------------- */

// 对受保护快照的实际内容做哈希/清单比对：文件被增删、改名或内容被替换都会告警。
async function runIntegrityCheck(silent = false) {
  const btn = $("integrityBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>校验中`;
  try {
    const data = await api("/api/integrity");
    const results = data.results || [];
    // 用 source 标记区分，覆盖上一次的 v2 结论
    state.deepAlerts = state.deepAlerts.filter((a) => a.source !== "integrity");
    for (const r of results) {
      state.deepAlerts.push({ ...r, source: "integrity" });
    }
    renderMonitorResults(results, "integrity");
    renderBanners();
    if (!silent) {
      if (results.length) {
        toast(`发现 ${results.length} 处快照内容异常`, "err");
      } else {
        toast("受保护快照内容完整，未被篡改", "ok");
      }
    }
  } catch (err) {
    toast("校验失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "校验快照是否被改动";
  }
}

/* ------------------------- v3 勒索行为检测 ------------------------- */

// 扫描生产（实时）目录：扩展名突变 / 熵值骤升 / 批量改名三类信号。
// 注意：需要服务器能本地访问这些目录（btrfs/zfs 本地模式）；QNAP 远程管理模式
// 下服务器在管理机，NAS 目录未挂载，会提示路径不可达。
async function runBehaviorScan(silent = false) {
  const btn = $("behaviorBtn");
  let paths = ($("watchPaths").value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!paths.length && state.activeVolume) paths = [state.activeVolume.mountpoint];
  if (!paths.length) {
    toast("请先选择一个卷，或在上方填写监控路径", "err");
    return;
  }
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>扫描中`;
  try {
    const qs = paths.map((p) => "paths=" + encodeURIComponent(p)).join("&");
    const data = await api("/api/behavior?" + qs);
    state.deepAlerts = state.deepAlerts.filter((a) => a.source !== "behavior");
    for (const s of data.signals || []) {
      state.deepAlerts.push({
        level: s.level,
        title: "勒索行为：" + s.type,
        detail: s.summary,
        source: "behavior",
      });
    }
    renderMonitorResults(data, "behavior");
    renderBanners();
    if (!silent) {
      if (data.suspicious) {
        toast("⚠ 检测到疑似勒索行为！", "err");
      } else {
        toast("未检测到明显勒索行为", "ok");
      }
    }
  } catch (err) {
    toast("扫描失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "扫描勒索迹象";
  }
}

/* ------------------------- 自动持续监控 ------------------------- */

// 开启后按选定间隔自动跑 v2 深度校验（默认每 5 分钟）；可选附带 v3 勒索行为扫描。
// 自动模式下静默成功提示（只更新结果区与顶栏横幅），避免每 5 分钟弹一次 toast 打扰用户；
function updateMonitorDot(on) {
  const dot = $("monitorDot");
  if (!dot) return;
  dot.className = "monitor-dot " + (on ? "monitor-dot-on" : "monitor-dot-off");
}

// 失败仍提示，便于第一时间发现监控链路异常。
function startAutoMonitor() {
  // 未登录不开自动监控：避免匿名用户触发 interval 后反复 403 打扰（控制需登录）
  if (!currentUser) { stopAutoMonitor(); return; }
  stopAutoMonitor();
  const ms = parseInt($("autoInterval").value, 10) || 300000;
  runIntegrityCheck(true);
  if ($("autoBehavior").checked) runBehaviorScan(true);
  state.autoMonitorTimer = setInterval(() => {
    runIntegrityCheck(true);
    if ($("autoBehavior").checked) runBehaviorScan(true);
  }, ms);
  state.autoMonitor = true;
  updateMonitorDot(true);
}

function stopAutoMonitor() {
  if (state.autoMonitorTimer) {
    clearInterval(state.autoMonitorTimer);
    state.autoMonitorTimer = null;
  }
  state.autoMonitor = false;
  updateMonitorDot(false);
}

// 刷新页面后按 localStorage 恢复自动监控状态（关闭页面不会丢失监控中状态）
function restoreAutoMonitor() {
  const on = localStorage.getItem("nassafe.autoMonitor") === "1";
  const iv = localStorage.getItem("nassafe.autoInterval");
  const beh = localStorage.getItem("nassafe.autoBehavior") === "1";
  if (iv) $("autoInterval").value = iv;
  $("autoBehavior").checked = beh;
  if (on && currentUser) {
    $("autoMonitor").checked = true;
    startAutoMonitor();
  } else {
    updateMonitorDot(false);
  }
  updateOverview();
}

/* ------------------------- 监控结果渲染 ------------------------- */

function renderMonitorResults(payload, kind) {
  const box = $("monitorResults");
  if (kind === "integrity") {
    const rs = payload || [];
    if (!rs.length) {
      box.innerHTML = `<p class="muted">✅ 校验通过：受保护快照里的文件与创建时完全一致，没有发现被篡改的迹象。</p>`;
      return;
    }
    box.innerHTML = rs
      .map(
        (r) => `
        <div class="alert-card warn">
          <div class="ac-title">${escapeHtml(r.title || "快照内容异常")}</div>
          <div class="ac-detail">${escapeHtml(r.detail || "")}</div>
        </div>`
      )
      .join("");
    return;
  }

  // behavior
  const d = payload || {};
  const suspicious = !!d.suspicious;
  const signals = d.signals || [];
  let html = `
    <div class="monitor-score ${suspicious ? "bad" : "good"}">
      风险评分：${d.score != null ? d.score : 0} / 100
      ${suspicious ? "⚠ 可疑" : "✅ 正常"}
    </div>`;
  const total = d.total_files || 0;
  html += `<div class="monitor-meta">扫描文件 ${total} 个 · 勒索扩展名 ${d.ransom_files || 0} · 高熵 ${d.high_entropy_files || 0}</div>`;

  if (!signals.length) {
    html += `<p class="muted">${escapeHtml(d.recommendation || "未检测到明显勒索行为，继续保持监控。")}</p>`;
  } else {
    html += signals
      .map(
        (s) => `
        <div class="alert-card ${s.level === "critical" ? "crit" : "warn"}">
          <div class="ac-title">${escapeHtml(s.type)} · ${s.level === "critical" ? "严重" : "警告"}</div>
          <div class="ac-detail">${escapeHtml(s.summary || "")}</div>
        </div>`
      )
      .join("");
    html += `<div class="notice">${escapeHtml(d.recommendation || "")}</div>`;
  }
  box.innerHTML = html;
}

/* ------------------------- 通知与 AI 设置 ------------------------- */

// 通知通道字段模板：按类型动态生成表单。
let notifyDraft = {};

// 后端/存储里偶尔会出现被字符串化的空值（"null" / "None"），直接回显会让用户
// 以为输入框里已经填了内容。统一在这里洗成空字符串。
function notifyVal(v) {
  if (v === null || v === undefined) return "";
  const s = String(v).trim();
  if (!s) return "";
  if (/^(null|undefined|none|nan)$/i.test(s)) return "";
  return s;
}

const NOTIFY_FIELDS = {
  relay: [
    { key: "recipients", label: "接收邮箱（多个用逗号或换行分隔）", multiline: true },
  ],
  wechat_service_account: [
    { key: "appid", label: "AppID" },
    { key: "appsecret", label: "AppSecret", secret: true },
    { key: "template_id", label: "模板 ID" },
    { key: "openid", label: "接收者 OpenID" },
  ],
  feishu: [
    { key: "url", label: "飞书机器人 Webhook 地址" },
    { key: "secret", label: "签名密钥（机器人没开签名校验就留空）", secret: true },
  ],
  webhook: [{ key: "url", label: "机器人地址（群设置里复制的网址）" }],
  telegram: [
    { key: "token", label: "机器人 Token（找 @BotFather 建机器人后复制的那串）", secret: true },
    { key: "chat_id", label: "接收人 ID（给 @userinfobot 发条消息就会告诉你）" },
  ],
  bark: [
    { key: "key", label: "推送钥匙（App 里复制的那串；也可以直接填完整网址）" },
    { key: "base", label: "服务地址（一般不用改，默认是官方的）" },
  ],
  ntfy: [
    { key: "topic", label: "频道名（自己起一个，手机上订阅填一样的）" },
    { key: "base", label: "服务地址（一般不用改，默认是官方的）" },
  ],
  email: [
    { key: "host", label: "SMTP 主机" },
    { key: "port", label: "端口（默认 465）" },
    { key: "user", label: "账号" },
    { key: "pass", label: "密码", secret: true },
    { key: "to", label: "收件人（不填就发给自己）" },
    { note: "常用邮箱照抄示例（都是免费的）：\n" +
            "· QQ 邮箱：主机 smtp.qq.com，端口 465，账号填 QQ 号，密码填邮箱设置里开启 SMTP 服务后生成的「授权码」（不是 QQ 密码）\n" +
            "· 163 邮箱：主机 smtp.163.com，端口 465，账号填邮箱名，密码填客户端授权密码\n" +
            "· Gmail：主机 smtp.gmail.com，端口 465，密码填应用专用密码（需先开两步验证）\n" +
            "· Outlook / 企业邮箱：主机 smtp.office365.com，端口 587，账号填完整邮箱" },
  ],
};

function renderNotifyFields(type) {
  const fields = NOTIFY_FIELDS[type] || [];
  $("notifyFields").innerHTML = fields
    .map(
      (f) => {
        if (!f) return "";
        if (f.note) {
          return `<div class="notice" style="margin:6px 0 12px; line-height:1.9; white-space:pre-wrap">${escapeHtml(f.note)}</div>`;
        }
        if (!f.key) return "";
        if (f.multiline) {
          return `
      <div class="set-row">
        <span class="set-label">${f.label}</span>
        <textarea id="nf_${f.key}" class="text-input" rows="3"
          placeholder="例如 a@qq.com, b@163.com">${escapeHtml(notifyVal(notifyDraft[f.key]))}</textarea>
      </div>`;
        }
        return `
      <div class="set-row">
        <span class="set-label">${f.label}</span>
        <input id="nf_${f.key}" class="text-input"
          type="${f.secret ? "password" : "text"}"
          value="${escapeAttr(notifyVal(notifyDraft[f.key]))}"
          placeholder="${f.secret ? "敏感信息，仅保存在本地" : ""}">
      </div>`;
      }
    )
    .join("");
  // 邮件代发就绪提示：没开通时必须给出能立刻上手的替代方案，不能只报错
  const hint = $("relayHint");
  const sw = $("relaySwitchBox");
  const relayOK = !!window.__relayAvailable;
  if (type === "relay") {
    if (hint) {
      hint.style.display = "block";
      hint.style.whiteSpace = "pre-wrap";
      hint.textContent = relayOK
        ? "✅ 邮件代发已就绪：填上接收邮箱就能收到报警，不用配任何 Key。"
        : "⚠️ 这台机器没有开通「邮件代发」，所以「邮件（填邮箱就能收）」发不出去。\n" +
          "✅ 现在就能用的免费办法：选「自备邮箱发信（SMTP）」——QQ / 163 邮箱照着下面的示例填，一分钟搞定（不要信任任何凭证）。\n" +
          "（点下面按钮可以一键切过去）";
    }
    if (sw) sw.style.display = relayOK ? "none" : "block";
  } else {
    if (hint) hint.style.display = "none";
    if (sw) sw.style.display = "none";
  }
}

// 一键从「邮件代发」切到「自备邮箱发信」：把已填的接收邮箱带到收件人栏，省得重打一遍
function switchToSmtp() {
  const old = notifyVal(notifyDraft.recipients);
  $("notifyType").value = "email";
  if (old) {
    const first = old.split(/[,;\uff0c\u3001\s]+/).filter(Boolean)[0];
    if (first) notifyDraft.to = first;
  }
  renderNotifyFields("email");
  toast("已切到「自备邮箱发信」，照着下面示例填好 SMTP，再点「发送测试」", "");
}

function gatherNotifyChannel() {
  const type = $("notifyType").value;
  const ch = { type };
  for (const f of NOTIFY_FIELDS[type] || []) {
    if (!f || !f.key) continue;
    const el = $("nf_" + f.key);
    const v = notifyVal(el ? el.value : "");
    if (v) ch[f.key] = v;
  }
  return ch;
}

async function saveNotify() {
  const cfg = { enabled: $("notifyEnabled").checked, channels: [gatherNotifyChannel()] };
  try {
    await api("/api/notify/config", { method: "POST", body: JSON.stringify(cfg) });
    toast("通知设置已保存", "ok");
  } catch (e) {
    toast("保存失败：" + e.message, "err");
  }
}

const CHANNEL_LABELS = {
  relay: "邮件",
  wechat_service_account: "微信服务号",
  feishu: "飞书机器人",
  telegram: "Telegram 推送",
  webhook: "群机器人",
  bark: "苹果手机推送",
  ntfy: "手机推送（安卓苹果都行）",
  email: "自备邮箱 SMTP",
};
function channelLabel(ch) { return CHANNEL_LABELS[ch] || ch || "服务端通道"; }

async function testNotify() {
  const ch = gatherNotifyChannel();
  try {
    const data = await api("/api/notify/test", { method: "POST", body: JSON.stringify({ channel: ch }) });
    if (data.ok) {
      toast("测试消息已发送，请查看接收端", "ok");
    } else {
      const m = data.msg || data.error || "未知";
      toast("发送失败：" + m, "err");
      if (!window.__relayAvailable && /中继未配置|RELAY_APIKEY/.test(m)) {
        toast("这台机器没开通邮件代发，请点「改用自备邮箱发信」按钮，照示例填 SMTP", "err");
      }
    }
  } catch (e) {
    toast("测试失败：" + e.message, "err");
  }
}

// 供应商切换：联动 API Key 是否必填 + 显示对应的引导提示（本地 Ollama 附带安装指引）。
function aiProviderChanged() {
  const prov = $("aiProvider").value;
  const isOllama = prov === "ollama";
  $("aiBaseRow").style.display = isOllama ? "" : "none";
  $("aiModelRow").style.display = isOllama ? "" : "none";
  // 切到本地 AI 时自动填好默认值，降低上手门槛
  if (isOllama) {
    if (!$("aiBase").value.trim()) $("aiBase").value = "http://localhost:11434/v1";
    if (!$("aiModel").value.trim()) $("aiModel").value = "qwen2.5:7b";
  }
  const hint = $("aiHint");
  if (isOllama) {
    hint.innerHTML =
      "本地 AI 跑在你自己的电脑上（Ollama / LM Studio 等），数据不出本机，无需密钥。" +
      "直连失败会自动改走 NAS 中转，此时需把服务地址填成电脑的局域网地址（如 <code>http://192.168.8.242:11434/v1</code>，Ollama 需开启「Expose to network」）。" +
      "若想浏览器直连，请以管理员设置系统变量 <code>OLLAMA_ORIGINS=*</code> 后重启 Ollama。";
  } else {
    const urls = {
      deepseek: "https://platform.deepseek.com",
      openai: "https://platform.openai.com",
      qwen: "https://dashscope.aliyun.com",
      zhipu: "https://open.bigmodel.cn",
      qiniu: "https://www.qiniu.com/products/ai",
    };
    hint.innerHTML = `去对应平台申请一个 API Key 粘贴到上面即可（<a href="${urls[prov] || '#'}" target="_blank" style="color:#38bdf8">${prov === "qiniu" ? "七牛云 AI" : "推荐 DeepSeek"}</a>）。数据将发送给该云端供应商。`;
  }
}

// 按当前版本裁剪可选的 AI 接口：用不了的置灰并标注，
// 免得用户选中后保存才被后端拒绝、还被弹到升级页。
function applyAiProviderLimits(list, current) {
  const sel = $("aiProvider");
  if (!sel || !Array.isArray(list) || !list.length) return;
  Array.from(sel.options).forEach((o) => {
    // 当前版本用不上的接口直接不显示（不是置灰），升级后自动出现在列表里
    const ok = list.indexOf(o.value) >= 0 || o.value === current;
    const base = (o.getAttribute("data-label") || o.textContent).replace(/（升级后可用）$/, "");
    o.setAttribute("data-label", base);
    o.textContent = base;
    o.hidden = !ok;
    o.style.display = ok ? "" : "none";
    o.disabled = !ok;
  });
  const hint = $("aiEditionHint");
  if (hint) {
    const names = Array.from(sel.options)
      .filter((o) => list.indexOf(o.value) >= 0)
      .map((o) => o.getAttribute("data-label") || o.textContent).join("、");
    hint.textContent = `当前版本可用：${names}。升级到家庭版后，其余云端 AI 和本地 AI 会自动加进列表。`;
  }
}

function applyNotifyTypeLimits(list, current) {
  const sel = $("notifyType");
  if (!sel || !Array.isArray(list) || !list.length) return;
  Array.from(sel.options).forEach((o) => {
    if (!o.value) return;
    const ok = list.indexOf(o.value) >= 0 || o.value === current;
    const base = (o.getAttribute("data-label") || o.textContent).replace(/（专业版）$/, "");
    o.setAttribute("data-label", base);
    o.textContent = base;
    o.hidden = !ok;
    o.style.display = ok ? "" : "none";
    o.disabled = !ok;
  });
  // 整组都被隐藏时，把分组标题也一起藏掉，不留空标题
  Array.from(sel.querySelectorAll("optgroup")).forEach((g) => {
    const any = Array.from(g.querySelectorAll("option")).some((o) => !o.hidden);
    g.hidden = !any;
    g.style.display = any ? "" : "none";
  });
  const hint = $("notifyEditionHint");
  if (hint) {
    const names = Array.from(sel.options)
      .filter((o) => o.value && list.indexOf(o.value) >= 0)
      .map((o) => o.getAttribute("data-label") || o.textContent).join("、");
    hint.textContent = `当前版本可用：${names}。升级到专业版后，会自动出现飞书、群机器人、手机推送、Telegram 等更多方式。`;
  }
}

async function saveAI() {
  const prov = $("aiProvider").value;
  const keyVal = ($("aiKey").value || "").trim();
  const cfg = {
    enabled: $("aiEnabled").checked,
    provider: prov,
    base_url: ($("aiBase").value || "").trim(),
    model: ($("aiModel").value || "").trim(),
  };
  // 本地地址/模型输入框只在「本地 AI」时显示；云端供应商一律清空这两个值，
  // 防止之前试本地 AI 时残留的 192.168.x.x:11434 地址串进云端配置（曾致云端 404）
  if (prov !== "ollama") {
    cfg.base_url = "";
    cfg.model = "";
  }
  // 空值或脱敏占位 *** 都不传 api_key，由后端保留旧 Key
  if (keyVal && keyVal !== "***") cfg.api_key = keyVal;
  // 保存类操作被版本卡点挡下时，只在原地提示，不要把用户弹到「版本与升级」菜单
  window.__suppressUpgradeJump = true;
  try {
    const data = await api("/api/ai/config", { method: "POST", body: JSON.stringify(cfg) });
    delete window.__suppressUpgradeJump;
    toast(
      data.ready ? "AI 设置已保存，可用" : "AI 设置已保存（未配置密钥，相关功能将隐藏）",
      "ok"
    );
  } catch (e) {
    delete window.__suppressUpgradeJump;
    toast("保存失败：" + e.message, "err");
  }
}

async function loadSettings() {
  try {
    const nc = await api("/api/notify/config");
    const cfg = nc.config || {};
    window.__relayAvailable = !!nc.relay_available;
    $("notifyEnabled").checked = !!cfg.enabled;
    const ch = (cfg.channels || [])[0] || {};
    if (ch.type) {
      $("notifyType").value = ch.type;
      notifyDraft = Object.assign({}, ch);
    } else {
      // 默认推荐：厂商邮件代发（填邮箱即用）；
      // 本机没开通代发时，直接给能用的「自备邮箱发信」，避免一上来就是死路
      $("notifyType").value = window.__relayAvailable ? "relay" : "email";
    }
    applyNotifyTypeLimits(nc.allowed_types, ch.type);
    renderNotifyFields($("notifyType").value);
  } catch (e) { /* 忽略 */ }

  try {
    const ac = await api("/api/ai/config");
    const cfg = ac.config || {};
    $("aiEnabled").checked = !!cfg.enabled;
    if (cfg.provider) $("aiProvider").value = cfg.provider;
    if (cfg.base_url) $("aiBase").value = cfg.base_url;
    if (cfg.model) $("aiModel").value = cfg.model;
    applyAiProviderLimits(ac.providers, cfg.provider);
    // 已设置的 Key 回显为 *** 占位，避免强刷后空输入框让用户误以为丢失；保存时 *** 不会被覆盖（由 saveAI 判断）
    if (cfg.api_key === "***") $("aiKey").value = "***";
  } catch (e) { /* 忽略 */ }
  loadDaily();
}

// 把报告（存储单元 + 告警 + 系统）交给 AI 翻译成大白话 + 处置建议。
async function aiInterpret() {
  const btn = $("aiInterpretBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>解读中`;
  let text = "";
  try {
    const [vols, alerts, sys] = await Promise.all([
      api("/api/volumes"),
      api("/api/alerts?integrity=1"),
      api("/api/system"),
    ]);
    const lines = ["【存储单元】"];
    (vols.volumes || []).forEach((v) =>
      lines.push(`- ${v.name} (${v.fs_type})：${v.snapshot_count} 张快照，最近 ${v.latest_snapshot || "无"}`)
    );
    lines.push("【告警】");
    const al = alerts.alerts || [];
    if (!al.length) lines.push("- 无");
    al.forEach((a) => lines.push(`- [${a.level}] ${a.title}：${a.detail}`));
    const s = sys.system;
    lines.push("【系统】");
    lines.push(`- ${s.os_name} 内核 ${s.kernel} 容器内=${s.is_container} 可用文件系统=${s.fs_available.join(",") || "无"}`);
    text = lines.join("\n");
  } catch (e) {
    toast("无法收集数据：" + e.message, "err");
    btn.disabled = false;
    btn.textContent = "AI 解读报告";
    return;
  }

  try {
    const data = await routeAI(
      "请解读以下 NAS 状态报告，用通俗中文指出风险等级，并给出 2-3 条可立即执行的处置建议（300 字以内）：\n\n" + text,
      "/api/ai/interpret"
    );
    openModal(
      "AI 解读报告",
      `<div style="white-space:pre-wrap;line-height:1.75;font-size:13.5px;color:var(--text)">${escapeHtml(data.text)}</div>`,
      `<button class="btn ghost" data-act="close">关闭</button>`,
      {},
      { stay: true }
    );
  } catch (e) {
    toast("AI 解读失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "AI 解读报告";
  }
}

/* ------------------------- 事件绑定 ------------------------- */

$("refreshBtn").onclick = async () => {
  $("refreshBtn").innerHTML = `<span class="spinner"></span>刷新中`;
  try {
    await loadVolumes();
    if (state.activeVolume) await loadSnapshots();
    toast("已刷新", "ok");
  } catch (err) {
    toast("刷新失败：" + err.message, "err");
  } finally {
    $("refreshBtn").textContent = "刷新";
  }
};

$("snapBtn").onclick = createSnapshot;
ensureEditionCache();
const _vssCleanBtn = $("vssCleanBtn");
if (_vssCleanBtn) _vssCleanBtn.onclick = () => vssCleanupRun(false);

/* =======================================================================
 * 二期：RAG 知识库（家庭/专业版专属）—— 弹窗 UI
 * 摄入文档 + 列表 + 自然语言提问（向量召回 + LLM 作答）
 * ===================================================================== */


// 设置页知识库 pane 的按钮只需绑定一次（元素常驻 DOM）
let _kbPaneBound = false;
/* ------------------------- 照片语义搜索（专业版） ------------------------- */
let _psBound = false;
let _psStop = false;

function initPhotoPane() {
  if (!_psBound) {
    const idx = $("psIndexBtn"), q = $("psQuery"), s = $("psSearchBtn");
    if (idx) idx.onclick = photoIndexRun;
    if (s) s.onclick = photoSearchRun;
    if (q) q.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); photoSearchRun(); } });
    _psBound = true;
  }
  photoStatusRefresh();
}

async function photoStatusRefresh() {
  try {
    const d = await api("/api/ai/photo/status");
    if (d && d.ok) {
      const el = $("psProgress");
      if (el && !el.dataset.busy) {
        el.textContent = d.indexed ? `已索引 ${d.indexed} 张照片${(d.roots || []).length ? "（目录：" + d.roots.join("、") + "）" : ""}` : "还没有索引过照片，填目录点「开始建索引」";
      }
      if (d.roots && d.roots.length && !$("psRoot").value) $("psRoot").value = d.roots[0];
    }
  } catch (e) { /* 专业版校验失败静默 */ }
}

async function photoIndexRun() {
  const root = ($("psRoot").value || "").trim();
  if (!root) { toast("请先填照片目录", "warn"); return; }
  const btn = $("psIndexBtn"), prog = $("psProgress");
  btn.disabled = true; btn.textContent = "索引中…（再点一次停止）";
  _psStop = false;
  let errs = 0;
  prog.dataset.busy = "1";
  try {
    for (let i = 0; i < 400; i++) {
      if (_psStop) { toast("已停止，下次点「开始建索引」会接着来", "warn"); break; }
      const d = await api("/api/ai/photo/index", { method: "POST", body: JSON.stringify({ root, batch: 10 }) });
      if (!d || !d.ok) { toast((d && (d.error || d.message)) || "索引失败", "err"); break; }
      prog.textContent = `已索引 ${d.indexed_total} 张，剩余 ${d.remaining}` + (d.last_error ? `（最近一条错误：${d.last_error}）` : "");
      if (d.done) { toast("索引完成", "ok"); break; }
      if (d.indexed_now === 0) { errs++; if (errs >= 2) { toast("连续多张失败已暂停：" + (d.last_error || "未知错误"), "err"); break; } }
      else errs = 0;
    }
  } catch (e) { toast("索引失败：" + e.message, "err"); }
  finally {
    btn.disabled = false; btn.textContent = "开始建索引";
    delete prog.dataset.busy;
    photoStatusRefresh();
  }
}

async function photoSearchRun() {
  const q = ($("psQuery").value || "").trim();
  const box = $("psResults");
  if (!q) { toast("先输入想找的内容", "warn"); return; }
  box.innerHTML = `<div class="muted"><span class="spinner"></span> 搜索中…</div>`;
  try {
    const d = await api("/api/ai/photo/search", { method: "POST", body: JSON.stringify({ query: q }) });
    if (!d || !d.ok) { box.innerHTML = `<div class="muted">${esc((d && (d.error || d.message)) || "搜索失败")}</div>`; return; }
    if (!d.results || !d.results.length) { box.innerHTML = `<div class="muted">没找到匹配的照片（共索引 ${d.total_indexed} 张）。试试换个说法，或先建索引。</div>`; return; }
    box.innerHTML = d.results.map((r, i) => `
      <div style="border:1px solid var(--border);border-radius:10px;overflow:hidden;background:var(--card, #fff)">
        <img src="/api/ai/photo/thumb?path=${encodeURIComponent(r.path)}" loading="lazy"
             style="width:100%;height:140px;object-fit:cover;display:block" onerror="this.style.display='none'">
        <div style="padding:8px 10px">
          <div style="font-size:13px;line-height:1.5">${esc(r.desc || "")}</div>
          <div class="muted" style="font-size:12px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.path)}">${esc(r.path)}</div>
          <div class="muted" style="font-size:12px">匹配度 ${(r.score * 100).toFixed(0)}%</div>
        </div>
      </div>`).join("");
  } catch (e) {
    box.innerHTML = `<div class="muted">搜索失败：${esc(String(e.message || e))}</div>`;
  }
}

function initKbPane() {
  if (!_kbPaneBound) {
    const ingest = $("kbIngestBtn"), refresh = $("kbRefreshBtn"), query = $("kbQueryBtn"), q = $("kbQuery");
    if (ingest) ingest.onclick = kbIngest;
    if (refresh) refresh.onclick = kbRefresh;
    if (query) query.onclick = kbQuery;
    if (q) q.addEventListener("keydown", (e) => { e.key === "Enter" && (e.preventDefault(), kbQuery()); });
    _kbPaneBound = true;
  }
  // 免费版没有知识库（家庭版起）：显示升级提示、隐藏功能框，避免填了才被后端拒绝。
  api("/api/license").then((d) => {
    const free = (d.edition || "free") === "free";
    const tip = $("kbFreeTip");
    if (tip) tip.style.display = free ? "" : "none";
    document.querySelectorAll("#kbPaneBody .kb-block").forEach((el) => { el.style.display = free ? "none" : ""; });
  }).catch(() => {});
  kbRefresh();
}

async function kbRefresh() {
  const quotaEl = $("kbQuota");
  const listEl = $("kbDocList");
  if (!listEl) return;
  try {
    const data = await api("/api/ai/kb/list");
    if (quotaEl) quotaEl.textContent = (typeof data.remaining === "number" && data.remaining >= 0)
      ? `本月知识库剩余调用：${data.remaining} 次` : "知识库调用额度：不限量";
    if (!data.docs || !data.docs.length) {
      listEl.innerHTML = `<div class="muted">还没有文档，先在上方摄入。</div>`;
      return;
    }
    listEl.innerHTML = data.docs.map(d =>
      `<div style="display:flex;align-items:center;gap:8px;padding:6px 4px;border-bottom:1px solid var(--border)">
         <div style="flex:1;min-width:0">
           <div style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(d.title)}</div>
           <div class="muted" style="font-size:12px">${esc(d.kind || "")} · ${d.chunks} 段 · ${d.created}</div>
         </div>
         <button class="btn ghost sm" data-del="${d.id}" data-title="${esc(d.title)}">删除</button>
       </div>`).join("");
    listEl.querySelectorAll("button[data-del]").forEach(b => {
      b.onclick = () => kbDelete(b.getAttribute("data-del"), b.getAttribute("data-title"));
    });
  } catch (err) {
    listEl.innerHTML = `<div class="muted">加载失败：${esc(err.message)}</div>`;
  }
}

async function kbIngest() {
  const btn = $("kbIngestBtn");
  const title = ($("kbTitle").value || "").trim();
  const fileInput = $("kbFile");
  const text = ($("kbText").value || "").trim();
  if (btn) { btn.disabled = true; btn.textContent = "摄入中…"; }
  try {
    if (fileInput && fileInput.files && fileInput.files[0]) {
      const f = fileInput.files[0];
      const ext = (f.name.split(".").pop() || "txt").toLowerCase();
      let payload;
      if (["txt", "md", "srt", "text"].includes(ext)) {
        const txt = await f.text();
        payload = { title: title || f.name, text: txt, kind: ext };
      } else {
        const b64 = await new Promise((res, rej) => {
          const r = new FileReader();
          r.onload = () => res(r.result.split(",")[1]);
          r.onerror = () => rej(new Error("读取文件失败"));
          r.readAsDataURL(f);
        });
        payload = { title: title || f.name, data_base64: b64, ext: ext };
      }
      const r = await api("/api/ai/kb/ingest", { method: "POST", body: JSON.stringify(payload) });
      toast(`已摄入：${r.title}（${r.chunks} 段）`, "ok");
      $("kbText").value = ""; if (fileInput) fileInput.value = ""; $("kbTitle").value = "";
    } else if (text) {
      const r = await api("/api/ai/kb/ingest", { method: "POST", body: JSON.stringify({ title: title || "粘贴内容", text }) });
      toast(`已摄入：${r.title}（${r.chunks} 段）`, "ok");
      $("kbText").value = ""; $("kbTitle").value = "";
    } else {
      toast("请选择文件或粘贴文本", "warn");
    }
    await kbRefresh();
  } catch (err) {
    toast("摄入失败：" + err.message, "err");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "摄入"; }
  }
}

async function kbDelete(id, title) {
  if (!confirm(`确认删除「${title}」？`)) return;
  try {
    await api("/api/ai/kb/delete", { method: "POST", body: JSON.stringify({ id }) });
    toast("已删除", "ok");
    await kbRefresh();
  } catch (err) {
    toast("删除失败：" + err.message, "err");
  }
}

async function kbQuery() {
  const qEl = $("kbQuery");
  const ansEl = $("kbAnswer");
  const btn = $("kbQueryBtn");
  const q = (qEl.value || "").trim();
  if (!q) { toast("请输入问题", "warn"); return; }
  if (btn) { btn.disabled = true; btn.textContent = "检索中…"; }
  if (ansEl) ansEl.innerHTML = `<span class="spinner"></span> 正在检索知识库并生成回答…`;
  try {
    const data = await api("/api/ai/kb/query", { method: "POST", body: JSON.stringify({ question: q }) });
    let html = "";
    if (data.text) html += `<div>${esc(data.text)}</div>`;
    if (data.sources && data.sources.length) {
      html += `<div class="muted" style="font-size:12px;margin-top:8px">来源：${data.sources.map(s => esc(s.title)).join("、")}</div>`;
    }
    if (ansEl) ansEl.innerHTML = html;
  } catch (err) {
    if (ansEl) ansEl.textContent = "查询失败：" + err.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "提问"; }
  }
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 知识库入口已并入设置页 pane；仪表盘只保留「问 AI」一个 AI 按钮（问答/管家合一）
$("integrityBtn").onclick = () => { if (!currentUser) { requireLoginFor("monitor"); return; } runIntegrityCheck(); };
$("behaviorBtn").onclick = () => { if (!currentUser) { requireLoginFor("monitor"); return; } runBehaviorScan(); };
$("aiInterpretBtn").onclick = aiInterpret;
$("aiDiagnoseBtn").onclick = aiDiagnose;
$("aiAskBtn").onclick = aiAsk;
// AI 全页悬浮窗：任意页面点击即可提问（复用 aiAsk 弹窗）
if ($("aiFabBtn")) $("aiFabBtn").onclick = aiAsk;
$("notifyType").onchange = () => renderNotifyFields($("notifyType").value);
$("notifySaveBtn").onclick = saveNotify;
$("notifyTestBtn").onclick = testNotify;
if ($("relayToSmtpBtn")) $("relayToSmtpBtn").onclick = switchToSmtp;
$("aiProvider").onchange = aiProviderChanged;
$("aiSaveBtn").onclick = saveAI;
// 输入即暂存到 draft，切换通道类型时不丢已填内容
$("notifyFields").addEventListener("input", (e) => {
  if (e.target.id && e.target.id.startsWith("nf_")) {
    notifyDraft[e.target.id.slice(3)] = e.target.value;
  }
});

function persistAutoMonitor() {
  localStorage.setItem("nassafe.autoMonitor", state.autoMonitor ? "1" : "0");
  localStorage.setItem("nassafe.autoInterval", $("autoInterval").value);
  localStorage.setItem("nassafe.autoBehavior", $("autoBehavior").checked ? "1" : "0");
}

$("autoMonitor").onchange = (e) => {
  if (!currentUser) {
    e.target.checked = false;          // 未登录：还原勾选并提示去登录
    requireLoginFor("monitor");
    return;
  }
  if (e.target.checked) {
    startAutoMonitor();
    toast("已开启自动持续监控", "ok");
  } else {
    stopAutoMonitor();
    toast("已关闭自动监控", "");
  }
  persistAutoMonitor();
  updateOverview();
};
$("autoInterval").onchange = () => {
  if (state.autoMonitor) startAutoMonitor(); // 间隔变更后重启计时以生效
  persistAutoMonitor();
};
$("autoBehavior").onchange = () => {
  if (state.autoMonitor) startAutoMonitor(); // 勾选后立即生效
  persistAutoMonitor();
};

$("browseBtn").onclick = () => {
  const latest = state.snapshots[state.snapshots.length - 1];
  if (latest) openBrowser(latest, isQnapSnap(latest) ? "" : latest.path);
};

$("modalClose").onclick = closeModal;
$("modalMask").onclick = closeModal;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modalRoot").hidden) {
    // 含输入框的弹窗不靠 Esc 关闭，避免误触丢失已输入内容
    if (modalHasInput || modalSticky) return;
    closeModal();
  }
});

boot();

// 自动监控状态恢复：刷新页面后按上次设置恢复（localStorage 持久化）
restoreAutoMonitor();

// 加载已保存的通知 / AI 设置到面板
loadSettings();
// 按当前供应商显示 AI 引导提示（Ollama 附安装指引）
aiProviderChanged();

// ---- 护眼主题：随日出日落自动切换浅色/深色（设置页开关，localStorage 持久化）----
// 日出日落近似计算（中纬度，误差约 ±15 分钟）：昼长随季节正弦变化
function sunTimes(d) {
  const doy = Math.floor((d - new Date(d.getFullYear(), 0, 0)) / 86400000);
  const daylight = 12 + 2.4 * Math.sin((2 * Math.PI * (doy - 81)) / 365);
  return [12 - daylight / 2, 12 + daylight / 2]; // [日出, 日落]（小时）
}
function isDaylight(d = new Date()) {
  const h = d.getHours() + d.getMinutes() / 60;
  const [rise, set] = sunTimes(d);
  return h >= rise && h < set;
}
function applyAutoTheme() {
  const on = localStorage.getItem("nassafe_auto_theme") === "1";
  document.body.classList.toggle("light", on && isDaylight());
}
const themeChk = $("autoThemeChk");
if (themeChk) {
  themeChk.checked = localStorage.getItem("nassafe_auto_theme") === "1";
  themeChk.onchange = () => {
    localStorage.setItem("nassafe_auto_theme", themeChk.checked ? "1" : "0");
    applyAutoTheme();
    if (typeof toast === "function") {
      toast(themeChk.checked ? "已开启：白天浅色、夜间深色，自动切换" : "已关闭：始终使用当前配色", "ok");
    }
  };
}
applyAutoTheme();
setInterval(applyAutoTheme, 60000); // 每分钟检查一次，跨过日出/日落自动换肤

// 主导航：绑定标签页点击。
// 数据查看类（设备控制台/本机数据/联机数据/快照预览）免登录直接看；
// 「告警信息」是独立的告警与历史页面（看告警免登录）；
// 只有「功能设置」是受保护入口，未登录点它会先要求注册/登录。
document.querySelectorAll("#mainTabs .tab").forEach((t) => {
  t.onclick = () => {
    const v = t.dataset.view;
    if (v === "settings") requireLoginFor("settings");
    else if (v === "alerts") showView("alerts");
    else showView(v);
  };
});
applyView();
_syncInitialRoute();

// 跨品牌多设备总控制台：按钮绑定
$("consoleRefreshBtn").onclick = () => loadConsole(true);
$("monRefreshBtn").onclick = () => loadDeviceOverview(true);
$("exportMigBtn").onclick = exportConfig;
$("importMigFile").onchange = (e) => { if (e.target.files[0]) onPickMigFile(e.target.files[0]); };
$("migPreviewBtn").onclick = previewMig;
if ($("migTargetDevice")) $("migTargetDevice").onchange = onPickMigTarget;
$("migApplyBtn").onclick = applyMig;

// 系统仪表盘：手动刷新 + 15s 自动轮询（仅主页可见时取数，SSH 采集有 15s 缓存）
$("metricsRefreshBtn").onclick = () => loadMetrics(true);
$("alertsRefreshBtn").onclick = () => loadAlertsPage();
$("pickPathsBtn").onclick = openPathPicker;
$("aiDiscoverBtn").onclick = aiDiscover;
// 顶部告警横幅可点击：打开异常面板（逐项查看 + AI 修复方案）
$("alertBanner").onclick = () => openAnomalyModal();
$("alertBanner").style.cursor = "pointer";
$("alertBanner").title = "点击查看异常详情";

// 异常主动提醒：电脑弹窗授权 + 三个开关（本地 AI 文案 / 电脑弹窗 / 远端通道）
// 电脑弹窗权限引导：浏览器不给自动同意的口子（尤其 http 地址连询问框都不弹），
// 这里用一个弹框把「怎么允许」讲清楚，并给出「已允许 → 点我验证」的按钮，不让用户自己瞎找。
async function notifyPermState() {
  if (!("Notification" in window)) return "unsupported";
  try {
    if (navigator.permissions && navigator.permissions.query) {
      const st = await navigator.permissions.query({ name: "notifications" });
      if (st && st.state) return st.state; // granted / prompt / denied
    }
  } catch (_) { /* 部分浏览器不支持 permissions.query，回退下面的值 */ }
  return Notification.permission;
}

function showNotifyPermGuide(why) {
  const isHttps = location.protocol === "https:";
  const steps = isHttps
    ? `<p>点击下面「立即允许」，浏览器会弹一个询问框，选「允许」即可。</p>`
    : `<p class="muted">当前地址是 <b>http</b>（不是 https），浏览器默认不给网页弹询问框，
       所以要你在浏览器设置里手动允许一次。<b>只需要做一次，之后永久生效。</b></p>
       <ol class="perm-steps">
         <li>看浏览器<b>地址栏最左边</b>，点那个 ⓘ（或「调节」小图标）</li>
         <li>点 <b>网站设置</b>（有的浏览器叫「权限」）</li>
         <li>找到 <b>通知</b>，把它改成 <b>允许</b></li>
         <li>回到这个窗口，点右下角 <b>「我已在浏览器里允许，点击验证」</b></li>
       </ol>
       <p class="muted">如果上面找不到，也可以复制这段地址到地址栏打开（Edge 换成 edge://）：
         <code>chrome://settings/content/notifications</code>，
         然后把 <b>${escapeHtml(location.host)}</b> 加进「允许发送通知」名单。</p>
       <div class="notice warn" style="margin-top:8px">
         如果你在网站设置里看到「通知」的选项是<b>灰色的、改不了</b>（显示"已屏蔽相应权限"）——
         那不是你操作错了：新版浏览器对 <b>http</b> 地址会直接锁死通知权限。
         这种情况只能等正式域名 <b>https://nassafe.tsetch.com</b> 上线后一键授权；
         在此期间请点下方<b>「改用桌面小助手」</b>，关掉网页也能收到提醒。
       </div>`;
  const body = `
    <p>${escapeHtml(why || "")}</p>
    ${steps}
    <div id="permState" class="notice" style="margin-top:10px">正在检查当前权限…</div>`;

  const verifyLabel = isHttps
    ? "立即允许（浏览器会弹询问框）"
    : "我已在浏览器里允许，点击验证";
  const foot = `
    <button class="btn ghost" data-act="agent">改用桌面小助手</button>
    <button class="btn ghost" data-act="close">稍后再说</button>
    <button class="btn primary" data-act="verify">${verifyLabel}</button>`;

  const refresh = async () => {
    const box = $("permState");
    if (!box) return;
    const st = await notifyPermState();
    if (st === "granted") {
      box.className = "notice ok";
      box.textContent = "✅ 已经生效，可以正常收到电脑弹窗提醒了。";
    } else if (st === "denied") {
      box.className = "notice warn";
      box.textContent = "⚠ 浏览器里仍是「已拒绝」。请按上面 1–4 步在网站设置里改成「允许」，再点验证。";
    } else {
      box.className = "notice";
      box.textContent = `当前状态：${st === "unsupported" ? "浏览器不支持桌面通知" : "尚未授权"}。改完设置后点右下角验证即可。`;
    }
    return st;
  };

  openModal("🔔 开启电脑弹窗提醒", body, foot, {
    verify: async () => {
      let st = await notifyPermState();
      if (st === "prompt") {
        // 还有救：浏览器允许弹询问框时就直接问一次（https 或 localhost 场景）
        st = await Notification.requestPermission();
      }
      if (st === "granted") {
        localStorage.setItem("nassafe_desk_notify", "1");
        $("deskNotifyChk").checked = true;
        closeModal();
        toast("已授权电脑弹窗提醒", "ok");
        return;
      }
      await refresh();
    },
    agent: () => showDesktopAgentGuide(),
  });
  refresh();
}

// 关掉网页也想收到提醒 → 桌面小助手：下载 zip → 解压 → 双击 → 安装窗口确认
function showDesktopAgentGuide() {
  const hasZip = true;
  openModal(
    "🖥 安装桌面小助手（关掉网页也能提醒）",
    `<p>装上这个本机小助手后，<b>彻底关掉网页也能收到提醒</b>：它缩在电脑右下角的托盘图标里，
       有异常时图标上亮起红点并弹一次提醒；开机自启，一直守护。</p>
     <ol class="perm-steps">
       <li>点右下角 <b>「⬇ 下载安装包（zip）」</b>，得到 <code>桌面助手.zip</code>（要连哪台设备已经帮你填好了，什么都不用填）</li>
       <li><b>右键解压</b>到任意文件夹，双击里面的 <code>桌面助手.exe</code></li>
       <li>自动弹出「安装 TS Safe 助手」窗口并显示进度，几秒后提示「安装完成」——
           <b>不用选地址、不用填任何东西</b></li>
       <li>右下角出现 <b>蓝紫色盾牌图标</b>（和网页左上角的标志一样），鼠标放上去显示「TS Safe · 快照保护中」</li>
     </ol>
     <p class="muted">不需要安装 Python，也不需要管理员权限。<br>
       若 Windows 提示「已保护你的电脑」：点 <b>更多信息 → 仍要运行</b>（未签名软件的正常提示）。<br>
       以后在「设置 → 异常主动提醒」里可一键启停；主动退出后，提醒会自动改走微信服务号 / 邮件，不会丢。</p>`,
    `<button class="btn ghost" data-act="exe">直接下载 exe（不解压）</button>
     <button class="btn primary" data-act="download">⬇ 下载安装包（zip）</button>`,
    {
      download: () => triggerDownload("/agent/NASSafeAgent.zip", "桌面助手.zip",
        "已开始下载，解压后双击「桌面助手.exe」即可"),
      exe: () => triggerDownload("/agent/NASSafeAgent.exe", "桌面助手.exe",
        "已开始下载「桌面助手.exe」（若被浏览器拦截，请改用 zip）"),
    }
  );
  void hasZip;
}

// 下载完整版安装包：先向本总控台申请一次性「报到票据」，票据随安装包下发。
// 新机器装完启动后会自动回来登记 + 每分钟心跳，总控台因此能显示它在线。
// 关键原因：总控台可能在公网、被管机器在内网，总控台无法反向连过去，
// 只能靠设备主动上报（这台机器若没拿到票据，安装依然能正常，只是不会自动登记）。
async function downloadFullBundle(devName) {
  let ticket = "";
  try {
    const r = await api("/api/devices/full-ticket", {
      method: "POST",
      body: JSON.stringify({ name: devName || "", source: location.origin }),
    });
    ticket = (r && r.ticket) || "";
  } catch (e) {
    toast("没拿到自动登记票据（需登录管理员），安装包仍会照常下载", "");
  }
  const q = ticket ? "?ticket=" + encodeURIComponent(ticket) : "";
  triggerDownload("/api/downloads/full-bundle" + q, "NAS-Safe-Full.zip",
    "已开始下载完整版安装包（解压后按《首次使用指南》安装；装完这台机器会自动回到本总控台登记并保持在线）");
}

// 触发真实下载（a[download]，比 window.open 可靠，不会被当成弹窗拦掉）
function triggerDownload(href, filename, okMsg) {
  const a = document.createElement("a");
  // 加时间戳/版本戳：强制浏览器不拿缓存里的旧版 zip/exe
  const sep = href.includes("?") ? "&" : "?";
  a.href = href + sep + "_v=1.0.6.12&t=" + Date.now();
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  if (okMsg) toast(okMsg, "ok");
}

if ("Notification" in window) {
  $("notifyPermBtn").onclick = async () => {
    const st = await notifyPermState();
    if (st === "granted") { toast("已授权电脑弹窗提醒", "ok"); return; }
    if (st === "prompt") {
      const p = await Notification.requestPermission();
      if (p === "granted") {
        localStorage.setItem("nassafe_desk_notify", "1");
        $("deskNotifyChk").checked = true;
        toast("已授权电脑弹窗提醒", "ok");
        return;
      }
    }
    showNotifyPermGuide("浏览器要求你亲手允许通知权限（网页无法代替用户点同意）。");
  };
} else {
  $("notifyPermBtn").onclick = () => showNotifyPermGuide("当前浏览器不支持桌面通知。");
}
// ------------------------- 桌面小助手：设置页开关直控 -------------------------
// 小助手在本机 127.0.0.1:18765 提供 /ping（在线检测）与 /stop（请求退出）；
// 启动走 nassafe-agent:// 自定义协议（安装时注册），浏览器会弹一次「打开？」确认。
const AGENT_CTRL = "http://127.0.0.1:18765";

function setTestResult(t) {
  const el = document.getElementById("testResult");
  if (el) el.textContent = t;
}

function fetchT(url, ms) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  return fetch(url, { signal: ctl.signal }).finally(() => clearTimeout(t));
}

async function agentPing(ms = 1500) {
  try {
    const r = await fetchT(`${AGENT_CTRL}/ping`, ms);
    const data = await r.json();
    return { ok: data.agent === "nassafe", ver: data.ver || "old", unread: data.unread || 0 };
  } catch (_) {
    return { ok: false, ver: "", unread: 0 };
  }
}

function setAgentState(text, running) {
  const el = $("agentState");
  if (el) {
    el.textContent = text;
    el.style.color = running === true ? "var(--z-monitor,#2fe0a0)" : running === false ? "var(--text-3,#8892a6)" : "";
  }
}

async function refreshAgentState() {
  const { ok: on, ver, unread } = await agentPing();
  const chk = $("agentChk");
  if (chk) {
    chk.checked = on;
    chk.disabled = false;
  }
  const verText = on ? `● 运行中${ver && ver !== "old" ? " · v" + ver : ""}${unread ? " · " + unread + " 条未读" : ""}` : "未运行";
  setAgentState(verText, on);
  // 小助手在跑时，电脑提醒全部由它接管，网页弹窗备选自动隐藏
  const webRow = $("webNotifyRow");
  if (webRow) webRow.hidden = on;
  localStorage.setItem("nassafe_agent_enabled", on ? "1" : "0");
  if (on) localStorage.setItem("nassafe_agent_ever", "1");   // 记录"曾经连上过=已安装"
  return on;
}

// 拉起本机小助手：安装时已注册 nassafe-agent:// 协议，即使当前未运行也能启动
function launchAgent() {
  try { location.href = "nassafe-agent://start"; } catch (_) { /* 忽略 */ }
}

// 轮询等待小助手上线（启动可能要几秒，避免一次失败就误判"未安装"）
async function waitAgentOnline(maxMs = 9000, stepMs = 1000) {
  const end = Date.now() + maxMs;
  while (Date.now() < end) {
    if ((await agentPing(1200)).ok) return true;
    await new Promise((r) => setTimeout(r, stepMs));
  }
  return false;
}

// 已安装但当前未运行：给"重新启动"入口，而不是又弹下载页
// 拉起桌面小助手：优先让**本机服务端**直接起进程（可靠），
// 失败再退回浏览器的 nassafe-agent:// 协议（协议没注册时浏览器会静默失败）。
async function relaunchAgent() {
  try {
    const r = await api("/api/agent/launch", { method: "POST", body: "{}" });
    if (r && r.ok) {
      const ok = await waitAgentOnline(12000);
      if (ok) return true;
      if (r.already_running) return true;
    }
  } catch (e) { /* 权限/未登录等，走协议兜底 */ }
  launchAgent();
  return await waitAgentOnline(9000);
}

// 把服务端诊断结果渲染成大白话清单
function renderAgentDiag(d) {
  if (!d || !d.applicable) {
    return `<p class="muted">${escapeHtml((d && d.reason) || "本机不是 Windows，桌面小助手不可用。")}</p>`;
  }
  const p = d.protocol || {};
  const pr = d.program || {};
  const yn = (b) => (b ? '<b style="color:#2fe0a0">是</b>' : '<b style="color:#f87171">否</b>');
  const rows = [
    ["助手进程是否在运行", yn(!!d.running) + ` <span class="muted">(端口 ${d.ctrl_port})</span>`],
    ["开机启动协议是否注册", yn(!!p.registered)],
    ["协议指向的程序是否还在", yn(!!p.target_exists) + (p.target ? ` <span class="muted">${escapeHtml(p.target)}</span>` : "")],
    ["程序目录是否存在", yn(!!pr.dir_exists) + (pr.dir ? ` <span class="muted">${escapeHtml(pr.dir)}</span>` : "")],
  ];
  let html = `<div class="notice" style="line-height:2">${rows
    .map((r) => `<div>· ${r[0]}：${r[1]}</div>`)
    .join("")}</div>`;
  if (d.verdict) {
    html += `<p><b>${escapeHtml(d.verdict)}</b></p>`;
  }
  if (d.advice) {
    html += `<p class="muted">${escapeHtml(d.advice)}</p>`;
  }
  if (Array.isArray(d.log) && d.log.length) {
    html += `<p class="muted" style="margin-top:8px">助手日志（最后 ${d.log.length} 条）：</p>
      <pre style="max-height:160px;overflow:auto;background:rgba(127,127,127,.12);padding:8px;border-radius:8px;font-size:12px;white-space:pre-wrap">${escapeHtml(
        d.log.join("\n")
      )}</pre>`;
  }
  return html;
}

async function showAgentNotRunning() {
  let diagHtml = `<p class="muted">正在检测本机情况…</p>`;
  const dlg = openModal(
    "🖥 桌面小助手未运行",
    `<p>检测到桌面小助手<b>已经安装过</b>，但当前没有在运行。</p>
     <p class="muted">下面这台电脑的实际情况（由本机 TS Safe 直接检查）：</p>
     <div id="agentDiagBox">${diagHtml}</div>`,
    `<button class="btn ghost" data-act="rediag">重新检测</button>
     <button class="btn ghost" data-act="redown">重新下载安装</button>
     <button class="btn primary" data-act="relaunch">重新启动</button>`,
    {
      relaunch: async (btn) => {
        if (btn) btn.disabled = true;
        toast("正在启动桌面小助手…", "");
        const ok = await relaunchAgent();
        if (ok) { closeModal(); await refreshAgentState(); toast("桌面小助手已启动", "ok"); }
        else { toast("还是没起来，请把上面的检测结果发我", "warn"); }
      },
      rediag: async () => {
        const box = $("agentDiagBox");
        if (box) box.innerHTML = `<p class="muted">正在重新检测…</p>`;
        const d2 = await api("/api/agent/diag").catch((e) => ({ applicable: false, reason: String(e.message || e) }));
        if (box) box.innerHTML = renderAgentDiag(d2);
      },
      redown: () => showDesktopAgentGuide(),
    }
  );
  const d = await api("/api/agent/diag").catch((e) => ({ applicable: false, reason: String(e.message || e) }));
  const box = $("agentDiagBox");
  if (box) box.innerHTML = renderAgentDiag(d);
  return dlg;
}

async function onAgentToggle() {
  const chk = $("agentChk");
  if (chk.disabled) return;
  chk.disabled = true;
  try {
    if (chk.checked) {
      if ((await agentPing()).ok) {
        setAgentState("● 运行中", true);
        localStorage.setItem("nassafe_agent_enabled", "1");
        toast("桌面小助手已在运行", "ok");
        return;
      }
      setAgentState("正在启动…", null);
      // 浏览器不允许网页直接执行本地程序，走自定义协议拉起（浏览器可能弹一次「打开？」）
      launchAgent();
      const online = await waitAgentOnline(9000);
      if (online) {
        setAgentState("● 运行中", true);
        localStorage.setItem("nassafe_agent_enabled", "1");
        localStorage.setItem("nassafe_agent_ever", "1");
        toast("桌面小助手已启动并开机自启", "ok");
      } else {
        chk.checked = false;
        // 已经装过 → 提示"未运行"而非"未安装/下载"
        if (localStorage.getItem("nassafe_agent_ever") === "1") {
          setAgentState("已安装 · 未运行", false);
          showAgentNotRunning();
        } else {
          setAgentState("未安装", false);
          showDesktopAgentGuide();
        }
      }
    } else {
      setAgentState("正在停止…", null);
      let stopped = false;
      try {
        await fetchT(`${AGENT_CTRL}/stop`, 2500);
        stopped = true;
      } catch (_) { /* 进程可能本就不在 */ }
      await new Promise((r) => setTimeout(r, 2500));
      const still = await agentPing();
      if (still.ok) {
        chk.checked = true;
        setAgentState("● 运行中", true);
        toast("小助手未响应停止请求，请稍后重试", "warn");
      } else {
        setAgentState(stopped ? "已停止" : "未运行", false);
        localStorage.setItem("nassafe_agent_enabled", "0");
        toast("桌面小助手已停止", "ok");
      }
    }
  } finally {
    chk.disabled = false;
  }
}

$("agentChk").onchange = onAgentToggle;
$("agentInstallBtn").onclick = showDesktopAgentGuide;
refreshAgentState();
setInterval(() => { if (!$("agentChk").disabled) refreshAgentState(); }, 30000);

$("deskNotifyChk").checked = localStorage.getItem("nassafe_desk_notify") === "1";
$("deskNotifyChk").onchange = () => localStorage.setItem("nassafe_desk_notify", $("deskNotifyChk").checked ? "1" : "0");
$("pushTestBtn").onclick = async () => {
  const btn = $("pushTestBtn");
  btn.disabled = true;
  const t0 = toast("正在发送测试提醒…", "");
  setTestResult("正在发送测试提醒…");
  let localOk = false;
  try {
    // 桌面助手在线时优先本机直推（瞬间到达托盘），成功立即反馈
    const p = await agentPing();
    if (p.ok) {
      try {
        const lr = await fetch(`${AGENT_CTRL}/notify`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: "TS Safe 测试提醒",
            detail: "这是一条异常提醒通道的测试消息",
            level: "warn",
            key: "test-" + Date.now()
          })
        });
        localOk = lr.ok;
      } catch (_) { localOk = false; }
      if (localOk) toast("桌面助手已收到测试提醒 ✓", "ok");
      setTestResult("✓ 桌面助手已收到测试提醒");
    }
    // 同时走服务端通道（微信/邮件等），带超时避免卡死
    try {
      const r = await api("/api/notify/alert", {
        method: "POST",
        body: JSON.stringify({ title: "TS Safe 测试提醒", detail: "这是一条异常提醒通道的测试消息", level: "warn" })
      }, 8000);
      if (r && r.ok) {
        toast(`已推送（桌面助手 + ${channelLabel(r.channel)}）`, "ok");
        setTestResult(`✓ 已推送（桌面助手 + ${channelLabel(r.channel)}）`);
      } else if (!localOk) {
        toast("推送失败：" + ((r && (r.msg || r.error)) || "未知"), "err");
        setTestResult("✗ 推送失败：" + ((r && (r.msg || r.error)) || "未知"));
      }
    } catch (e) {
      if (!localOk) {
        toast("推送失败：" + (e.message || "未知"), "err");
        setTestResult("✗ 推送失败：" + (e.message || "未知"));
      } else {
        toast("桌面助手已收到，但服务端通道失败：" + (e.message || "未知"), "warn");
        setTestResult("⚠ 桌面助手已收到，但服务端通道失败：" + (e.message || "未知"));
      }
    }
  } catch (e) {
    toast("推送失败：" + e.message, "err");
    setTestResult("✗ 推送失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
};
// 时间轴页：卷切换下拉框 —— 不用回总览，直接换卷看时间轴
$("tlVolumeSel").onchange = () => {
  const key = $("tlVolumeSel").value;
  // 联机设备模式：下拉里是那台设备的卷，走代理重新拉
  if (state.snapDevice && state.snapDevice !== "local") { loadRemoteSnapshots(); return; }
  const vol = state.volumes.find((v) => (v.mountpoint ?? String(v.id)) === key);
  if (vol) selectVolume(vol);
};
loadMetrics();
setInterval(() => {
  if ((localStorage.getItem("nassafe_view") || "home") === "home") loadMetrics();
}, 15000);

// 监控路径：恢复上次选择器保存的值（优先于自动填充）
const savedWp = localStorage.getItem("nassafe_watchpaths");
if (savedWp != null) $("watchPaths").value = savedWp;

// 篡改检测告警轮询：每 30s 拉一次 /api/alerts，发现异常则顶栏告警
pollAlerts();
setInterval(pollAlerts, 30000);

// ---- 自动快照设置 ----
loadAutoSnap();
$("autoSnapSaveBtn").onclick = saveAutoSnap;
$("autoSnapRunBtn").onclick = runAutoSnap;

// ---- 每日健康日报 ----
loadDaily();
$("dailySaveBtn").onclick = saveDaily;
$("dailyRunBtn").onclick = runDaily;

// ---- 版本与激活 ----
const UPGRADE_ROWS = [
  ["可管理设备", "2 台（含本机）", "5 台 + 轻量代理", "不限 + 异地组网设备"],
  ["一键备份 / 换机迁移", "—", "✅", "✅"],
  ["跨设备控制台", "—", "✅", "✅"],
  ["消息通知", "邮件 + 微信服务号", "邮件 + 微信服务号", "+ 群机器人 / 飞书 / 手机推送 / Telegram"],
  ["AI 接口", "3 种云端", "全部常见云端 + 本地模型", "自定义 API 随意接"],
  ["自动快照 / 日报 / 清理", "全有", "全有", "全有"],
  ["防勒索 / 快照 / 取回", "全有", "全有", "全有"],
];

// 升级入口：跳到「版本与升级」pane 看三档对比；若指定版本则直接弹微信支付
function openUpgradeModal(edition) {
  showSettingsTab("license");
  if (edition === "home" || edition === "business") {
    const price = LIC_PRICES[edition] || 0;
    const name = { home: "家庭版", business: "专业版" }[edition] || edition;
    openWeChatPayModal(edition, price, name);
  }
}

// ---- 版本与升级 ----
// 三档能力对照表（与后端 editions.py 同步；价格由 /api/license/prices 实时覆盖）
const LIC_MATRIX = {
  rows: [
    { label: "可管理设备数", free: "2 台", home: "5 台", business: "不限" },
    { label: "数据保护功能", free: "全开", home: "全开", business: "全开" },
    { label: "消息通知", free: "邮件 + 微信", home: "邮件 + 微信", business: "邮件 + 微信 + 自定义" },
    { label: "云端 AI", free: "3 种", home: "全部常见 + 本地", business: "全部 + 自定义接口" },
    { label: "换机迁移", free: "—", home: "✓", business: "✓" },
    { label: "异地组网设备", free: "—", home: "—", business: "✓" },
    { label: "知识库 (RAG)", free: "—", home: "✓", business: "✓（不限）" },
    { label: "应急响应自动化", free: "—", home: "—", business: "✓" },
    { label: "照片语义搜索", free: "—", home: "—", business: "✓" },
  ],
  cols: [["free", "免费版"], ["home", "家庭版"], ["business", "专业版"]],
  level: { free: 0, home: 1, business: 2 },
};
let LIC_PRICES = { home: 8, business: 18 };

function loadLicense() {
  Promise.all([api("/api/license"), api("/api/license/prices")]).then(([d, p]) => {
    if (!d || !d.ok) return;
    window.__lic = d;
    window.__licIssueDemo = !!d.issue_demo;
    window.__licPayUrl = (d.pay_url || "").trim();
    if (p && p.ok && p.prices) LIC_PRICES = p.prices;
    $("licFp").textContent = d.fingerprint || "—";
    let label = d.label || d.edition || "—";
    if (d.licensed) label += d.permanent ? "（永久有效）" : "（已激活）";
    $("licEdition").textContent = label;
    renderLicenseCompare(d.edition || "free");
  }).catch(() => {});
}

function renderLicenseCompare(current) {
  const box = $("licCompare");
  if (!box) return;
  const cols = LIC_MATRIX.cols;
  const cls = { free: "c0", home: "c1", business: "c2" };
  // 表头：版本名 + 价格 + 当前版本标记 + 升级按钮
  let head = `<div class="lc-row lc-head"><div class="lc-cell lc-feat">功能区别</div>`;
  for (const [key, name] of cols) {
    const isCur = key === current;
    const lv = LIC_MATRIX.level[key], clv = LIC_MATRIX.level[current];
    let btn = "";
    if (isCur) btn = `<button class="up-btn cur" disabled>当前版本</button>`;
    else if (lv > clv) btn = `<button class="up-btn" data-up="${key}">升级 ¥${LIC_PRICES[key] || 0}</button>`;
    else btn = `<span class="up-btn na">—</span>`;
    head += `<div class="lc-cell lc-col ${cls[key]}${isCur ? " cur" : ""}"><div class="lc-colname">${name}</div>${isCur ? '<div class="lc-curtag">当前</div>' : ""}${btn}</div>`;
  }
  head += `</div>`;
  // 数据行（隔行变色）
  let rows = LIC_MATRIX.rows.map((r, i) => {
    const cls2 = i % 2 ? " odd" : "";
    let cells = `<div class="lc-cell lc-feat${cls2}">${r.label}</div>`;
    for (const [key] of cols) {
      const v = r[key];
      const mark = (v === "✓") ? " ok" : (v === "—" ? " no" : "");
      cells += `<div class="lc-cell lc-col ${cls[key]}${cls2}${key === current ? " cur" : ""}"><span class="lc-val${mark}">${v}</span></div>`;
    }
    return `<div class="lc-row${cls2}">${cells}</div>`;
  }).join("");
  box.innerHTML = `<div class="lc-table">${head}${rows}</div>`;
  box.querySelectorAll("button[data-up]").forEach((b) => {
    b.onclick = () => upgradeTo(b.dataset.up);
  });
}

function upgradeTo(edition) {
  const price = LIC_PRICES[edition] || 0;
  const name = { home: "家庭版", business: "专业版" }[edition] || edition;
  const base = (window.__licPayUrl || "").trim();
  if (base) startRealPay(base, edition, price, name);
  else openWeChatPayModal(edition, price, name);
}

// 真实微信支付：发卡网关下单 → 展示二维码 → 轮询订单 → 支付成功自动填入激活码。
// 网关不可达（如域名解析未恢复）时自动回落到线下购买引导。
async function startRealPay(base, edition, price, name) {
  const fp = ($("licFp").textContent || "").trim();
  const api = base.replace(/\/$/, "");
  let d = null;
  try {
    const r = await fetch(api + "/order", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_id: "ts_" + edition, machine_code: fp })
    });
    d = await r.json();
    if (!d || !d.ok || !d.code_url) throw new Error((d && d.error) || "下单失败");
  } catch (e) {
    toast("在线支付暂不可用，已切换线下购买方式", "warn");
    openWeChatPayModal(edition, price, name);
    return;
  }
  const orderId = d.order_id;
  openModal(
    "微信支付 · 升级到" + name,
    `<div class="wx-pay">
       <div class="wx-pay-head"><span class="wx-logo">💚</span> 微信扫码支付</div>
       <div class="wx-amount">¥${price}<small> .00</small></div>
       <div class="wx-qrtitle">请用微信「扫一扫」付款</div>
       <div class="wx-qr" id="wxQr"></div>
       <p class="muted" id="wxPayState" style="font-size:12px;margin:8px 0 0">等待支付中…支付成功会自动填入激活码（本窗可关，激活码也可稍后到「版本与升级」页粘贴）。</p>
       <div class="wx-machine">本机机器码：<code>${escapeHtml(fp)}</code></div>
     </div>`,
    `<button class="btn ghost" data-act="close">关闭</button>`,
    {}
  );
  const qrEl = $("wxQr");
  if (qrEl) {
    try {
      const q = qrcode(0, "M");
      q.addData(d.code_url);
      q.make();
      qrEl.innerHTML = q.createSvgTag({ cellSize: 4, margin: 2 });
    } catch (_) { qrEl.textContent = d.code_url; }
  }
  const box = $("modalBox");
  if (box) box.style.maxWidth = "420px";
  let tries = 0;
  const timer = setInterval(async () => {
    tries++;
    if (tries > 240 || !$("wxQr")) { clearInterval(timer); return; }
    try {
      const st = await fetch(api + "/order/" + orderId);
      const sd = await st.json();
      if (sd && sd.ok && sd.status === "paid" && sd.license_key) {
        clearInterval(timer);
        closeModal();
        $("licKey").value = sd.license_key;
        const el = $("licStatus");
        if (el) { el.style.display = "block"; el.className = "notice ok"; el.textContent = "支付成功！激活码已填入，点下方「激活」完成升级。"; }
        toast("支付成功，已获取激活码", "ok");
      }
    } catch (_) { /* 网络抖动忽略，下轮再试 */ }
  }, 2500);
}

// 模拟微信支付窗口：真实项目里这里会展示由发卡端生成的微信支付二维码；
// 本机演示版点「我已支付」后由服务端按本机机器码实时签一张激活码，自动填入激活框。
function openWeChatPayModal(edition, price, name) {
  const fp = ($("licFp").textContent || "").trim();
  const demo = !!window.__licIssueDemo;
  // 在线支付未接（默认）：不展示假二维码与「我已支付」，改为引导线下购买；
  // 演示环境（NASSAFE_ISSUE_DEMO=1）保持原模拟支付流程。
  const bodyHtml = demo
    ? `<div class="wx-pay-head"><span class="wx-logo">💚</span> 微信支付</div>
       <div class="wx-amount">¥${price}<small> .00</small></div>
       <div class="wx-qrtitle">请使用微信扫码支付（演示）</div>
       <div class="wx-qr" id="wxQr"></div>
       <div class="wx-scanline"></div>
       <p class="muted" style="font-size:12px;margin:8px 0 0">本项目开源，付费用于持续开发与专属服务。<br>演示环境点「我已支付」即可模拟支付成功。</p>
       <div class="wx-machine">本机机器码：<code>${escapeHtml(fp)}</code></div>`
    : `<div class="wx-pay-head"><span class="wx-logo">💚</span> 升级到${name}</div>
       <div class="wx-amount">¥${price}<small> .00</small></div>
       <p style="line-height:1.9;margin:10px 0 4px">在线支付即将开通。开通前购买方式：<br>
         ① 把下面的<b>机器码</b>发给卖家（复制按钮在下方）；<br>
         ② 微信转账 ¥${price} 并注明版本（${name}）；<br>
         ③ 卖家签发激活码，粘贴到「版本与升级」页的激活框点「激活」。</p>
       <div class="wx-machine" style="font-size:14px">本机机器码：<code>${escapeHtml(fp)}</code></div>`;
  openModal(
    (demo ? "微信支付 · " : "升级 · ") + name,
    `<div class="wx-pay">${bodyHtml}</div>`,
    demo
      ? `<button class="btn ghost" data-act="close">取消</button>
         <button class="btn primary" data-act="paid">我已支付</button>`
      : `<button class="btn ghost" data-act="close">关闭</button>
         <button class="btn primary" data-act="copyfp">复制机器码</button>`,
    demo
      ? {
          paid: async () => {
            try {
              const r = await api("/api/license/issue", { method: "POST", body: JSON.stringify({ edition }) });
              if (!r || !r.ok) { toast("发码失败：" + (r && r.error || ""), "err"); return; }
              closeModal();
              $("licKey").value = r.code;
              toast(`已获取 ${name} 授权码，点击「激活」完成升级`, "ok");
              const st = $("licStatus");
              if (st) { st.style.display = "block"; st.className = "notice"; st.textContent = `已自动填入 ${name} 授权码，点击下方「激活」即可升级。`; }
            } catch (e) { toast("支付/发码失败：" + (e.message || e), "err"); }
          },
        }
      : {
          copyfp: () => {
            if (navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(fp).then(() => toast("机器码已复制，发给卖家即可", "ok")).catch(() => {});
            } else {
              const ta = document.createElement("textarea");
              ta.value = fp; document.body.appendChild(ta); ta.select();
              document.execCommand("copy"); ta.remove(); toast("机器码已复制，发给卖家即可", "ok");
            }
          },
        }
  );
  // 演示模式画一个装饰性二维码（非真实可扫）
  const qr = $("wxQr");
  if (qr) {
    let cells = "";
    for (let i = 0; i < 169; i++) cells += `<i class="${Math.random() > 0.5 ? "on" : ""}"></i>`;
    qr.innerHTML = cells;
  }
  const box = $("modalBox");
  if (box) { box.style.maxWidth = "420px"; }
}
$("licCopyFp").onclick = () => {
  const fp = $("licFp").textContent || "";
  if (!fp || fp === "—") return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(fp).then(() => toast("机器码已复制", "ok")).catch(() => {});
  } else {
    const ta = document.createElement("textarea");
    ta.value = fp; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove(); toast("机器码已复制", "ok");
  }
};
$("licActivateBtn").onclick = () => {
  const key = ($("licKey").value || "").trim();
  if (!key) { toast("先把激活码粘贴进来", "warn"); return; }
  const st = $("licStatus");
  st.style.display = "block";
  api("/api/license/activate", { method: "POST", body: JSON.stringify({ key }) })
    .then((d) => {
      st.className = "notice ok";
      st.textContent = d.message || "激活成功";
      $("licKey").value = "";
      loadLicense();
      toast(d.message || "激活成功", "ok");
    })
    .catch((e) => {
      st.className = "notice warn";
      st.textContent = e.message || "激活失败";
      toast("激活失败：" + (e.message || e), "err");
    });
};
loadLicense();

// ---- 设置页菜单切换 ----
// 所有子菜单都在设置页右侧 pane 内显示（含工具类：重复文件/磁盘清理/换机迁移），
// 不再单独跳出全屏视图，避免每次弄完又要重新点「功能设置」进来。
document.querySelectorAll(".settings-tab").forEach((btn) => {
  btn.onclick = () => {
    const tab = btn.dataset.tab;
    if (tab === "devices") { addDevice(); return; }   // 添加设备是弹窗操作，已在登录态设置页内，直接开
    showSettingsTab(tab);
  };
});
function loadAutoSnap() {
  api("/api/autosnapshot").then((d) => {
    if (!d || !d.ok) return;
    const c = d.config || {};
    $("autoSnapEnabled").checked = !!c.enabled;
    $("autoSnapInterval").value = c.interval_hours != null ? c.interval_hours : 1;
    $("autoSnapKeep").value = c.keep != null ? c.keep : 48;
    $("autoSnapVolumes").value = (c.volumes || []).join(",");
  }).catch(() => {});
}

function saveAutoSnap() {
  const volumes = ($("autoSnapVolumes").value || "")
    .split(",").map((s) => s.trim()).filter(Boolean);
  const cfg = {
    enabled: $("autoSnapEnabled").checked,
    interval_hours: parseInt($("autoSnapInterval").value, 10) || 1,
    keep: parseInt($("autoSnapKeep").value, 10) || 48,
    volumes: volumes,
  };
  api("/api/autosnapshot", { method: "POST", body: JSON.stringify(cfg) })
    .then(() => toast("自动快照设置已保存", "ok"))
    .catch((e) => toast("保存失败：" + (e && e.message ? e.message : e), "err"));
}

function runAutoSnap() {
  const st = $("autoSnapStatus");
  st.style.display = "block";
  st.className = "notice";
  st.textContent = "正在执行一轮自动快照（远程模式可能需要数秒）…";
  api("/api/autosnapshot/run", { method: "POST", body: "{}" })
    .then((d) => {
      if (!d || !d.ok) {
        st.className = "notice warn";
        st.textContent = "执行失败：" + ((d && d.error) || "未知");
        return;
      }
      const created = (d.created || []).length;
      const cleaned = (d.cleaned || []).length;
      const errs = (d.errors || []);
      let msg = "已完成：新建 " + created + " 个快照，清理 " + cleaned + " 个旧快照";
      if (errs.length) msg += "；" + errs.length + " 个错误（详见后端日志）";
      st.className = errs.length ? "notice warn" : "notice ok";
      st.textContent = msg;
      toast(msg, errs.length ? "warn" : "ok");
    })
    .catch((e) => {
      st.className = "notice warn";
      st.textContent = "执行失败：" + (e && e.message ? e.message : e);
    });
}

/* ------------------------- 每日健康日报 ------------------------- */

function loadDaily() {
  api("/api/daily-report").then((d) => {
    if (!d || !d.ok) return;
    const c = d.config || {};
    $("dailyEnabled").checked = !!c.enabled;
    $("dailyHour").value = c.hour != null ? c.hour : 8;
    $("dailyMinute").value = c.minute != null ? c.minute : 0;
    renderDailyLast(d.last);
  }).catch(() => {});
}

function renderDailyLast(last) {
  const box = $("dailyLast");
  if (!last) { box.style.display = "none"; return; }
  box.style.display = "block";
  box.className = "notice";
  const r = last.report || {};
  const sent = last.dispatch || {};
  let ch = "";
  if (sent.enabled === false) ch = "（通知总开关未开，仅本地存档，未推送）";
  else if (sent.sent) {
    const ok = sent.sent.filter((x) => x.ok).map((x) => x.channel);
    ch = ok.length ? "上次已推送：" + ok.join("、") : "上次推送失败：" + ((sent.sent[0] || {}).msg || "");
  }
  box.innerHTML =
    `<div style="white-space:pre-wrap;line-height:1.7;margin-bottom:6px">` +
    escapeHtml(last.text || "") +
    `</div><div class="muted">发送时间 ${escapeHtml(last.sent_at || "")} ${escapeHtml(ch)}</div>`;
}

function saveDaily() {
  const cfg = {
    enabled: $("dailyEnabled").checked,
    hour: parseInt($("dailyHour").value, 10) || 0,
    minute: parseInt($("dailyMinute").value, 10) || 0,
  };
  api("/api/daily-report/config", { method: "POST", body: JSON.stringify(cfg) })
    .then(() => toast("每日健康日报设置已保存", "ok"))
    .catch((e) => toast("保存失败：" + (e && e.message ? e.message : e), "err"));
}

function runDaily() {
  const st = $("dailyLast");
  st.style.display = "block";
  st.className = "notice";
  st.textContent = "正在生成并推送日报（远程模式需数秒）…";
  api("/api/daily-report/run", { method: "POST", body: "{}" })
    .then((d) => {
      if (!d || !d.ok) { st.className = "notice warn"; st.textContent = "失败：" + ((d && d.error) || "未知"); return; }
      renderDailyLast(d.last);
      toast("日报已发送", "ok");
    })
    .catch((e) => { st.className = "notice warn"; st.textContent = "失败：" + (e && e.message ? e.message : e); });
}

/* ------------------------- 重复文件清理 ------------------------- */
// 原则：扫描只读出报告；清理 = 移入隔离区（软删除，可恢复）；每组至少保留一份。

function fmtBytes(n) {
  if (n == null || isNaN(n)) return "--";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + u[i];
}

let dupPollTimer = null;

function dupSetStatus(html, kind) {
  const st = $("dupStatus");
  if (!html) { st.style.display = "none"; return; }
  st.style.display = "block";
  st.className = "notice" + (kind ? " " + kind : "");
  st.innerHTML = html;
}

async function startDupScan() {
  const root = ($("dupRoot").value || "").trim();
  if (!root) { toast("请先填写或选择要扫描的目录", "warn"); return; }
  const btn = $("dupScanBtn");
  btn.disabled = true;
  try {
    await api(targetApi("/api/duplicates/scan", state.dupTarget), {
      method: "POST",
      body: JSON.stringify({ root }),
    }, 15000);
    toast("扫描已开始，正在清点文件…", "ok");
    pollDupStatus();
  } catch (e) {
    toast("启动扫描失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

function pollDupStatus() {
  clearInterval(dupPollTimer);
  dupPollTimer = setInterval(refreshDupStatus, 2000);
  refreshDupStatus();
}

async function refreshDupStatus() {
  let st;
  try { st = await api(targetApi("/api/duplicates/status", state.dupTarget)); } catch { return; }
  if (st.status === "scanning") {
    if (st.phase === "inventory") {
      dupSetStatus(`<span class="spinner"></span>正在清点文件… 已发现 ${st.files_seen} 个（隐藏目录 / 回收站 / 系统目录自动跳过）`);
    } else if (st.phase === "hash") {
      const pct = st.candidate_bytes
        ? Math.min(99, Math.round((st.hashed_bytes / st.candidate_bytes) * 100))
        : 0;
      dupSetStatus(`<span class="spinner"></span>正在逐字节比对 ${st.candidates} 个疑似重复文件… ${pct}%（${fmtBytes(st.hashed_bytes)} / ${fmtBytes(st.candidate_bytes)}）`);
    } else {
      dupSetStatus(`<span class="spinner"></span>扫描中…`);
    }
  } else {
    if (dupPollTimer) { clearInterval(dupPollTimer); dupPollTimer = null; }
    if (st.status === "error") {
      dupSetStatus("扫描失败：" + escapeHtml(st.error || "未知"), "warn");
    } else if (st.status === "done") {
      dupSetStatus("");
    }
  }
}

async function loadDupReport() {
  refreshDupStatus();
  let d;
  try { d = await api(targetApi("/api/duplicates/report", state.dupTarget)); } catch { return; }
  renderDupReport(d);
  loadDupQuarantine();
}

function renderDupReport(d) {
  const box = $("dupResults");
  if (d.empty || !d.groups || !d.groups.length) {
    box.innerHTML = `<p class="muted">当前没有重复文件报告，或上次扫描没发现重复 —— 选个目录扫一下吧。${d.scanned_at ? `（上次扫描 ${escapeHtml(String(d.scanned_at).slice(0, 16).replace("T", " "))}，未发现重复）` : ""}</p>`;
    return;
  }

  // ---- 推荐引擎：广告词命中 / 原始版识别 / 置信度标注 ----
  const AD_RE = /(公众号|广告|推广|宣传|加微信|扫码关注|领福利|douyin|抖音号)/i;
  const COPY_RE = /(\(\d+\)|\[\d+\]|副本|copy|备份|\.bak)/i;
  const baseName = (p) => (p.split("/").pop() || p);
  const stemOf = (p) => {
    const b = baseName(p);
    const dot = b.lastIndexOf(".");
    return (dot > 0 ? b.slice(0, dot) : b).replace(COPY_RE, "").trim();
  };

  // 每组预处理：标记广告命中、判断能否自动推荐
  // 能自动 = 广告版与干净版并存（无脑留干净版）；或去掉 (1)/副本 等后缀后文件名相同（纯改名副本）
  const entries = (d.groups || []).map((g) => {
    const files = g.files.map((f) => ({ ...f, ad: AD_RE.test(f.path) }));
    const nonAd = files.filter((f) => !f.ad);
    const hasAdSplit = nonAd.length > 0 && nonAd.length < files.length;
    const stemsEqual = new Set(files.map((f) => stemOf(f.path))).size === 1;
    return { g, files, nonAd, manual: !(hasAdSplit || stemsEqual) };
  });

  const sizeMap = {};
  entries.forEach((en) => en.files.forEach((f) => { sizeMap[f.path] = f.size; }));

  const mtimeOf = (f) => f.mtime || 0;
  const baseLen = (f) => baseName(f.path).length;

  // 按策略选组内该保留的文件（manual 组也算出推荐项，供展示但默认不勾）
  function pickKeep(en, policy) {
    const pool = policy === "recommend" && en.nonAd.length ? en.nonAd : en.files;
    const sorted = [...pool];
    if (policy === "newest") sorted.sort((a, b) => mtimeOf(b) - mtimeOf(a) || baseLen(a) - baseLen(b));
    else if (policy === "shortest") sorted.sort((a, b) => baseLen(a) - baseLen(b) || mtimeOf(a) - mtimeOf(b));
    else sorted.sort((a, b) => mtimeOf(a) - mtimeOf(b) || baseLen(a) - baseLen(b));
    return sorted[0].path;
  }

  const KEEP_LABEL = { recommend: "原始版", earliest: "最早版本", shortest: "名最短", newest: "最新版本" };

  // 自动组排前、人工组排后；各自按可释放空间降序
  entries.sort((a, b) => (a.manual - b.manual) || (b.g.wasted - a.g.wasted));
  const manualCount = entries.filter((en) => en.manual).length;

  const head = `
    <div class="dup-summary">
      <b>${d.group_count} 组重复内容</b>
      <span class="muted">扫描 ${escapeHtml(d.root)} · 全清可释放 ${fmtBytes(d.wasted_bytes || 0)} · ${escapeHtml(String(d.scanned_at).slice(0, 16).replace("T", " "))}</span>
      <span class="dup-policy">
        <span class="muted">保留策略</span>
        <label><input type="radio" name="dupPolicy" value="recommend" checked> 推荐</label>
        <label><input type="radio" name="dupPolicy" value="earliest"> 保留最早</label>
        <label><input type="radio" name="dupPolicy" value="shortest"> 文件名最短</label>
        <label><input type="radio" name="dupPolicy" value="newest"> 保留最新</label>
      </span>
    </div>
    <div class="dup-cta">
      <div>
        <div id="dupCtaText">正在计算推荐…</div>
        ${manualCount ? `<div class="muted dup-cta-sub">${manualCount} 组文件名差异较大、无法自动判断，默认未勾选，请展开核对</div>` : ""}
      </div>
      <button class="btn primary dup-cta-btn" id="dupQuarantineBtn" disabled>按推荐清理</button>
    </div>`;

  const html = entries.map((en, i) => {
    const g = en.g;
    const rows = en.files.map((f) => {
      const when = f.mtime ? new Date(f.mtime * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
      const bn = baseName(f.path);
      const dir = f.path.slice(0, f.path.length - bn.length);
      const nameHtml = f.ad
        ? `<bdi>${escapeHtml(dir)}<mark class="dup-mark">${escapeHtml(bn)}</mark></bdi>`
        : `<bdi>${escapeHtml(f.path)}</bdi>`;
      return `
        <label class="pick-row dup-file">
          <input type="checkbox" class="dup-cb" data-path="${escapeAttr(f.path)}">
          <span class="dup-path" title="${escapeAttr(f.path)}">${nameHtml}</span>
          <span class="dup-badge" data-path="${escapeAttr(f.path)}" data-ad="${f.ad ? 1 : 0}"></span>
          <span class="dup-meta">${fmtBytes(f.size)}${when ? " · " + escapeHtml(when) : ""}</span>
        </label>`;
    }).join("");
    return `
      <div class="dup-group${en.manual ? " dup-manual" : ""}" data-manual="${en.manual ? 1 : 0}" data-gi="${i}">
        <div class="dup-group-head">
          <b>组 ${i + 1} / ${entries.length}</b>
          <span class="muted">内容完全相同 · 单个 ${fmtBytes(g.size)} × ${en.files.length} 份 · 可释放 ${fmtBytes(g.wasted)}</span>
          ${en.manual
            ? `<span class="dup-flag dup-flag-warn">建议人工确认</span>`
            : `<span class="dup-flag dup-flag-ok" data-role="flag"></span>`}
          <button class="btn ghost dup-group-clean" data-gi="${i}" disabled>清理勾选</button>
        </div>
        ${rows}
        ${en.manual ? `<div class="dup-manual-note">文件名差异大且没有广告词，系统不猜哪份是原版 —— 核对后手动勾选多余的（每组至少保留一份）。</div>` : ""}
      </div>`;
  }).join("");

  box.innerHTML = head + `<div class="dup-groups">${html}</div>`;

  function applySelection() {
    const policyEl = box.querySelector('input[name="dupPolicy"]:checked');
    const policy = policyEl ? policyEl.value : "recommend";
    box.querySelectorAll(".dup-group").forEach((grp) => {
      const en = entries[Number(grp.dataset.gi)];
      const keep = pickKeep(en, policy);
      grp.querySelectorAll(".dup-cb").forEach((cb) => {
        cb.checked = !en.manual && cb.dataset.path !== keep;
      });
      const flag = grp.querySelector('[data-role="flag"]');
      if (flag) flag.textContent = "自动保留 " + KEEP_LABEL[policy];
      grp.querySelectorAll(".dup-badge").forEach((b) => {
        if (b.dataset.path === keep) {
          b.textContent = "保留 · " + (en.manual ? "推荐" : KEEP_LABEL[policy]);
          b.className = "dup-badge dup-badge-keep";
        } else if (b.dataset.ad === "1") {
          b.textContent = "删 · 含广告词";
          b.className = "dup-badge dup-badge-del";
        } else {
          b.textContent = "删 · 多余副本";
          b.className = "dup-badge dup-badge-del";
        }
      });
    });
    updateSummary();
  }

  function updateSummary() {
    const cbs = [...box.querySelectorAll(".dup-cb:checked")];
    const bytes = cbs.reduce((s, cb) => s + (sizeMap[cb.dataset.path] || 0), 0);
    const btn = $("dupQuarantineBtn");
    $("dupCtaText").innerHTML = cbs.length
      ? `已为你勾选 <b>${cbs.length}</b> 项 · 将释放 <b class="dup-free">${fmtBytes(bytes)}</b> · 每组保留一份`
      : `当前没有勾选任何文件 —— 切换保留策略或手动勾选（每组至少保留一份）`;
    btn.textContent = cbs.length ? `清理 ${cbs.length} 项（释放 ${fmtBytes(bytes)}）` : "按推荐清理";
    btn.disabled = !cbs.length;
    // 每组自己的「清理勾选」按钮：随本组勾选实时启停
    box.querySelectorAll(".dup-group").forEach((grp) => {
      const gcbs = [...grp.querySelectorAll(".dup-cb:checked")];
      const gbytes = gcbs.reduce((s, cb) => s + (sizeMap[cb.dataset.path] || 0), 0);
      const gbtn = grp.querySelector(".dup-group-clean");
      if (!gbtn) return;
      gbtn.textContent = gcbs.length ? `清理本组 ${gcbs.length} 项（${fmtBytes(gbytes)}）` : "清理勾选";
      gbtn.disabled = !gcbs.length;
    });
  }

  // 手动勾选/取消时实时刷新汇总与按钮（含顶部 CTA 和每组按钮）
  box.addEventListener("change", (ev) => {
    if (ev.target && ev.target.classList && ev.target.classList.contains("dup-cb")) {
      updateSummary();
    }
  });

  // 每组「清理勾选」：只把本组勾选的文件移入隔离区
  box.querySelectorAll(".dup-group-clean").forEach((gbtn) => {
    gbtn.onclick = () => {
      const grp = gbtn.closest(".dup-group");
      const paths = [...grp.querySelectorAll(".dup-cb:checked")].map((cb) => cb.dataset.path);
      if (!paths.length) { toast("本组没有勾选任何文件", "warn"); return; }
      const gbytes = paths.reduce((s, p) => s + (sizeMap[p] || 0), 0);
      openModal(
        "清理本组勾选",
        `<p>即将把本组勾选的 <b>${paths.length}</b> 个文件（约 ${fmtBytes(gbytes)}）移入隔离目录。</p>
         <p class="muted">文件<b>不会被删除</b>，只是移入「.nassafe-quarantine」隔离目录，随时可以在隔离区一键恢复。</p>`,
        `<button class="btn ghost" data-act="close">取消</button>
         <button class="btn primary" data-act="ok">确认移入隔离区</button>`,
        { ok: () => doDupQuarantine(paths) }
      );
    };
  });

  box.querySelectorAll('input[name="dupPolicy"]').forEach((r) => { r.onchange = applySelection; });
  applySelection();

  $("dupQuarantineBtn").onclick = () => {
    const paths = [...box.querySelectorAll(".dup-cb:checked")].map((cb) => cb.dataset.path);
    if (!paths.length) { toast("请先勾选要清理的文件（每组至少保留一份）", "warn"); return; }
    const bytes = paths.reduce((s, p) => s + (sizeMap[p] || 0), 0);
    openModal(
      "确认移入隔离区",
      `<p>即将把 <b>${paths.length}</b> 个文件（约 ${fmtBytes(bytes)}）移入隔离目录。</p>
       <p class="muted">文件<b>不会被删除</b>，只是移动到「.nassafe-quarantine」隔离目录，随时可以在下方一键恢复原位。同一组重复内容会自动至少保留一份。</p>`,
      `<button class="btn ghost" data-act="close">取消</button>
       <button class="btn primary" data-act="ok">确认移入隔离区</button>`,
      { ok: () => doDupQuarantine(paths) }
    );
  };
}

async function doDupQuarantine(paths) {
  dupSetStatus(`<span class="spinner"></span>正在移入隔离区（同卷移动，瞬时完成）…`);
  try {
    const r = await api(targetApi("/api/duplicates/quarantine", state.dupTarget), {
      method: "POST",
      body: JSON.stringify({ confirm: true, files: paths }),
    }, 120000);
    closeModal();
    const fail = (r.failed || []).length;
    let msg = `已隔离 ${r.quarantined} 个文件，可在隔离区随时恢复`;
    if (fail) msg += `；${fail} 个失败`;
    toast(msg, fail ? "warn" : "ok");
    dupSetStatus("");
    loadDupReport();
  } catch (e) {
    dupSetStatus("隔离失败：" + escapeHtml(e.message), "warn");
  }
}

async function loadDupQuarantine() {
  const box = $("dupQuarantine");
  let d;
  try { d = await api(targetApi("/api/duplicates/quarantine", state.dupTarget)); } catch { return; }
  if (!d.count) { box.innerHTML = ""; return; }
  const rows = (d.entries || []).map((e) => `
    <div class="pick-row dup-file">
      <span class="dup-path" title="${escapeAttr(e.original)}">🛡 ${escapeHtml(e.original)}</span>
      <span class="dup-meta">${fmtBytes(e.size)} · ${escapeHtml(String(e.time || "").slice(0, 16).replace("T", " "))}</span>
      <button class="btn ghost dup-restore" data-id="${escapeAttr(e.id)}">恢复</button>
      <button class="btn ghost dup-purge" data-id="${escapeAttr(e.id)}" data-name="${escapeAttr(e.original)}" data-size="${e.size || 0}">彻底删除</button>
    </div>`).join("");
  box.innerHTML = `
    <div class="dup-group">
      <div class="dup-group-head">
        <b>🛡 隔离区</b>
        <span class="muted">${d.count} 个文件 · 约 ${fmtBytes(d.total_bytes)} · 原文件完好保存在隔离目录，点「恢复」放回原位置，确认没问题后点「彻底删除」释放空间</span>
        <button class="btn ghost dup-purge dup-purge-all" id="dupPurgeAllBtn">清空隔离区（${d.count}）</button>
      </div>
      ${rows}
    </div>`;
  box.querySelectorAll(".dup-restore").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api(targetApi("/api/duplicates/restore", state.dupTarget), {
          method: "POST",
          body: JSON.stringify({ confirm: true, ids: [btn.dataset.id] }),
        });
        toast("已恢复到原位置", "ok");
        loadDupReport();
      } catch (e) {
        toast("恢复失败：" + e.message, "err");
        btn.disabled = false;
      }
    };
  });
  const askPurge = (ids, title, html) => {
    openModal(
      title,
      html + `<p class="muted" style="margin-top:8px">彻底删除<b>不进回收站、不可恢复</b>——如果还有一点犹豫，先点「恢复」放回原位。</p>`,
      `<button class="btn ghost" data-act="close">取消</button>
       <button class="btn primary" data-act="ok" style="color:var(--red);border-color:var(--red)">确认彻底删除</button>`,
      { ok: () => doDupPurge(ids) }
    );
  };
  box.querySelectorAll(".dup-purge:not(.dup-purge-all)").forEach((btn) => {
    btn.onclick = () => askPurge(
      [btn.dataset.id],
      "彻底删除该文件",
      `<p>将永久删除 <b>${escapeHtml(btn.dataset.name)}</b>（${fmtBytes(Number(btn.dataset.size) || 0)}）。</p>`
    );
  });
  const allBtn = $("dupPurgeAllBtn");
  if (allBtn) {
    allBtn.onclick = () => askPurge(
      (d.entries || []).map((e) => e.id),
      "清空隔离区",
      `<p>将永久删除隔离区里<b>全部 ${d.count} 个文件</b>（约 ${fmtBytes(d.total_bytes)}）。</p>`
    );
  }
}

async function doDupPurge(ids) {
  dupSetStatus(`<span class="spinner"></span>正在彻底删除…`);
  try {
    const r = await api(targetApi("/api/duplicates/purge", state.dupTarget), {
      method: "POST",
      body: JSON.stringify({ confirm: true, ids }),
    }, 120000);
    closeModal();
    const fail = (r.failed || []).length;
    let msg = `已彻底删除 ${r.purged} 个文件，释放 ${fmtBytes(r.freed_bytes || 0)}`;
    if (fail) msg += `；${fail} 个失败`;
    toast(msg, fail ? "warn" : "ok");
    dupSetStatus("");
    loadDupQuarantine();
  } catch (e) {
    dupSetStatus("删除失败：" + escapeHtml(e.message), "warn");
  }
}

// 重复文件扫描路径选择：单选（与监控路径多选选择器不同）
async function openDupPicker() {
  let roots = [];
  // 1) 优先用 /api/volumes（含远程设备的存储单元，mountpoint/name 更准）
  try {
    const vl = await api(targetApi("/api/volumes", state.dupTarget));
    if (vl.ok && vl.volumes && vl.volumes.length) {
      roots = vl.volumes
        .filter((v) => v && (v.mountpoint || v.mount))
        .map((v) => ({
          mount: v.mountpoint || v.mount,
          name: v.name || (v.mountpoint || v.mount).split(/[\\/]/).pop() || (v.mountpoint || v.mount),
        }));
    }
  } catch (e) { /* 忽略 */ }
  // 2) /api/volumes 拿不到再用 metrics.volumes 兜底
  if (!roots.length) {
    try {
      const mm = await api(targetApi("/api/system/metrics", state.dupTarget));
      if (mm.ok && mm.metrics.volumes) {
        roots = mm.metrics.volumes.map((v) => {
          const known = state.volumes.find((x) => x.mountpoint === v.mount);
          return {
            mount: v.mount,
            name: v.name || (known ? known.name : (v.mount.split(/[\\/]/).pop() || v.mount)),
          };
        });
      }
    } catch (e) { /* 忽略 */ }
  }
  // 3) 最后 fallback 到本机 state.volumes（仅兼容旧路径）
  if (!roots.length) {
    roots = state.volumes
      .filter((v) => (v.mountpoint || "").startsWith("/"))
      .map((v) => ({ mount: v.mountpoint, name: v.name }));
  }

  const volRow = (v) => `
    <div class="pick-vol">
      <label class="pick-row">
        <input type="radio" name="dupPick" data-path="${escapeAttr(v.mount)}" ${$("dupRoot").value.trim() === v.mount ? "checked" : ""}>
        <b>${escapeHtml(v.name)}</b>
        <span class="muted">${escapeHtml(v.mount)}</span>
      </label>
      <button class="btn ghost pick-expand" data-path="${escapeHtml(v.mount)}">展开子目录 ▾</button>
      <div class="pick-children" hidden></div>
    </div>`;

  openModal(
    "选择要扫描的目录",
    `<p class="muted" style="margin-top:0">选整个存储卷或其中某个文件夹（单选）。建议直接扫 media / download 这类容易囤重复文件的目录。</p>
     ${roots.map(volRow).join("") || `<p class="muted">未发现存储卷</p>`}`,
    `<button class="btn ghost" data-act="close">取消</button>
     <button class="btn primary" data-act="ok">确定</button>`,
    {
      ok: () => {
        const sel = document.querySelector("#modalBody input[name=dupPick]:checked");
        if (sel) $("dupRoot").value = sel.dataset.path;
        closeModal();
      },
    }
  );

  $("modalBody").onclick = async (ev) => {
    const btn = ev.target.closest(".pick-expand");
    if (!btn) return;
    const wrap = btn.parentElement.querySelector(".pick-children");
    if (!wrap.hidden) { wrap.hidden = true; btn.textContent = "展开子目录 ▾"; return; }
    if (!wrap.dataset.loaded) {
      btn.textContent = "读取中…";
      try {
        const base = btn.dataset.path.replace(/\/+$/, "");
        const data = await api(targetApi(`/api/list_dir?path=${encodeURIComponent(base)}`, state.dupTarget));
        wrap.innerHTML = (data.dirs || []).map((d) => {
          const sep = base.includes("\\") ? "\\" : "/";
          const p = base + sep + d;
          return `<label class="pick-row sub">
            <input type="radio" name="dupPick" data-path="${escapeAttr(p)}" ${$("dupRoot").value.trim() === p ? "checked" : ""}>
            📁 ${escapeHtml(d)}
          </label>`;
        }).join("") || `<p class="muted">没有子目录</p>`;
        wrap.dataset.loaded = "1";
      } catch (e) {
        wrap.innerHTML = `<p class="muted">读取失败：${escapeHtml(e.message)}</p>`;
      }
    }
    wrap.hidden = false;
    btn.textContent = "收起 ▴";
  };
}

// ---- 重复文件清理：事件绑定与初始加载 ----
$("dupPickBtn").onclick = openDupPicker;
$("dupScanBtn").onclick = startDupScan;
if ((localStorage.getItem("nassafe_view") || "home") === "dups") loadDupReport();

// ---- 磁盘垃圾清理：扫描 / 报告 / 按类清理 ----
let junkPollTimer = null;
let junkCleanSeenAt = null; // 已处理过的清理完成时间戳（防止重复触发刷新）

function junkSetStatus(html, kind) {
  const st = $("junkStatus");
  if (!html) { st.style.display = "none"; return; }
  st.style.display = "block";
  st.className = "notice" + (kind ? " " + kind : "");
  st.innerHTML = html;
}

async function startJunkScan() {
  // Windows 电脑不支持磁盘垃圾清理（后端也会拦截），提前给出清晰提示，避免误以为出错。
  // 仅本机是 Windows 时拦截；联机设备走代理，目标若是 Linux NAS 则可正常清理。
  if (state.junkTarget === "local" && state.system && /windows/i.test(state.system.os_name || "")) {
    junkSetStatus("磁盘垃圾清理当前仅支持 QNAP/QTS 等 Linux NAS；Windows 电脑暂不支持此功能（重复文件清理可在 Windows 正常使用）。", "warn");
    toast("磁盘清理暂不支持 Windows 电脑", "warn");
    return;
  }
  const btn = $("junkScanBtn");
  btn.disabled = true;
  try {
    await api(targetApi("/api/junk/scan", state.junkTarget), { method: "POST", body: "{}" });
    toast("垃圾扫描已开始…", "ok");
    pollJunkStatus();
  } catch (e) {
    toast("启动扫描失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

function pollJunkStatus() {
  clearInterval(junkPollTimer);
  junkPollTimer = setInterval(refreshJunkStatus, 2000);
  refreshJunkStatus();
}

async function refreshJunkStatus() {
  let st;
  try { st = await api(targetApi("/api/junk/status", state.junkTarget)); } catch { return; }
  const catName = { recycle: "回收站", thumbs: "缩略图缓存", logs: "旧日志", docker: "Docker" };
  const cl = st.clean || { status: "idle" };
  if (st.status === "scanning") {
    const phaseName = catName[st.phase] || "";
    junkSetStatus(`<span class="spinner"></span>正在扫描${phaseName ? "（" + phaseName + "）" : "…"}（只读，不改动数据）`);
    return;
  }
  if (cl.status === "running") {
    const pct = cl.total ? Math.round((cl.done / cl.total) * 100) : 0;
    const cat = catName[cl.category] || cl.category || "";
    junkSetStatus(`<span class="spinner"></span>正在清理（${cat}）… 已完成 ${cl.done}/${cl.total} 项（${pct}%）。大目录删除需要几分钟，页面可先做别的，完成后自动刷新`);
    return;
  }
  if (junkPollTimer) { clearInterval(junkPollTimer); junkPollTimer = null; }
  if (st.status === "error") junkSetStatus("扫描失败：" + escapeHtml(st.error || "未知"), "warn");
  else if (cl.status === "error") junkSetStatus("清理失败：" + escapeHtml(cl.error || "未知"), "warn");
  else if (cl.status === "done") {
    if (!junkCleanSeenAt && cl.finished_at) { junkCleanSeenAt = cl.finished_at; }
    else if (cl.finished_at && cl.finished_at !== junkCleanSeenAt) {
      junkCleanSeenAt = cl.finished_at;
      const freed = cl.freed_bytes || 0;
      toast(`清理完成${cl.cleaned ? " " + cl.cleaned + " 项" : ""}${freed ? "，释放 " + fmtBytes(freed) : ""}`, "ok");
      junkSetStatus("");
      startJunkScan(); // 清理后自动重新扫描，报告数字立即变新
    }
  }
  else if (st.status === "done") junkSetStatus("");
}

async function loadJunkReport() {
  refreshJunkStatus();
  let d;
  try { d = await api(targetApi("/api/junk/report", state.junkTarget)); } catch { return; }
  renderJunkReport(d);
}

function renderJunkReport(d) {
  const box = $("junkResults");
  if (d.empty || !d.categories) {
    box.innerHTML = `<p class="muted">还没有扫描报告 —— 点「开始扫描」清点可回收空间。</p>`;
    return;
  }
  const head = `
    <div class="dup-summary">
      <b>扫描完成</b>
      <span class="muted">${escapeHtml(String(d.scanned_at).slice(0, 16).replace("T", " "))} · 各类垃圾单独确认后才清理</span>
      ${junkCleanableAll(d).length > 1 ? `<button class="btn primary junk-clean-all">🧹 一键清理全部</button>` : ""}
    </div>`;

  const ICONS = { recycle: "🗑", thumbs: "🖼", logs: "📄", docker: "📦" };
  const cards = d.categories.map((cat) => {
    const count = cat.items.length;
    const empty = !cat.total_bytes;
    const rows = cat.items.slice(0, 12).map((i) => {
      const size = i.kb != null ? fmtBytes(i.kb * 1024) : fmtBytes(i.size || 0);
      return `<div class="junk-item"><span class="dup-path">${escapeHtml(i.path)}</span><span class="dup-meta">${size}</span></div>`;
    }).join("");
    const more = count > 12 ? `<div class="muted junk-more">…还有 ${count - 12} 项</div>` : "";
    const dockerBlock = cat.key === "docker" ? renderJunkDocker(d.docker) : "";
    return `
      <div class="dup-group junk-card${empty ? " junk-empty" : ""}">
        <div class="dup-group-head">
          <b>${ICONS[cat.key] || "•"} ${escapeHtml(cat.name)}</b>
          <span class="muted">${count ? count + " 项 · " : ""}${empty ? "没有可清理的" : fmtBytes(cat.total_bytes) + " 可回收"}</span>
          <button class="btn ghost junk-clean" data-cat="${escapeAttr(cat.key)}" ${empty ? "disabled" : ""}>清理此类</button>
        </div>
        ${dockerBlock || rows + more}
        <div class="junk-hint muted">${escapeHtml(cat.hint || "")}</div>
      </div>`;
  }).join("");

  box.innerHTML = head + `<div class="dup-groups">${cards}</div>`;

  box.querySelectorAll(".junk-clean").forEach((btn) => {
    btn.onclick = () => {
      const cat = d.categories.find((c) => c.key === btn.dataset.cat);
      if (!cat) return;
      const extra = cat.key === "docker"
        ? `清理方式：清除<b>悬空镜像</b>与<b>构建缓存</b>（docker image/builder prune），<b>不会触碰任何在运行的容器和正在使用的镜像</b>。`
        : cat.key === "thumbs"
          ? `这只是系统自动生成的<b>缓存</b>，删掉后下次看图片会自动重新生成，<b>不会动你的照片和视频本身</b>。`
          : cat.key === "logs"
            ? `只删除 <b>30 天前</b>的轮转/压缩日志，活动日志一律不碰。`
            : `回收站里的文件本来就是已删除状态，清空后彻底释放。`;
      openModal(
        "确认清理：" + cat.name,
        `<p>将清理 <b>${cat.items.length}</b> 项，约 <b>${fmtBytes(cat.total_bytes)}</b>。</p>
         <p class="muted">${extra}</p>
         <p class="muted">此操作<b>不可恢复</b>（缩略图会自动重建，不在此列）。</p>`,
        `<button class="btn ghost" data-act="close">取消</button>
         <button class="btn primary" data-act="ok" style="color:var(--red);border-color:var(--red)">确认清理</button>`,
        { ok: () => doJunkClean(cat.key) }
      );
    };
  });

  const cleanAllBtn = box.querySelector(".junk-clean-all");
  if (cleanAllBtn) cleanAllBtn.onclick = () => openJunkCleanAllModal(d);
}

// 「一键清理全部」：统计当前有东西可清的类别（Docker 只在有悬空镜像时算）
function junkCleanableAll(d) {
  const cats = (d.categories || []).filter((c) => c.key !== "docker" && c.items && c.items.length);
  const hasDocker = d.docker && (d.docker.dangling_images || []).length > 0;
  return [...cats, ...(hasDocker ? [{ key: "docker" }] : [])];
}

function openJunkCleanAllModal(d) {
  const ICONS = { recycle: "🗑", thumbs: "🖼", logs: "📄", docker: "📦" };
  const allCats = junkCleanableAll(d);
  const names = { recycle: "回收站", thumbs: "缩略图缓存", logs: "旧日志", docker: "Docker 可回收空间" };
  const rows = allCats.map((c) => {
    if (c.key === "docker") {
      const n = (d.docker.dangling_images || []).length;
      return `<li>${ICONS.docker} Docker 悬空镜像 ${n} 个 + 构建缓存（不动在用容器）</li>`;
    }
    return `<li>${ICONS[c.key] || "•"} ${escapeHtml(names[c.key] || c.key)}：${c.items.length} 项 · 约 <b>${fmtBytes(c.total_bytes)}</b></li>`;
  }).join("");
  openModal(
    "一键清理全部",
    `<p style="margin-top:0">将按顺序依次清理：</p><ul>${rows}</ul>
     <p class="muted">回收站内容多时可能需要<b>几分钟到几十分钟</b>，进度会实时显示，期间可正常使用其他页面，完成后自动重新扫描。</p>
     <p class="muted">此操作<b>不可恢复</b>（缩略图会自动重建，不在此列）。</p>`,
    `<button class="btn ghost" data-act="close">取消</button>
     <button class="btn primary" data-act="ok" style="color:var(--red);border-color:var(--red)">全部清理</button>`,
    { ok: () => doJunkClean(allCats.map((c) => c.key)) }
  );
}

function renderJunkDocker(docker) {
  if (!docker || !docker.df || !docker.df.length) return "";
  const dfRows = docker.df.map((r) => `
    <div class="junk-item"><span class="dup-path">Docker ${escapeHtml(r.type)}</span>
    <span class="dup-meta">${escapeHtml(r.size)} · 可回收 ${escapeHtml(r.reclaimable)}</span></div>`).join("");
  const dang = (docker.dangling_images || []);
  const dangNote = dang.length
    ? `<div class="muted junk-more">悬空镜像 ${dang.length} 个（如 ${escapeHtml(dang.slice(0, 3).map((x) => x.id).join("、"))}…）</div>`
    : `<div class="muted junk-more">没有悬空镜像</div>`;
  return dfRows + dangNote;
}

async function doJunkClean(category) {
  // 清理已改为后台任务：接口立即返回，进度走 2s 轮询（refreshJunkStatus）
  // category 可传单个字符串或数组（数组=一键清理全部）
  const categories = Array.isArray(category) ? category : [category];
  try {
    const r = await api(targetApi("/api/junk/clean", state.junkTarget), {
      method: "POST",
      body: JSON.stringify({ confirm: true, categories }),
    });
    closeModal();
    if (!r.ok) {
      junkSetStatus("清理启动失败：" + escapeHtml(r.error || "未知"), "warn");
      return;
    }
    junkCleanSeenAt = null; // 新一轮清理，完成时允许触发一次刷新
    toast(categories.length > 1
      ? `一键清理已在后台开始（${categories.length} 类，共 ${r.total} 项）…`
      : r.total ? `清理已在后台开始（${r.total} 项）…` : "清理已在后台开始…", "ok");
    pollJunkStatus();
  } catch (e) {
    junkSetStatus("清理启动失败：" + escapeHtml(e.message), "warn");
  }
}

// ---- 磁盘垃圾清理：事件绑定 ----
$("junkScanBtn").onclick = startJunkScan;
if ((localStorage.getItem("nassafe_view") || "home") === "junk") loadJunkReport();
