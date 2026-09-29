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
};

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
  $("tlTitle").textContent = `快照时间轴 — ${vol.name}`;
  $("tlSubtitle").textContent = vol.mountpoint;
  $("browseBtn").disabled = true;

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
        <div class="tl-size">${snap.size_human || ""}</div>
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
  const body = `
    <div class="kv">
      <div class="kv-row"><span class="kv-k">快照名</span><span class="kv-v">${escapeHtml(snap.name)}</span></div>
      <div class="kv-row"><span class="kv-k">创建时间</span><span class="kv-v">${escapeHtml(snap.created_at || "未知")}</span></div>
      <div class="kv-row"><span class="kv-k">占用空间</span><span class="kv-v">${escapeHtml(snap.size_human || "计算中")}</span></div>
      <div class="kv-row"><span class="kv-k">只读保护</span><span class="kv-v">${snap.readonly ? "已启用（无法被修改）" : "未启用"}</span></div>
      <div class="kv-row"><span class="kv-k">实体路径</span><span class="kv-v">${escapeHtml(snap.path || "-")}</span></div>
    </div>
    <div class="notice">
      这份快照是只读的，勒索软件无法修改其中的数据。<br>
      建议使用「浏览文件」取回单个文件，这是最安全的恢复方式 —— 不会影响你当前的数据。
    </div>
  `;

  const canBrowse = snap.path && snap.path.startsWith("/");
  const foot = `
    <button class="btn ghost" data-act="close">关闭</button>
    <button class="btn primary" data-act="browse" ${canBrowse ? "" : "disabled"}>
      ${canBrowse ? "浏览并取回文件" : "该系统不支持直接浏览"}
    </button>
  `;

  openModal(`快照详情`, body, foot, {
    browse: () => {
      closeModal();
      openBrowser(snap, snap.path);
    },
  });
}

/* ------------------------- 文件浏览 ------------------------- */

async function openBrowser(snap, path, pushStack = true) {
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
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);

    const rows = data.entries.map((e) => `
      <div class="file-row" data-path="${escapeAttr(e.path)}" data-dir="${e.is_dir}">
        <span class="file-icon ${e.is_dir ? "dir" : ""}"></span>
        <span class="file-name">${escapeHtml(e.name)}</span>
        <span class="file-size">${e.is_dir ? "" : escapeHtml(e.size_human)}</span>
      </div>
    `).join("");

    $("modalBody").innerHTML = `
      <div class="crumb">${escapeHtml(path)}</div>
      <div id="fileList">
        ${data.parent && data.parent.includes(".nassafe") ? `<div class="file-row" data-path="${escapeAttr(data.parent)}" data-dir="true"><span class="file-icon dir"></span><span class="file-name">.. 返回上级</span><span class="file-size"></span></div>` : ""}
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
  // 相对路径 = 快照路径之后的剩余部分
  const base = snap.path.replace(/\/+$/, "");
  let rel = fullPath.startsWith(base) ? fullPath.slice(base.length) : fullPath;
  rel = rel.replace(/^\/+/, "");

  const destination = defaultRestoreDir();
  if (!destination) {
    toast("无法确定恢复目录，请检查配置", "err");
    return;
  }

  if (!confirm(`确定要把这个文件取回到：\n${destination}\n\n不会覆盖同名文件（会自动加后缀）。`)) {
    return;
  }

  try {
    toast("正在恢复…");
    const data = await api("/api/snapshot/restore", {
      method: "POST",
      body: JSON.stringify({
        snapshot_path: snap.path,
        relative_file: rel,
        destination,
        confirm: true,
      }),
    });
    toast(`已恢复：${data.restored_to}`, "ok");
  } catch (err) {
    toast("恢复失败：" + err.message, "err");
  }
}

function defaultRestoreDir() {
  // 恢复到快照所属存储单元下的 _restored 目录
  const vol = state.activeVolume;
  if (!vol) return null;
  return vol.mountpoint.replace(/\/+$/, "") + "/_restored";
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

$("browseBtn").onclick = () => {
  const latest = state.snapshots[state.snapshots.length - 1];
  if (latest) openBrowser(latest, latest.path);
};

$("modalClose").onclick = closeModal;
$("modalMask").onclick = closeModal;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modalRoot").hidden) closeModal();
});

boot();
