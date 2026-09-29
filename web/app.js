/* NAS Safe — 前端逻辑 */

const $ = (id) => document.getElementById(id);

const state = {
  system: null,
  volumes: [],
  activeVolume: null,
  snapshots: [],
  browseSnapshot: null,
  browsePath: null,
  browseStack: [],
  restoreDir: localStorage.getItem("nassafe.restoreDir") || "",
  tamperAlerts: [],   // v1 基线对比法（30s 轮询）
  deepAlerts: [],     // v2 内容完整性 + v3 勒索行为（主动巡检后写入）
};

// 快照是否为威联通（QNAP）远程后端：这类快照没有本地实体路径，
// 浏览/取回必须走 snapshot_id + volume_id 通道。
function isQnapSnap(snap) {
  return snap && (snap.backend === "qnap" || snap.fs_type === "qnap");
}

/* ------------------------- 网络 ------------------------- */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error(`服务返回了非法响应 (HTTP ${res.status})`);
  }
  if (!data.ok) throw new Error(data.error || "未知错误");
  return data;
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
    const sys = await api("/api/system");
    state.system = sys.system;
    const s = sys.system;
    $("sysLine").textContent =
      `${s.os_name} · 内核 ${s.kernel}` +
      (s.is_container ? " · 容器内运行" : "") +
      ` · 可用文件系统：${s.fs_available.join(", ") || "无"}`;

    setStatus("已连接", "ok");

    if (s.warnings && s.warnings.length) {
      showBanner("warn", "使用提示", s.warnings.join(" "));
    }

    await loadVolumes();
  } catch (err) {
    setStatus("连接失败", "err");
    showBanner("error", "无法连接到 NAS Safe 服务", err.message);
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

  const data = await api("/api/volumes");
  state.volumes = data.volumes;

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
}

/* ------------------------- 时间轴 ------------------------- */

async function selectVolume(vol, keepSnapshot) {
  state.activeVolume = vol;
  $("timelinePanel").hidden = false;
  $("monitorPanel").hidden = false;
  $("tlTitle").textContent = `快照时间轴 — ${vol.name}`;
  $("tlSubtitle").textContent = vol.mountpoint;
  $("browseBtn").disabled = true;
  // 默认把当前卷的挂载点填入勒索行为监控路径
  if ($("watchPaths").value.trim() === "") {
    $("watchPaths").value = vol.mountpoint;
  }

  document.querySelectorAll(".volume-card").forEach((c) => c.classList.remove("active"));
  await loadSnapshots();
}

async function loadSnapshots() {
  const vol = state.activeVolume;
  if (!vol) return;

  const tl = $("timeline");
  tl.innerHTML = `<p class="muted"><span class="spinner"></span>读取快照列表…</p>`;

  try {
    const data = await api(`/api/snapshots?volume=${encodeURIComponent(vol.mountpoint)}`);
    state.snapshots = data.snapshots;

    if (!state.snapshots.length) {
      tl.innerHTML = `<p class="muted">
        该存储单元还没有快照。点击右上角「立即拍一张快照」开始保护。
      </p>`;
      return;
    }

    // 按时间升序排列，最新的在右边
    const sorted = [...state.snapshots].sort((a, b) => {
      const ta = a.created_at || a.name;
      const tb = b.created_at || b.name;
      return String(ta).localeCompare(String(tb));
    });

    tl.innerHTML = "";
    sorted.forEach((snap, idx) => {
      const isLatest = idx === sorted.length - 1;
      const node = document.createElement("div");
      node.className = "tl-node" + (isLatest ? " latest" : "");
      node.innerHTML = `
        <div class="tl-label">${formatShort(snap.created_at || snap.name)}</div>
        <div class="tl-dot"></div>
        <div class="tl-size">${snap.size_human || ""}${snap.protected ? ' <span class="lock" title="受 NAS Safe 保护">🔒</span>' : ""}</div>
      `;
      node.onclick = () => openSnapshotDetail(snap);
      tl.appendChild(node);
    });
  } catch (err) {
    tl.innerHTML = `<p class="muted">读取失败：${escapeHtml(err.message)}</p>`;
  }
}

/* ------------------------- 快照详情 ------------------------- */

function openSnapshotDetail(snap) {
  const qnap = isQnapSnap(snap);
  const pathLabel = qnap
    ? (snap.mount_path || `卷#${snap.volume_id} / 快照#${snap.snapshot_id}`)
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
    <div class="notice">
      这份快照是只读的，勒索软件无法修改其中的数据。<br>
      建议使用「浏览文件」取回单个文件，这是最安全的恢复方式 —— 不会影响你当前的数据。
    </div>
  `;

  const canBrowse = qnap || (snap.path && snap.path.startsWith("/"));
  const foot = `
    <button class="btn ghost" data-act="close">关闭</button>
    <button class="btn primary" data-act="browse" ${canBrowse ? "" : "disabled"}>
      ${canBrowse ? "浏览并取回文件" : "该系统不支持直接浏览"}
    </button>
  `;

  openModal(`快照详情`, body, foot, {
    browse: () => {
      closeModal();
      // QNAP 快照无本地实体路径，从快照根（subpath 为空）开始浏览
      openBrowser(snap, qnap ? "" : snap.path);
    },
  });
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
        <span class="file-size">${e.is_dir ? "" : escapeHtml(e.size_human || "")}</span>
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
      该目录位于「运行 NAS Safe 的这台机器」上：若本服务装在 NAS 本机，就是 NAS 共享目录；
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

function openModal(title, body, foot, actions) {
  modalActions = actions || {};
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = body;
  $("modalFoot").innerHTML = foot || "";
  $("modalRoot").hidden = false;

  $("modalFoot").onclick = (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "close") closeModal();
    else if (modalActions[act]) modalActions[act]();
  };

  if (!foot) $("modalFoot").innerHTML = `<button class="btn ghost" data-act="close">关闭</button>`;
}

function closeModal() {
  $("modalRoot").hidden = true;
  $("modalBox").dataset.mode = "";
  $("modalBody").innerHTML = "";
}

/* ------------------------- 格式化工具 ------------------------- */

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
    if (wasActive) {
      $("alertBanner").hidden = true;
      state.tamperActive = false;
      state.deepActive = false;
    }
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

// 受保护快照（被 NAS Safe 锁定的）一旦消失或被解锁，后端 /api/alerts 会告警。
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
async function runIntegrityCheck() {
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
    if (results.length) {
      toast(`发现 ${results.length} 处快照内容异常`, "err");
    } else {
      toast("受保护快照内容完整，未被篡改", "ok");
    }
  } catch (err) {
    toast("校验失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "深度校验快照 (v2)";
  }
}

/* ------------------------- v3 勒索行为检测 ------------------------- */

// 扫描生产（实时）目录：扩展名突变 / 熵值骤升 / 批量改名三类信号。
// 注意：需要服务器能本地访问这些目录（btrfs/zfs 本地模式）；QNAP 远程管理模式
// 下服务器在管理机，NAS 目录未挂载，会提示路径不可达。
async function runBehaviorScan() {
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
    if (data.suspicious) {
      toast("⚠ 检测到疑似勒索行为！", "err");
    } else {
      toast("未检测到明显勒索行为", "ok");
    }
  } catch (err) {
    toast("扫描失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "扫描勒索行为 (v3)";
  }
}

/* ------------------------- 监控结果渲染 ------------------------- */

function renderMonitorResults(payload, kind) {
  const box = $("monitorResults");
  if (kind === "integrity") {
    const rs = payload || [];
    if (!rs.length) {
      box.innerHTML = `<p class="muted">✅ 所有带内容基线的受保护快照均完整，未检测到内容被篡改。</p>`;
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

$("integrityBtn").onclick = runIntegrityCheck;
$("behaviorBtn").onclick = runBehaviorScan;

$("browseBtn").onclick = () => {
  const latest = state.snapshots[state.snapshots.length - 1];
  if (latest) openBrowser(latest, isQnapSnap(latest) ? "" : latest.path);
};

$("modalClose").onclick = closeModal;
$("modalMask").onclick = closeModal;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modalRoot").hidden) closeModal();
});

boot();

// 篡改检测告警轮询：每 30s 拉一次 /api/alerts，发现异常则顶栏告警
pollAlerts();
setInterval(pollAlerts, 30000);
