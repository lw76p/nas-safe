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
    // 顶栏不再外露系统细节（内核/文件系统等黑话），保持产品化文案（index.html 静态文案）

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

  // 时间轴页的卷切换下拉框：选项 = 所有存储卷
  const sel = $("tlVolumeSel");
  if (sel) {
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
  const g4 = disks.reduce((g, d) => Math.max(g, gDisk(d)), 0);
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
    const title = d.model ? ` title="${escapeHtml(d.model)}${hasSize ? " " + (d.size_b / 1024**4).toFixed(1) + "TB" : ""}"` : "";
    return `
      <div class="disk-chip"${title}>
        <b>${label}</b>
        ${tb}
        ${temp}
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
  const card5 = disks.length ? `
    <div class="metric-card metric-card-wide mc-${GRADE[g4][0]}">
      ${cardHead(`${bayIco(g4)}磁盘`, g4, `<span class="muted">${okCount}/${disks.length} 正常</span>`)}
      ${diskGroups}
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
  // 卷空间
  (m.volumes || []).forEach((v) => {
    if (!v || v.total_kb <= 0) return;
    if (v.percent >= 90)
      push(`vol-${v.mount}`, 2, `「${volNameShort(v.mount)}」空间即将用尽（已用 ${v.percent}%）`, "空间满会影响快照与正常使用", `存储卷「${volNameShort(v.mount)}」已用 ${v.percent}%，请给出清理和扩容建议`);
    else if (v.percent >= 75)
      push(`vol-${v.mount}`, 1, `「${volNameShort(v.mount)}」空间偏紧（已用 ${v.percent}%）`, "建议关注增长", `存储卷「${volNameShort(v.mount)}」已用 ${v.percent}%，有哪些安全的清理建议？`);
  });
  (m.trends || []).forEach((t) => {
    if (t.days_to_full)
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
      q: `NAS Safe 报告异常：${a.title || ""}${a.detail ? "：" + a.detail : ""}。请分析原因并给出排查与修复步骤`,
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
  const wantRemote = localStorage.getItem("nassafe_remote_notify") === "1";

  if (wantDesk && "Notification" in window && Notification.permission === "granted") {
    let body = summary;
    // AI 供应商 = 本地模型时，用本地 AI 把异常写成一句人话提醒（数据不出本机）
    const provider = ($("aiProvider") && $("aiProvider").value) || "";
    if (provider === "ollama" && localStorage.getItem("nassafe_ai_notify") !== "0") {
      try {
        const d = await api("/api/ai/ask", {
          method: "POST",
          body: JSON.stringify({
            question: `请用一句通俗中文（30 字以内）提醒电脑前的用户：${summary}。只输出提醒文案，不要解释。`,
          }),
        });
        if (d && d.text) body = d.text.trim().slice(0, 60);
      } catch (e) { /* AI 不可用时退回原始摘要 */ }
    }
    try {
      const n = new Notification("NAS Safe 异常提醒", { body, tag: "nassafe-anom", requireInteraction: true });
      n.onclick = () => { window.focus(); openAnomalyModal(); };
    } catch (e) { /* 部分浏览器限制非 HTTPS 通知，忽略 */ }
  }

  if (wantRemote) {
    try {
      await api("/api/notify/alert", {
        method: "POST",
        body: JSON.stringify({
          title: "NAS Safe 异常提醒",
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
        <button class="btn ghost" data-act="dismiss${i}">忽略</button>
      </div>
    </div>`).join("");
  list.forEach((a, i) => {
    actions[`view${i}`] = () => { closeModal(); showView(a.view || "home"); };
    actions[`fix${i}`] = () => askAiFix(a);
    actions[`dismiss${i}`] = () => {
      dismissedAnoms.add(a.key);
      localStorage.setItem("nassafe_anom_dismissed", JSON.stringify([...dismissedAnoms]));
      openAnomalyModal();
    };
  });
  openModal("⚠ 检测到异常", body +
    `<p class="muted" style="margin-top:6px">AI 只提供修复建议，不会自动执行任何操作；操作前请自行确认。</p>`, "", actions);
}

// AI 修复方案：带异常上下文提问，展示建议（不执行）
async function askAiFix(a) {
  openModal("🤖 AI 修复方案",
    `<p style="margin-top:0"><b>异常：</b>${escapeHtml(a.title)}</p>
     <p class="muted"><span class="spinner"></span>AI 正在分析…</p>`,
    `<button class="btn ghost" data-act="back">返回异常列表</button>`,
    { back: () => openAnomalyModal() });
  try {
    const data = await api("/api/ai/ask", {
      method: "POST",
      body: JSON.stringify({ question: a.q }),
    });
    openModal("🤖 AI 修复方案",
      `<p style="margin-top:0"><b>异常：</b>${escapeHtml(a.title)}</p>
       <div style="white-space:pre-wrap; line-height:1.8">${escapeHtml(data.text)}</div>
       <p class="muted" style="margin-top:10px">以上为 AI 建议，仅供参考；执行任何操作前请确认。</p>`,
      `<button class="btn ghost" data-act="back">返回异常列表</button>
       <button class="btn primary" data-act="close">关闭</button>`,
      { back: () => openAnomalyModal() });
  } catch (e) {
    openModal("🤖 AI 修复方案",
      `<p>分析失败：${escapeHtml(e.message)}</p>
       <p class="muted">如果提示 AI 未配置，请到「设置 → AI 解读」先启用。</p>`,
      `<button class="btn ghost" data-act="back">返回异常列表</button>`,
      { back: () => openAnomalyModal() });
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
       <p class="muted">如果提示 AI 未配置，请到「设置 → AI 解读」选择云端供应商填密钥，或用「🔍 自动搜索」一键接入本地 Ollama。</p>`,
      `<button class="btn primary" data-act="close">去设置</button>`,
      { goSettings: () => { closeModal(); showView("settings"); } }
    );
  }
  btn.disabled = false;
  btn.textContent = "🤖 AI 体检";
}

// 问 AI：自然语言问 NAS 状态（自动带上当前指标/告警当背景）
function aiAsk() {
  openModal(
    "🤖 问 AI",
    `<textarea id="aiAskText" class="text-input ask-textarea" rows="9"
       placeholder="用大白话问，例如：我的 NAS 现在安全吗？快照会不会把盘占满？最近有什么要注意的？"></textarea>
     <p class="muted" style="margin:8px 0 0">回答基于当前系统状态与告警，仅供参考；关键操作请以人工判断为准。</p>`,
    `<button class="btn ghost" data-act="close">取消</button>
     <button class="btn primary" data-act="send">提问</button>`,
    {
      send: async () => {
        const q = ($("aiAskText") && $("aiAskText").value || "").trim();
        if (!q) { toast("请先输入问题", "warn"); return; }
        $("modalBody").innerHTML = `<p class="muted"><span class="spinner"></span>思考中…</p>`;
        try {
          const data = await api("/api/ai/ask", {
            method: "POST",
            body: JSON.stringify({ question: q }),
          });
          openModal(
            "🤖 问 AI",
            `<p style="margin-top:0"><b>问：</b>${escapeHtml(q)}</p>
             <div style="white-space:pre-wrap; line-height:1.8">${escapeHtml(data.text)}</div>`,
            `<button class="btn ghost" data-act="again">再问一个</button>
             <button class="btn primary" data-act="close">关闭</button>`,
            { again: () => { closeModal(); aiAsk(); } }
          );
        } catch (e) {
          openModal(
            "🤖 问 AI",
            `<p>回答失败：${escapeHtml(e.message)}</p>
             <p class="muted">如果提示 AI 未配置，请到「设置 → AI 解读」先启用。</p>`,
            `<button class="btn primary" data-act="close">关闭</button>`
          );
        }
      },
    }
  );
  // 回车直接提问
  const ta = $("aiAskText");
  if (ta) ta.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); modalActions.send(); }
  });
}

/* ------------------------- 时间轴 ------------------------- */

async function selectVolume(vol, keepSnapshot) {
  state.activeVolume = vol;
  localStorage.setItem("nassafe_volume", vol.mountpoint ?? String(vol.id));
  $("tlTitle").textContent = `快照时间轴 — ${vol.name}`;
  $("tlSubtitle").textContent = `显示「${vol.name}」这一个存储卷的快照（其他卷的快照不在本时间轴内）`;
  const sel = $("tlVolumeSel");
  if (sel && sel.value !== (vol.mountpoint ?? String(vol.id))) sel.value = vol.mountpoint ?? String(vol.id);
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
    $("tlSubtitle").textContent =
      `当前显示「${vol.name}」这一个存储卷的快照，共 ${state.snapshots.length} 张 · 🔒 = 受 NAS Safe 保护`;

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
        <div class="tl-size">${snap.protected ? '<span class="lock" title="受 NAS Safe 保护">🔒</span>' : ""}</div>
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
$("aiDiagnoseBtn").onclick = aiDiagnose;
$("aiAskBtn").onclick = aiAsk;

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

// 主导航：绑定标签页点击并恢复上次所在视图（localStorage 持久化）
document.querySelectorAll("#mainTabs .tab").forEach((t) => {
  t.onclick = () => showView(t.dataset.view);
});
applyView();

// 系统仪表盘：手动刷新 + 15s 自动轮询（仅主页可见时取数，SSH 采集有 15s 缓存）
$("metricsRefreshBtn").onclick = () => loadMetrics(true);
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

// 关掉网页也想收到提醒 → 桌面小助手（本机常驻，不依赖浏览器）
function showDesktopAgentGuide() {
  const cmd = `pythonw "${(location.origin + "/desktop_agent.py").replace(/^/, "")}" --nas ${location.origin}`;
  openModal(
    "🖥 用桌面小助手（关掉网页也能弹）",
    `<p>浏览器权限只能让「网页开着时」弹窗。想彻底关掉网页也收到提醒，用这个本机常驻小程序：</p>
     <ol class="perm-steps">
       <li>在本机 NAS Safe 目录找到 <code>scripts/desktop_agent.py</code></li>
       <li>命令行运行（后台静默）：
         <div class="code-box"><code>pythonw scripts/desktop_agent.py --nas ${escapeHtml(location.origin)}</code></div></li>
       <li>它会在后台定时查 NAS 状态，有异常直接弹 Windows 通知中心提醒，不需要开网页</li>
     </ol>
     <p class="muted">小助手只用 Python 自带库，不用装任何东西；退出就是关掉对应进程。</p>`,
    `<button class="btn ghost" data-act="copy">复制命令</button>
     <button class="btn primary" data-act="close">知道了</button>`,
    {
      copy: () => {
        const text = `pythonw scripts/desktop_agent.py --nas ${location.origin}`;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(
            () => toast("命令已复制", "ok"),
            () => toast("复制失败，请手动选中复制", "warn")
          );
        } else toast("复制失败，请手动选中复制", "warn");
      },
    }
  );
  void cmd;
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
$("deskNotifyChk").checked = localStorage.getItem("nassafe_desk_notify") === "1";
$("aiNotifyChk").checked = localStorage.getItem("nassafe_ai_notify") !== "0";
$("remoteNotifyChk").checked = localStorage.getItem("nassafe_remote_notify") === "1";
$("deskNotifyChk").onchange = () => localStorage.setItem("nassafe_desk_notify", $("deskNotifyChk").checked ? "1" : "0");
$("aiNotifyChk").onchange = () => localStorage.setItem("nassafe_ai_notify", $("aiNotifyChk").checked ? "1" : "0");
$("remoteNotifyChk").onchange = () => localStorage.setItem("nassafe_remote_notify", $("remoteNotifyChk").checked ? "1" : "0");
$("pushTestBtn").onclick = async () => {
  try {
    const r = await api("/api/notify/alert", { method: "POST", body: JSON.stringify({ title: "NAS Safe 测试提醒", detail: "这是一条异常提醒通道的测试消息", level: "warn" }) });
    toast(r && r.ok ? `已推送（通道：${r.channel}）` : `推送失败：${(r && r.msg) || "未知"}`, r && r.ok ? "ok" : "err");
  } catch (e) { toast("推送失败：" + e.message, "err"); }
};
// 时间轴页：卷切换下拉框 —— 不用回总览，直接换卷看时间轴
$("tlVolumeSel").onchange = () => {
  const key = $("tlVolumeSel").value;
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
