# -*- coding: utf-8 -*-
"""前端补丁：添加设备=自动扫描（含异地组网）+ 任意设备改名"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def patch(path, pairs):
    p = os.path.join(ROOT, path)
    with io.open(p, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    crlf = raw.count("\r\n") * 2 > raw.count("\n")
    s = raw.replace("\r\n", "\n")
    for old, new in pairs:
        assert s.count(old) == 1, (path, s.count(old), old[:70])
        s = s.replace(old, new)
    if crlf:
        s = s.replace("\n", "\r\n")
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)
    print("patched", path)


JS = "web/app.js"
CSS = "web/style.css"

patch(JS, [
    # 1) 添加设备：改成「自动扫描」弹窗
    (
        '''async function addDevice() {
  const name = (prompt("设备名称（如：客厅群晖）：") || "").trim();
  if (!name) return;
  const host = (prompt("设备访问地址（NAS 的 IP 或域名，不含 http）：", "192.168.8.") || "").trim();
  if (!host) return;
  const port = (prompt("端口（NAS Safe 控制台端口，默认 8848）：", "8848") || "8848").trim();
  const group = (prompt("分组名（如：家里 / 公司 / 机房）：", "远程设备") || "远程设备").trim();
  const brand = (prompt("品牌（qnap/synology/ugreen/feiniu/truenas/omv/unraid/generic_linux）：", "generic_linux") || "generic_linux").trim();
  try {
    await api("/api/devices/add", { method: "POST", body: JSON.stringify({ name, host, port: Number(port) || 8848, group, brand }) });
    toast("已添加设备，正在汇总…", "ok");
    loadConsole(true);
  } catch (e) { toast("添加失败：" + e.message, "err"); }
}''',
        '''/* ---------- 添加设备：先自动扫一遍（局域网 + 异地组网），扫不到再手填 ---------- */
const SCAN_KIND_LABEL = { lan: "局域网", vpn: "异地组网", manual: "手填网段" };

function addDevice() { openAddDeviceModal(); }

function openAddDeviceModal() {
  openModal(
    "＋ 添加设备",
    `<div class="scan-form">
      <p class="muted scan-tip">先自动扫一遍：会挨个试着连一下网段里的地址，把装了 NAS Safe 的设备找出来（家里、办公室、异地组网都算）。只读探测，不会改对端任何东西。</p>
      <div class="scan-grid">
        <label class="scan-row"><span>端口</span><input id="scanPort" class="text-input" value="8848" inputmode="numeric"></label>
        <label class="scan-row"><span>分组</span><input id="scanGroup" class="text-input" value="联网设备" placeholder="家里 / 公司 / 机房"></label>
        <label class="scan-row"><span>账号（选填）</span><input id="scanUser" class="text-input" placeholder="对端 NAS Safe 的登录账号"></label>
        <label class="scan-row"><span>密码（选填）</span><input id="scanPwd" class="text-input" type="password" placeholder="对端登录密码"></label>
        <label class="scan-row wide"><span>额外网段</span><input id="scanCidrs" class="text-input" placeholder="如 100.64.0.0/10，多个用空格隔开"></label>
      </div>
      <div class="scan-opts">
        <label><input type="checkbox" id="scanLan" checked> 本机局域网</label>
        <label><input type="checkbox" id="scanVpn" checked> 异地组网（Tailscale / WireGuard / VPN 等）</label>
      </div>
    </div>
    <div class="scan-result" id="scanResult"></div>`,
    `<button class="btn ghost" data-act="close">关闭</button>
     <button class="btn ghost" data-act="manual">✍ 手动添加</button>
     <button class="btn primary" data-act="scan">🔍 开始扫描</button>`,
    { scan: runNetScan, manual: openManualAdd }
  );
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
    box.innerHTML = `<p class="muted">扫了 ${data.scanned || 0} 个地址，没找到装了 NAS Safe 的设备。<br>
      可能是对端还没装、端口不一样，或者被防火墙挡了。可以先点「手动添加」直接填地址。</p>`;
    return;
  }
  let html = `<div class="scan-sum">扫了 ${data.scanned} 个地址，找到 ${found.length} 台（用时 ${data.seconds}s）。给每台起个好认的名字，之后随时能改：</div>`;
  for (const f of found) {
    const kindTxt = SCAN_KIND_LABEL[f.kind] || "联网";
    const netTxt = f.iface && f.iface !== "手动填写" ? `${kindTxt} · ${escapeHtml(f.iface)}` : kindTxt;
    const brandTxt = f.brand_label || f.brand || "NAS Safe";
    const defName = `${brandTxt} ${f.ip}`;
    const kind = (f.kind === "vpn") ? "chip" : topoKindFromBrand(f.brand);
    html += `<div class="scan-item">
      <label class="si-pick"><input type="checkbox" checked
        data-ip="${escapeHtml(f.ip)}" data-port="${f.port}" data-brand="${escapeHtml(f.brand || "generic_linux")}" data-kind="${escapeHtml(f.kind || "")}"></label>
      <div class="si-ico">${topoDeviceGlyph(kind)}</div>
      <div class="si-main">
        <input class="text-input si-name" value="${escapeHtml(defName)}" maxlength="24">
        <div class="si-meta">${escapeHtml(f.ip)}:${f.port} · ${escapeHtml(brandTxt)} · ${escapeHtml(netTxt)}${f.need_auth ? " · 需要账号" : ""}</div>
      </div>
    </div>`;
  }
  if (data.truncated) html += `<p class="muted scan-note">网段太大，只扫了一部分；可以在「额外网段」里填更小的网段（如 192.168.8.0/24）缩小范围。</p>`;
  box.innerHTML = html;
  const foot = $("modalFoot");
  if (foot) {
    foot.innerHTML = `<button class="btn ghost" data-act="close">关闭</button>
      <button class="btn ghost" data-act="scan">重新扫描</button>
      <button class="btn primary" data-act="addsel">＋ 添加选中（${found.length}）</button>`;
  }
  modalActions.addsel = addSelectedDevices;
}

async function addSelectedDevices() {
  const rows = [...document.querySelectorAll("#scanResult .scan-item")];
  const picked = rows.filter((r) => { const c = r.querySelector("input[type=checkbox]"); return c && c.checked; });
  if (!picked.length) { toast("先勾选要添加的设备", "warn"); return; }
  const group = (($(("scanGroup") || {}).value) || "联网设备").trim();
  let ok = 0, fail = 0;
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
    };
    try { await api("/api/devices/add", { method: "POST", body: JSON.stringify(payload) }); ok++; }
    catch (e) { fail++; }
  }
  toast(fail ? `已添加 ${ok} 台，${fail} 台失败` : `已添加 ${ok} 台设备`, fail ? "warn" : "ok");
  closeModal();
  loadConsole(true);
}

function openManualAdd() {
  openModal(
    "✍ 手动添加设备",
    `<div class="scan-grid">
      <label class="scan-row"><span>名称</span><input id="maName" class="text-input" placeholder="如：办公室群晖"></label>
      <label class="scan-row"><span>地址</span><input id="maHost" class="text-input" placeholder="IP 或域名，不含 http"></label>
      <label class="scan-row"><span>端口</span><input id="maPort" class="text-input" value="8848" inputmode="numeric"></label>
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
      </select></label>
    </div>
    <p class="muted" style="margin:10px 0 0">异地组网的设备（Tailscale / WireGuard 等）填它的组网 IP 就行，和填局域网地址一样。</p>`,
    `<button class="btn ghost" data-act="close">取消</button>
     <button class="btn primary" data-act="save">添加</button>`,
    {
      save: async () => {
        const g = (id) => $(id) || {};
        const host = (g("maHost").value || "").trim();
        if (!host) { toast("请填写设备地址", "warn"); return; }
        const payload = {
          name: (g("maName").value || "").trim() || host,
          host,
          port: Number(g("maPort").value || 8848) || 8848,
          group: (g("maGroup").value || "联网设备").trim(),
          brand: g("maBrand").value || "generic_linux",
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
}''',
    ),
    # 2) 本机自定义名优先
    (
        '''function deviceDisplayName(d) {
  if (d && d.type === "local") {
    const b = (d.brand_label || d.brand || "").trim();
    return b ? b + " · 本机" : "本机";
  }
  return (d && d.name) || "设备";
}''',
        '''function deviceDisplayName(d) {
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
}''',
    ),
    # 3) 设备卡（本机）加改名按钮
    (
        '''      <div class="dev-card-name">${escapeHtml(deviceDisplayName(d))}</div>''',
        '''      <div class="dev-card-name">${escapeHtml(deviceDisplayName(d))}<button class="nm-edit" data-rename="${escapeHtml(d.id)}" title="给这台改名">✎</button></div>''',
    ),
    # 4) 联网设备图标框：名字也走统一显示 + 改名按钮
    (
        '''      <div class="nt-name">${escapeHtml(d.name || "设备")}</div>''',
        '''      <div class="nt-name">${escapeHtml(deviceDisplayName(d))}<button class="nm-edit" data-rename="${escapeHtml(d.id)}" title="给这台改名">✎</button></div>''',
    ),
    # 5) 详情面板加改名按钮
    (
        '''  if (dev.type !== "local" && !dev.__demo) {
    actions += `<button class="btn ghost sm" id="ddRemoveBtn">移除设备</button>`;
  }''',
        '''  if (!dev.__demo) {
    actions += `<button class="btn ghost sm" id="ddRenameBtn">✎ 改名</button>`;
  }
  if (dev.type !== "local" && !dev.__demo) {
    actions += `<button class="btn ghost sm" id="ddRemoveBtn">移除设备</button>`;
  }''',
    ),
    (
        '''  const rm = $("ddRemoveBtn");''',
        '''  const rn2 = $("ddRenameBtn");
  if (rn2) rn2.onclick = () => openRenameModal(dev.id);
  const rm = $("ddRemoveBtn");''',
    ),
    # 6) renderConsole 绑定改名按钮
    (
        '''  wrap.querySelectorAll("[data-remove]").forEach((b) => {''',
        '''  wrap.querySelectorAll("[data-rename]").forEach((b) => {
    b.onclick = (ev) => { ev.stopPropagation(); openRenameModal(b.dataset.rename); };
  });
  wrap.querySelectorAll("[data-remove]").forEach((b) => {''',
    ),
    # 7) 拓扑节点名字长度放宽（用户可能起长名）
    (
        '''    const name = escapeHtml(deviceDisplayName(d).slice(0, 8));''',
        '''    const name = escapeHtml(deviceDisplayName(d).slice(0, 10));''',
    ),
])

# ---------------- CSS ----------------
p = os.path.join(ROOT, CSS)
with io.open(p, "r", encoding="utf-8", newline="") as f:
    css = f.read()
css_crlf = css.count("\r\n") * 2 > css.count("\n")
css_work = css.replace("\r\n", "\n")
if ".scan-item" not in css_work:
    css_work += """

/* ---------- 添加设备：自动扫描 + 改名 ---------- */
.scan-tip { margin: 0 0 12px; line-height: 1.7; }
.scan-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.scan-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.scan-row.wide { grid-column: 1 / -1; }
.scan-row > span { flex: none; width: 84px; font-size: 13px; color: var(--muted, #8b97a8); }
.scan-row .text-input { flex: 1 1 auto; min-width: 0; }
.scan-opts { display: flex; flex-wrap: wrap; gap: 16px; margin-top: 12px; font-size: 13px; }
.scan-opts label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
.scan-result { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 14px; }
.scan-sum { font-size: 13px; margin-bottom: 12px; line-height: 1.7; }
.scan-item { display: flex; align-items: center; gap: 12px; padding: 10px; margin-bottom: 8px;
  border: 1px solid var(--border); border-radius: 12px; background: rgba(127,127,127,.04); }
.si-pick { flex: none; display: flex; align-items: center; }
.si-ico { flex: none; width: 38px; height: 38px; display: flex; align-items: center; justify-content: center;
  border-radius: 11px; background: rgba(127,127,127,.10); }
.si-ico svg { width: 22px; height: 22px; }
.si-main { flex: 1 1 auto; min-width: 0; }
.si-main .si-name { width: 100%; box-sizing: border-box; margin-bottom: 4px; }
.si-meta { font-size: 12px; color: var(--muted, #8b97a8); }
.scan-note { margin-top: 10px; line-height: 1.7; }
.nm-edit { flex: none; margin-left: 6px; padding: 0 4px; border: 0; background: transparent;
  color: var(--muted, #8b97a8); font-size: 13px; line-height: 1; cursor: pointer; opacity: .55; }
.nm-edit:hover { opacity: 1; color: var(--text); }
.net-tile .nm-edit { position: absolute; top: 4px; right: 6px; }
.net-tile { position: relative; }
@media (max-width: 560px) { .scan-grid { grid-template-columns: 1fr; } }
"""
    if css_crlf:
        css_work = css_work.replace("\n", "\r\n")
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        f.write(css_work)
    print("patched", CSS)

print("OK")
