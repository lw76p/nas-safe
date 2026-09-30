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
  autoMonitor: false, // 自动持续监控开关
  autoMonitorTimer: null,
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

  const guard = $("ovGuard");
  if (!units) {
    guard.textContent = "—"; guard.className = "ov-num";
  } else if (unprotected) {
    guard.textContent = `${unprotected} 个待保护`;
    guard.className = "ov-num is-bad";
  } else {
    guard.textContent = "健康";
    guard.className = "ov-num is-ok";
  }

  const mon = $("ovMonitor");
  if (state.autoMonitor) {
    mon.textContent = "运行中";
    mon.className = "ov-num is-ok";
  } else {
    mon.textContent = "未开启";
    mon.className = "ov-num";
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
  if (kb >= 1048576) return (kb / 1048576).toFixed(1) + " TB";
  if (kb >= 1024) return (kb / 1024).toFixed(0) + " GB";
  return kb + " KB";
}
function donut(label, percent, color) {
  const p = Math.max(0, Math.min(100, percent || 0));
  const r = 34, c = 2 * Math.PI * r;
  return `
    <div class="donut">
      <svg viewBox="0 0 84 84" width="84" height="84">
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

function renderMetrics(m) {
  const body = $("metricsBody");
  const cap = m.capabilities || {};

  // 状态评级：0=正常(绿) 1=注意(琥珀) 2=异常(红)。每张卡独立评级。
  const grade = (v, warn, bad) => (v == null ? 0 : v >= bad ? 2 : v >= warn ? 1 : 0);
  const GRADE = { 0: ["ok", "正常"], 1: ["warn", "注意"], 2: ["bad", "异常"] };
  const cardHead = (title, g, extra = "") => {
    const [cls, txt] = GRADE[g];
    return `<h4><span>${title}</span><span class="mc-pill ${cls}">${txt}</span>${extra}</h4>`;
  };

  // 卡1：系统运行状况
  const up = m.uptime || {};
  const gTemp = cap.cpu_temp ? grade(m.cpu.temp_c, 80, 90) : 0;
  const gLoad = grade(m.cpu && m.cpu.load1 != null ? m.cpu.load1 : null, 8, 16);
  const g1 = Math.max(gTemp, gLoad);
  const tempBad = gTemp === 2;
  const worst1 = gTemp >= gLoad
    ? (gTemp ? (gTemp === 2 ? "CPU 温度过高" : "CPU 温度偏高") : "")
    : (gLoad ? "系统负载过高" : "");
  let card1 = `
    <div class="metric-card mc-${GRADE[g1][0]}">
      ${cardHead("系统运行状况", g1)}
      <div class="metric-row"><span class="status-dot ${GRADE[g1][0] === "bad" ? "bad" : GRADE[g1][0] === "warn" ? "warn" : "ok"}"></span>
        <b>${escapeHtml(m.hostname || "NAS")}</b>
        ${worst1 ? `<span class="mc-reason">${worst1}</span>` : ""}</div>
      <div class="metric-kv"><span>运行时间</span><b>${up.days || 0} 天 ${up.hours || 0} 小时 ${up.minutes || 0} 分</b></div>
      ${cap.cpu_temp ? `<div class="metric-kv"><span>CPU 温度</span><b class="${gTemp === 2 ? "t-warn" : gTemp === 1 ? "t-warn" : "t-ok"}">${m.cpu.temp_c}°C</b></div>` : ""}
      ${m.cpu && m.cpu.load1 != null ? `<div class="metric-kv"><span>负载</span><b>${m.cpu.load1}</b></div>` : ""}
      ${cap.fan && m.fan ? (m.fan.cpu_fan_rpm ? `<div class="metric-kv"><span>CPU 风扇</span><b>${m.fan.cpu_fan_rpm} RPM</b></div>` : "") + (m.fan.fan_rpm ? `<div class="metric-kv"><span>机箱风扇</span><b>${m.fan.fan_rpm} RPM</b></div>` : "") : ""}
    </div>`;

  // 卡2：资源监控（网速行首轮用 -- 占位，避免卡片出现/消失引起跳动）
  const gCpu = cap.cpu_percent ? grade(m.cpu.percent, 80, 95) : 0;
  const gMem = cap.mem ? grade(m.mem.percent, 80, 90) : 0;
  const g2 = Math.max(gCpu, gMem);
  const hasNet = m.net && m.net.ifaces && m.net.ifaces.length;
  const fastest = hasNet
    ? m.net.ifaces.reduce((a, b) => ((b.rx_bps || 0) + (b.tx_bps || 0) > (a.rx_bps || 0) + (a.tx_bps || 0) ? b : a))
    : null;
  let card2 = `
    <div class="metric-card mc-${GRADE[g2][0]}">
      ${cardHead("资源监控", g2)}
      <div class="donut-row">
        ${cap.cpu_percent ? donut("CPU", m.cpu.percent, gCpu === 2 ? "var(--red)" : gCpu === 1 ? "var(--amber)" : "var(--z-storage)") : ""}
        ${cap.mem ? donut("RAM", m.mem.percent, gMem === 2 ? "var(--red)" : gMem === 1 ? "var(--amber)" : "var(--z-monitor)") : ""}
        ${!cap.cpu_percent && !cap.mem ? `<p class="muted">不可用</p>` : ""}
      </div>
      ${hasNet ? `<div class="metric-kv net"><span>↓ ${fmtBps(fastest.rx_bps)}</span><span>↑ ${fmtBps(fastest.tx_bps)}</span></div>
      <p class="muted" style="margin:2px 0 0">网卡 ${escapeHtml(fastest.iface)}${m.net.ifaces.length > 1 ? `（共 ${m.net.ifaces.length} 个）` : ""}</p>` : ""}
    </div>`;

  // 卡3：存储（卷用量条）
  const volRows = (m.volumes || []).map((v) => {
    const known = state.volumes.find((x) => x.mountpoint === v.mount);
    const label = known ? known.name : v.mount.split("/").pop() || v.mount;
    const pct = Math.max(0, Math.min(100, v.percent));
    return `
      <div class="vol-meter">
        <div class="vol-meter-head"><span>${escapeHtml(label)}</span>
          <span class="muted">${fmtKB(v.used_kb)} / ${fmtKB(v.total_kb)}（${pct}%）</span></div>
        <div class="meter"><i style="width:${pct}%" class="${pct >= 90 ? "danger" : pct >= 75 ? "warn" : ""}"></i></div>
      </div>`;
  }).join("");
  const g3 = (m.volumes || []).reduce((g, v) => Math.max(g, grade(v.percent, 75, 90)), 0);
  const card3 = (m.volumes || []).length ? `
    <div class="metric-card mc-${GRADE[g3][0]}">
      ${cardHead("存储", g3)}
      ${volRows}
    </div>` : "";

  // 卡4：磁盘槽位图 —— 按类型分组（nvme=M.2，sdX=SATA/HDD），显示容量/温度/实时读写
  const gDisk = (d) => d.temp_c == null ? 0 : grade(d.temp_c, 50, 60);
  const g4 = (m.disks || []).reduce((g, d) => Math.max(g, gDisk(d)), 0);
  const ioTxt = (d) => {
    if (d.read_bps == null && d.write_bps == null) return "";
    return `<span class="chip-io">↓${fmtBps(d.read_bps)} ↑${fmtBps(d.write_bps)}</span>`;
  };
  const chip = (d, label) => {
    const tb = (d.size_b / 1024**4).toFixed(1);
    const gd = gDisk(d);
    const temp = d.temp_c != null ? `<span class="chip-temp ${GRADE[gd][0]}">${d.temp_c}°C</span>` : "";
    return `
      <div class="disk-chip" title="${escapeHtml(d.model)} ${tb}TB">
        <b>${label}</b>
        <span>${tb}TB</span>
        ${temp}
        ${ioTxt(d)}
      </div>`;
  };
  const nvme = (m.disks || []).filter((d) => d.name.startsWith("nvme"));
  const sata = (m.disks || []).filter((d) => !d.name.startsWith("nvme"));
  let diskGroups = "";
  if (nvme.length) {
    diskGroups += `<div class="bay-group"><span class="bay-label">M.2</span>${
      nvme.map((d, i) => chip(d, `SSD ${i + 1}`)).join("")}</div>`;
  }
  if (sata.length) {
    diskGroups += `<div class="bay-group"><span class="bay-label">3.5"/SATA</span>${
      sata.map((d, i) => chip(d, `HDD ${i + 1}`)).join("")}</div>`;
  }
  const card4 = (m.disks || []).length ? `
    <div class="metric-card mc-${GRADE[g4][0]}">
      ${cardHead("磁盘", g4, `<span class="muted">（${m.disks.length} 块）</span>`)}
      ${diskGroups}
    </div>` : "";

  body.innerHTML = card1 + card2 + card3 + card4;
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
          name: known ? known.name : (v.mount.split("/").pop() || v.mount),
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
          const p = base + "/" + d;
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

/* ------------------------- 本地 AI 自动发现 ------------------------- */

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
  btn.textContent = "搜索中（约 5-10 秒）…";
  try {
    const data = await api("/api/ai/discover", { method: "POST", body: "{}" });
    const found = data.found || [];
    if (!found.length) {
      toast("没有找到本地 AI 服务。确认 Ollama 已安装（可用 DeployEasy 一键部署），且监听 0.0.0.0", "err");
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



// 顶栏标签页：总览 / 快照时间轴 / 实时监控 / 设置；风险横幅全局常驻。
const VIEWS = ["home", "snapshots", "monitor", "settings"];

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

function showView(name) {
  if (!VIEWS.includes(name)) name = "home";
  localStorage.setItem("nassafe_view", name);
  applyView();
  // 直接点进时间轴页但还没选过卷：恢复上次选的卷，没记录就自动选第一个
  if (name === "snapshots" && !state.activeVolume) autoSelectVolume();
  // 回到主页时立即刷新仪表盘
  if (name === "home") loadMetrics();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function autoSelectVolume() {
  let vol = null;
  const saved = localStorage.getItem("nassafe_volume");
  if (saved) vol = state.volumes.find((v) => v.mountpoint === saved || String(v.id) === saved);
  if (!vol && state.volumes.length) vol = state.volumes[0];
  if (vol) await selectVolume(vol, true);
}

/* ------------------------- 时间轴 ------------------------- */

async function selectVolume(vol, keepSnapshot) {
  state.activeVolume = vol;
  localStorage.setItem("nassafe_volume", vol.mountpoint ?? String(vol.id));
  $("tlTitle").textContent = `快照时间轴 — ${vol.name}`;
  $("tlSubtitle").textContent = vol.mountpoint;
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
    btn.textContent = "深度校验快照 (v2)";
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
    btn.textContent = "扫描勒索行为 (v3)";
  }
}

/* ------------------------- 自动持续监控 ------------------------- */

// 开启后按选定间隔自动跑 v2 深度校验（默认每 5 分钟）；可选附带 v3 勒索行为扫描。
// 自动模式下静默成功提示（只更新结果区与顶栏横幅），避免每 5 分钟弹一次 toast 打扰用户；
// 失败仍提示，便于第一时间发现监控链路异常。
function startAutoMonitor() {
  stopAutoMonitor();
  const ms = parseInt($("autoInterval").value, 10) || 300000;
  runIntegrityCheck(true);
  if ($("autoBehavior").checked) runBehaviorScan(true);
  state.autoMonitorTimer = setInterval(() => {
    runIntegrityCheck(true);
    if ($("autoBehavior").checked) runBehaviorScan(true);
  }, ms);
  state.autoMonitor = true;
}

function stopAutoMonitor() {
  if (state.autoMonitorTimer) {
    clearInterval(state.autoMonitorTimer);
    state.autoMonitorTimer = null;
  }
  state.autoMonitor = false;
}

// 刷新页面后按 localStorage 恢复自动监控状态（关闭页面不会丢失监控中状态）
function restoreAutoMonitor() {
  const on = localStorage.getItem("nassafe.autoMonitor") === "1";
  const iv = localStorage.getItem("nassafe.autoInterval");
  const beh = localStorage.getItem("nassafe.autoBehavior") === "1";
  if (iv) $("autoInterval").value = iv;
  $("autoBehavior").checked = beh;
  if (on) {
    $("autoMonitor").checked = true;
    startAutoMonitor();
  }
  updateOverview();
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

/* ------------------------- 通知与 AI 设置 ------------------------- */

// 通知通道字段模板：按类型动态生成表单。
let notifyDraft = {};

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
  webhook: [{ key: "url", label: "Webhook URL" }],
  bark: [
    { key: "key", label: "Bark Key / 完整 URL" },
    { key: "base", label: "服务地址（默认 https://api.day.app）" },
  ],
  ntfy: [
    { key: "topic", label: "ntfy Topic" },
    { key: "base", label: "服务地址（默认 https://ntfy.sh）" },
  ],
  email: [
    { key: "host", label: "SMTP 主机" },
    { key: "port", label: "端口（默认 465）" },
    { key: "user", label: "账号" },
    { key: "pass", label: "密码", secret: true },
    { key: "to", label: "收件人（默认同账号）" },
  ],
};

function renderNotifyFields(type) {
  const fields = NOTIFY_FIELDS[type] || [];
  $("notifyFields").innerHTML = fields
    .map(
      (f) => {
        if (f.multiline) {
          return `
      <div class="set-row">
        <span class="set-label">${f.label}</span>
        <textarea id="nf_${f.key}" class="text-input" rows="3"
          placeholder="例如 a@qq.com, b@163.com">${escapeHtml(notifyDraft[f.key] || "")}</textarea>
      </div>`;
        }
        return `
      <div class="set-row">
        <span class="set-label">${f.label}</span>
        <input id="nf_${f.key}" class="text-input"
          type="${f.secret ? "password" : "text"}"
          value="${escapeAttr(notifyDraft[f.key] || "")}"
          placeholder="${f.secret ? "敏感信息，仅保存在本地" : ""}">
      </div>`;
      }
    )
    .join("");
  // 中继就绪提示
  const hint = $("relayHint");
  if (type === "relay" && hint) {
    hint.style.display = "block";
    hint.textContent = window.__relayAvailable
      ? "✅ 厂商邮件中继已就绪：填邮箱即可接收报警，无需任何 Key。"
      : "⚠️ 厂商邮件中继尚未配置（需厂商设置 NASSAFE_RELAY_APIKEY）。可改用下方高级通道，或联系厂商开通。";
  } else if (hint) {
    hint.style.display = "none";
  }
}

function gatherNotifyChannel() {
  const type = $("notifyType").value;
  const ch = { type };
  for (const f of NOTIFY_FIELDS[type] || []) {
    const v = ($("nf_" + f.key).value || "").trim();
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

async function testNotify() {
  const ch = gatherNotifyChannel();
  try {
    const data = await api("/api/notify/test", { method: "POST", body: JSON.stringify({ channel: ch }) });
    if (data.ok) toast("测试消息已发送，请查看接收端", "ok");
    else toast("发送失败：" + (data.msg || data.error || "未知"), "err");
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
  const hint = $("aiHint");
  if (isOllama) {
    hint.innerHTML =
      "支持 Ollama / LM Studio / llama.cpp / vLLM 等本地模型服务，数据不出 NAS。" +
      "还没装？用 <b>DeployEasy 一键部署 Ollama</b>（最简单），或点下方「自动搜索」自动发现并配置；" +
      "手动配置：服务地址填 <code>http://NAS的IP:11434/v1</code> —— " +
      "注意别填 localhost：NAS Safe 跑在 Docker 里，容器内的 localhost 不是 NAS 本机。";
  } else {
    hint.textContent = "去对应平台申请一个 API Key 粘贴到上面即可（DeepSeek 最便宜，国内直连）。数据将发送给该云端供应商。";
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
  // 空值或脱敏占位 *** 都不传 api_key，由后端保留旧 Key
  if (keyVal && keyVal !== "***") cfg.api_key = keyVal;
  try {
    const data = await api("/api/ai/config", { method: "POST", body: JSON.stringify(cfg) });
    toast(
      data.ready ? "AI 设置已保存，可用" : "AI 设置已保存（未配置密钥，相关功能将隐藏）",
      "ok"
    );
  } catch (e) {
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
      // 默认推荐：厂商邮件中继（填邮箱即用），零配置入门
      $("notifyType").value = "relay";
    }
    renderNotifyFields($("notifyType").value);
  } catch (e) { /* 忽略 */ }

  try {
    const ac = await api("/api/ai/config");
    const cfg = ac.config || {};
    $("aiEnabled").checked = !!cfg.enabled;
    if (cfg.provider) $("aiProvider").value = cfg.provider;
    if (cfg.base_url) $("aiBase").value = cfg.base_url;
    if (cfg.model) $("aiModel").value = cfg.model;
    // api_key 脱敏为 ***，不回填
  } catch (e) { /* 忽略 */ }
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
    const data = await api("/api/ai/interpret", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    openModal(
      "AI 解读报告",
      `<div style="white-space:pre-wrap;line-height:1.75;font-size:13.5px;color:var(--text)">${escapeHtml(data.text)}</div>`,
      `<button class="btn ghost" data-act="close">关闭</button>`,
      {}
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

$("integrityBtn").onclick = runIntegrityCheck;
$("behaviorBtn").onclick = runBehaviorScan;
$("aiInterpretBtn").onclick = aiInterpret;

$("notifyType").onchange = () => renderNotifyFields($("notifyType").value);
$("notifySaveBtn").onclick = saveNotify;
$("notifyTestBtn").onclick = testNotify;
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
  if (e.key === "Escape" && !$("modalRoot").hidden) closeModal();
});

boot();

// 自动监控状态恢复：刷新页面后按上次设置恢复（localStorage 持久化）
restoreAutoMonitor();

// 加载已保存的通知 / AI 设置到面板
loadSettings();
// 按当前供应商显示 AI 引导提示（Ollama 附安装指引）
aiProviderChanged();

// 主导航：绑定标签页点击并恢复上次所在视图（localStorage 持久化）
document.querySelectorAll("#mainTabs .tab").forEach((t) => {
  t.onclick = () => showView(t.dataset.view);
});
applyView();

// 系统仪表盘：手动刷新 + 15s 自动轮询（仅主页可见时取数，SSH 采集有 15s 缓存）
$("metricsRefreshBtn").onclick = () => loadMetrics(true);
$("pickPathsBtn").onclick = openPathPicker;
$("aiDiscoverBtn").onclick = aiDiscover;
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
