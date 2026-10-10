"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let S = null;                        // last printer state
let cfgLight = false;                // config.json light_enabled
let root = "/local/", curPath = "/local/";
let fileItems = [];
const selected = new Set();

// ------------------------------------------------------------------ helpers
function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind; t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "bad" ? 7000 : 3500);
}
async function api(path, body, method) {
  const opt = body !== undefined ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : { method: method || "GET" };
  let r, j;
  try { r = await fetch(path, opt); j = await r.json(); }
  catch (e) { return { ok: false, message: "Request failed: " + e }; }
  return j;
}
async function act(path, body, okMsg) {
  const j = await api(path, body ?? {});
  if (j.ok) { if (okMsg) toast(okMsg, "ok"); } else toast(j.message || "Failed", "bad");
  return j;
}
function confirmDialog(html, okLabel = "OK", danger = false) {
  return new Promise((res) => {
    const d = $("dlg"); $("dlgBody").innerHTML = html;
    const ok = $("dlgOk"); ok.textContent = okLabel; ok.classList.toggle("danger", danger); ok.classList.toggle("primary", !danger);
    d.onclose = () => res(d.returnValue === "ok" ? d : null);
    d.showModal();
  });
}
const fmtDur = (s) => {
  if (s == null || !isFinite(s)) return "–";
  s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
};
const fmtSize = (b) => {
  if (b == null) return "–"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < 4) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + " " + u[i];
};
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "–";
function ago(ts) {
  if (!ts) return null;
  const d = Math.floor((Date.now() / 1000 - ts) / 86400);
  if (d <= 0) return "today"; if (d === 1) return "yesterday"; if (d < 60) return d + " days ago";
  const m = Math.floor(d / 30); return m < 24 ? m + " months ago" : Math.floor(d / 365) + " years ago";
}
function thumbSrc(t) {
  if (!t) return null;
  if (t.startsWith("data:")) return t;
  if (/^[A-Za-z0-9+/=]{200,}$/.test(t)) return "data:image/png;base64," + t;
  return "/api/fetch?u=" + encodeURIComponent(t);
}
function thumbHtml(t) {
  const s = thumbSrc(t);
  return s ? `<img class="thumb" loading="lazy" src="${esc(s)}" alt="" onerror="this.outerHTML='<div class=&quot;thumb&quot;>🗎</div>'">` : `<div class="thumb">🗎</div>`;
}

// ------------------------------------------------------------------ live state
function connectWS() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.type === "state") { S = m.state; render(); } else if (m.type === "log_init") { logEntries.length = 0; lv.textContent = ""; logAdd(m.entries, true); } else if (m.type === "log") logAdd(m.entries); };
  ws.onclose = () => { setTimeout(connectWS, 2000); S && ((S.connected = false), render(true)); };
}
let lastUploadPhase = null;
function render(serverDown) {
  if (!S) return;
  const conn = S.connected && !serverDown;
  $("connDot").classList.toggle("off", !conn);
  $("connText").textContent = serverDown ? "App server unreachable" : conn ? `Connected · ${S.printer_ip}` : `Printer offline (${S.printer_ip}) – retrying…`;
  const a = S.attributes || {};
  $("printerName").textContent = a.Name || "Saturn Control";
  document.title = (a.Name || "Saturn Control") + (S.machine_status_codes.includes(1) ? ` · ${Math.round(S.progress)}%` : "");
  const busy = S.machine_status_codes;
  const chip = $("stateChip");
  chip.textContent = S.print_status && busy.includes(1) ? S.print_status : (S.machine_status[0] || "–");
  chip.className = "chip " + (S.print_status_code === 6 ? "paused" : busy.includes(1) ? "printing" : "");
  const bn = $("banner");
  if (!conn) { bn.className = "banner"; bn.textContent = S.last_error ? `Cannot reach printer: ${S.last_error}` : "Waiting for printer…"; }
  else if (S.print_error) { bn.className = "banner"; bn.textContent = "Print error: " + S.print_error; }
  else bn.className = "banner hidden";

  // job
  const pi = (S.status || {}).PrintInfo || {};
  const printing = busy.includes(1);
  $("jobFile").textContent = pi.Filename ? pi.Filename : "No active print";
  $("printStatus").textContent = S.print_status || "Idle";
  $("jobBar").style.width = S.progress + "%"; $("jobPct").textContent = Math.round(S.progress) + "%";
  $("sLayer").textContent = pi.TotalLayer ? `${pi.CurrentLayer} / ${pi.TotalLayer}` : "–";
  $("sElapsed").textContent = pi.CurrentTicks ? fmtDur(pi.CurrentTicks / 1000) : "–";
  $("sRemain").textContent = printing && S.remaining_ms != null ? fmtDur(S.remaining_ms / 1000) : "–";
  $("sEta").textContent = printing && S.remaining_ms != null ? new Date(Date.now() + S.remaining_ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–";
  const paused = S.print_status_code === 6, pausing = S.print_status_code === 5;
  $("bPause").disabled = !(printing && !paused && !pausing && conn);
  $("bResume").disabled = !(printing && paused && conn);
  $("bStop").disabled = !(printing && conn);
  $("bSkip").disabled = !(printing && conn);
  $("bFeed").disabled = !conn;
  const je = $("jobError");
  if (S.print_error) { je.textContent = S.print_error; je.classList.remove("hidden"); } else je.classList.add("hidden");

  renderTweaks();
  // light + timelapse
  $("btnLight").classList.toggle("hidden", !(cfgLight || S.light_variant));
  $("btnLight").classList.toggle("on", S.light === true);
  $("btnLight").textContent = S.light === true ? "💡 Light on" : "💡 Light";
  const cs = S.camera, chipC = $("camSlots");
  if (cs) {
    chipC.classList.remove("hidden"); chipC.textContent = `Camera slots ${cs.used}/${cs.max}`;
    chipC.classList.toggle("warn", cs.others > 0);
    chipC.title = cs.others > 0 ? `${cs.others} other viewer(s) are using the printer camera (the Matrix app's live view counts as one)` : "Only this app is using the camera";
  } else chipC.classList.add("hidden");
  const tl = (S.status || {}).TimeLapseStatus;
  if (tl != null && document.activeElement !== $("tlSwitch")) $("tlSwitch").checked = !!tl;

  renderInfo(a);
  renderUpload(S.upload);
  renderMessages();
  $("camToggle").textContent = camOn ? "Stop" : "Start";
  $("btnDeleteSel").disabled = !selected.size;
}
function kv(k, v) { return `<div><label>${esc(k)}</label><b>${esc(v ?? "–")}</b></div>`; }
function kvo(k, v) { return v == null ? "" : kv(k, v); }      // optional: left out when the printer does not report it
function renderInfo(a) {
  const st = S.status || {};
  const film = st.ReleaseFilm, filmMax = a.ReleaseFilmMax;
  $("machineStats").innerHTML = [
    kvo("Vat temp", st.TempOfTank != null ? `${Number(st.TempOfTank).toFixed(0)} °C` + (st.TempTargetTank ? ` (target ${st.TempTargetTank} °C)` : "") : null),
    kvo("Vat heater", st.HeatStatus != null ? (st.HeatStatus ? "On" : "Off") : null),
    kvo("UV LED temp", st.TempOfUVLED != null ? Number(st.TempOfUVLED).toFixed(2) + " °C" : null),
    kvo("Release film use", film != null ? `${film}${filmMax ? ` / ${filmMax} (${Math.round(film / filmMax * 100)}%)` : ""}` : null),
    kvo("Time-lapse", st.TimeLapseStatus != null ? (st.TimeLapseStatus ? "On" : "Off") : null),
    kvo("Free space", a.RemainingMemory != null ? fmtSize(a.RemainingMemory) : null),
  ].join("") || '<div class="muted">Waiting for printer…</div>';
  const d = a.DevicesStatus || {};
  const lab = (v, labels) => labels[v] ?? v;
  const devs = [["UV LED sensor", d.TempSensorStatusOfUVLED, ["Disconnected", "Normal", "Abnormal"]],
    ["Vat temp sensor", d.TankStatus, ["Disconnected", "Normal", "Abnormal"]],
    ["LCD", d.LCDStatus, ["Disconnected", "Connected"]],
    ["Strain gauge", d.SgStatus, ["Disconnected", "Normal", "Calibration failed"]],
    ["Z motor", d.ZMotorStatus, ["Disconnected", "Connected"]],
    ["X motor", d.XMotorStatus, ["Disconnected", "Connected"]],
    ["Rotary motor", d.RotateMotorStatus, ["Disconnected", "Connected"]],
    ["Release film", d.RelaseFilmState, ["Abnormal", "Normal"]],
    ["Camera", a.CameraStatus, ["Disconnected", "Connected"]]];
  $("deviceStats").innerHTML = devs.map(([k, v, l]) => kvo(k, v == null ? null : lab(v, l))).join("");
  $("attrStats").innerHTML = [
    kv("Model", a.MachineName), kv("Brand", a.BrandName), kv("Firmware", a.FirmwareVersion),
    kv("Protocol", a.ProtocolVersion), kv("Resolution", a.Resolution), kv("Build volume (mm)", a.XYZsize),
    kv("IP", a.MainboardIP), kv("Mainboard ID", S.mainboard_id), kv("Network", a.NetworkStatus),
    kv("USB drive", a.UsbDiskStatus ? "Connected" : "Not connected"),
    kvo("Camera clients", a.MaximumVideoStreamAllowed != null ? `${a.NumberOfVideoStreamConnected} / ${a.MaximumVideoStreamAllowed}` : null),
    kv("File types", (a.SupportFileType || []).join(", ")), kv("Capabilities", (a.Capabilities || []).join(", ")),
    kvo("Max UV LED temp", a.TempOfUVLEDMax ? a.TempOfUVLEDMax + " °C" : null),
    kvo("Time-lapse start/height/interval", a.TLPStartCapPos != null ? `${a.TLPStartCapPos}mm / ${a.TLPNoCapPos}mm / ${a.TLPInterLayers} layers` : null),
    kvo("Connection restarts this session", S.reconnects != null ? String(Math.max(S.reconnects - 1, 0)) : null),
  ].join("");
  if (document.activeElement !== $("inName")) $("inName").value = a.Name || "";
}
function renderMessages() {
  const items = [...(S.errors || []).map((e) => ["Error", e]), ...(S.notices || []).map((e) => ["Notice", e])].sort((x, y) => y[1].t - x[1].t);
  $("msgList").innerHTML = items.length ? items.map(([k, e]) => `<div><b>${k}</b> <span class="muted">${new Date(e.t * 1000).toLocaleTimeString()}</span> ${esc(JSON.stringify(e.data))}</div>`).join("") : '<div class="muted">No messages</div>';
}
function renderUpload(u) {
  const box = $("uploadBox");
  if (!u || u.phase === "done" && Date.now() / 1000 - (u.finished || 0) > 6 || (u.phase === "cancelled")) { box.classList.add("hidden"); }
  else {
    box.classList.remove("hidden");
    $("upName").textContent = u.name;
    const pct = u.total ? (u.sent / u.total) * 100 : 0;
    $("upBar").style.width = pct + "%"; $("upPct").textContent = Math.round(pct) + "%";
    $("upPhase").textContent = { sending: "Sending to printer…", verifying: "Printer verifying…", done: "Uploaded ✓", error: "Failed" }[u.phase] + (u.error ? " – " + u.error : "");
    $("upCancel").classList.toggle("hidden", !["sending", "verifying"].includes(u.phase));
  }
  if (u && u.phase !== lastUploadPhase) {
    if (u.phase === "done") { toast(`Uploaded ${u.name}`, "ok"); loadFiles(); }
    if (u.phase === "error") toast("Upload failed: " + u.error, "bad");
    lastUploadPhase = u.phase;
  }
}

// ------------------------------------------------------------------ tabs
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
  $("tab-" + b.dataset.tab).classList.remove("hidden");
  if (b.dataset.tab === "history") loadHistory();
  if (b.dataset.tab === "explorer" && !exLoaded) exStart();
});

// ------------------------------------------------------------------ files
async function loadFiles() {
  $("fileList").innerHTML = '<div class="empty">Loading…</div>';
  const j = await api("/api/files?path=" + encodeURIComponent(curPath));
  if (!j.ok) { $("fileList").innerHTML = `<div class="empty">${esc(j.message)}</div>`; return; }
  fileItems = j.files; selected.clear();
  const nf = fileItems.filter((f) => f.fragment).length;
  $("fragBar").classList.toggle("hidden", !nf);
  $("fragText").textContent = `Show ${nf} incomplete upload${nf > 1 ? "s" : ""} (interrupted transfers, safe to delete)`;
  const sb = $("storageBar");
  if (j.storage && j.storage.total) {
    sb.classList.remove("hidden");
    $("storageFill").style.width = (j.storage.used / j.storage.total * 100) + "%";
    $("storageText").textContent = `${fmtSize(j.storage.used)} used of ${fmtSize(j.storage.total)}`;
  } else sb.classList.add("hidden");
  renderCrumbs(); renderFiles();
}
function renderCrumbs() {
  const parts = curPath.replace(/^\/|\/$/g, "").split("/");
  let acc = "/", html = "";
  parts.forEach((p, i) => { acc += p + "/"; html += (i ? " / " : "") + `<a data-p="${esc(acc)}">${i === 0 ? (p === "usb" ? "USB" : "Internal") : esc(p)}</a>`; });
  $("crumbs").innerHTML = html;
}
$("crumbs").addEventListener("click", (e) => { const a = e.target.closest("a"); if (a) { curPath = a.dataset.p; loadFiles(); } });
$("storageSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  root = curPath = b.dataset.root;
  document.querySelectorAll("#storageSeg button").forEach((x) => x.classList.toggle("active", x === b));
  loadFiles();
});
function renderFiles() {
  const q = $("fileFilter").value.toLowerCase();
  const items = fileItems.filter((f) => f.name.toLowerCase().includes(q) && (!f.fragment || $("showFrag").checked));
  if (!items.length) { $("fileList").innerHTML = '<div class="empty">No files here.</div>'; return; }
  $("fileList").innerHTML = items.map((f, i) => {
    const idx = fileItems.indexOf(f);
    if (f.is_dir) return `<div class="row-f"><input type="checkbox" data-i="${idx}"><div class="thumb">📁</div><div><div class="f-name dir" data-open="${idx}">${esc(f.name)}</div><div class="f-meta">Folder</div></div><div class="f-act"></div></div>`;
    if (f.fragment) return `<div class="row-f"><input type="checkbox" data-i="${idx}"><div class="thumb">⚠</div><div><div class="f-name">${esc(f.name)}<span class="badge warn">Incomplete upload</span></div><div class="f-meta">${f.size ? `<span>Size <b>${fmtSize(f.size)}</b></span>` : ""}<span>Left behind by an interrupted transfer</span></div></div><div class="f-act"><button class="btn sm danger" data-del="${idx}">🗑</button></div></div>`;
    const meta = [];
    if (f.layer_height != null) meta.push(`<span>Layer height <b>${Number(f.layer_height).toFixed(2)} mm</b></span>`);
    if (f.layers != null) meta.push(`<span>Layers <b>${f.layers}</b></span>`);
    if (f.est_seconds != null) meta.push(`<span>Est. time <b>${fmtDur(f.est_seconds)}</b></span>`);
    if (f.size != null) meta.push(`<span>Size <b>${fmtSize(f.size)}</b></span>`);
    if (f.create_time) meta.push(`<span>Created <b>${esc(typeof f.create_time === "number" ? (ago(f.create_time) + " · " + fmtDate(f.create_time)) : f.create_time)}</b></span>`);
    meta.push(f.last_printed ? `<span>Last printed <b>${ago(f.last_printed)}</b>${f.print_count > 1 ? ` (${f.print_count}×)` : ""}</span>` : `<span>Never printed</span>`);
    return `<div class="row-f"><input type="checkbox" data-i="${idx}">${thumbHtml(f.thumbnail)}<div><div class="f-name">${esc(f.name)}${f.percent_codes ? '<span class="badge bad" title="The name contains %xx codes, so it was uploaded with the wrong name. Delete it and upload again with the fixed app.">name has % codes</span>' : ""}</div><div class="f-meta">${meta.join("")}</div></div>
      <div class="f-act"><button class="btn sm primary" data-print="${idx}">▶ Print</button><button class="btn sm danger" data-del="${idx}">🗑</button></div></div>`;
  }).join("");
}
$("fileFilter").addEventListener("input", renderFiles);
$("showFrag").addEventListener("change", renderFiles);
const delFrag = document.createElement("button");
delFrag.className = "btn sm danger"; delFrag.textContent = "Delete incomplete uploads";
delFrag.onclick = () => deleteItems(fileItems.filter((f) => f.fragment));
$("fragBar").appendChild(delFrag);
$("fileList").addEventListener("click", async (e) => {
  const t = e.target;
  if (t.dataset.open != null) { curPath = fileItems[t.dataset.open].path.replace(/\/?$/, "/"); loadFiles(); }
  else if (t.dataset.print != null) startPrint(fileItems[t.dataset.print]);
  else if (t.dataset.del != null) deleteItems([fileItems[t.dataset.del]]);
});
$("fileList").addEventListener("change", (e) => {
  if (e.target.dataset.i == null) return;
  e.target.checked ? selected.add(+e.target.dataset.i) : selected.delete(+e.target.dataset.i);
  $("btnDeleteSel").disabled = !selected.size;
});
$("btnRefresh").onclick = loadFiles;
$("btnRawFiles").onclick = () => {
  const raw = JSON.stringify(fileItems.map((f) => f.raw), null, 1);
  navigator.clipboard.writeText(raw).then(() => toast("Raw file list copied (" + fileItems.length + " entries)", "ok"), () => { toast("Copy failed", "bad"); });
};
$("btnSettings").onclick = async () => {
  const d = await confirmDialog(`<h3>App settings</h3><label>Printer IP address<br><input class="input" id="setIp" value="${esc(S ? S.printer_ip : "")}" style="margin-top:6px;width:100%"></label><p class="muted">Saved to config.json. The app reconnects straight away.</p>`, "Save");
  if (!d) return;
  const j = await act("/api/settings", { printer_ip: $("setIp").value }, "Saved. Reconnecting…");
  if (j.ok) setTimeout(loadFiles, 2500);
};
$("btnDeleteSel").onclick = () => deleteItems([...selected].map((i) => fileItems[i]));
async function deleteItems(items) {
  if (!items.length) return;
  const list = items.slice(0, 8).map((f) => `<li>${esc(f.name)}</li>`).join("") + (items.length > 8 ? `<li>…and ${items.length - 8} more</li>` : "");
  const ok = await confirmDialog(`<h3>Delete ${items.length} item${items.length > 1 ? "s" : ""} from the printer?</h3><ul>${list}</ul><p class="muted">This cannot be undone.</p>`, "Delete", true);
  if (!ok) return;
  const j = await api("/api/files/delete", { files: items.filter((f) => !f.is_dir).map((f) => f.path), folders: items.filter((f) => f.is_dir).map((f) => f.path) });
  if (j.ok) toast("Deleted", "ok"); else toast((j.message || "Delete failed") + (j.failed && j.failed.length ? ": " + j.failed.join(", ") : ""), "bad");
  loadFiles();
}
async function startPrint(f) {
  if (S && S.machine_status_codes.includes(1)) return toast("A print is already running", "bad");
  const info = [f.layer_height != null ? Number(f.layer_height).toFixed(2) + " mm layers" : null, f.layers ? f.layers + " layers" : null, f.est_seconds ? "~" + fmtDur(f.est_seconds) : null].filter(Boolean).join(" · ");
  const d = await confirmDialog(`<h3>Start printing?</h3><p><b>${esc(f.name)}</b></p><p class="muted">${esc(info)}</p><p>Make sure the vat has resin and the plate is clean.</p><label>Start at layer <input class="input narrow" id="startLayer" type="number" min="0" value="0"></label>`, "Start print");
  if (!d) return;
  await act("/api/print", { filename: f.path, start_layer: +$("startLayer").value || 0 }, "Print started");
}

// ------------------------------------------------------------------ upload
$("btnUpload").onclick = () => $("fileInput").click();
$("fileInput").onchange = () => { uploadFiles([...$("fileInput").files]); $("fileInput").value = ""; };
const dz = $("dropzone");
["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
document.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]); });
async function uploadFiles(files) {
  if (!files.length) return;
  const d = await confirmDialog(`<h3>Upload to printer</h3><ul>${files.map((f) => `<li>${esc(f.name)} <span class="muted">(${fmtSize(f.size)})</span></li>`).join("")}</ul>${files.length === 1 ? '<label><input type="checkbox" id="printAfter"> Start printing as soon as the upload finishes</label>' : ""}`, "Upload");
  if (!d) return;
  const printAfter = files.length === 1 && $("printAfter").checked;
  for (const f of files) {
    const ok = await sendOne(f, printAfter);
    if (!ok) break;
  }
}
function sendOne(file, printAfter) {
  return new Promise((resolve) => {
    const fd = new FormData(); fd.append("print_after", printAfter ? "1" : "0"); fd.append("file", file, file.name);
    const x = new XMLHttpRequest(); x.open("POST", "/api/upload");
    const box = $("uploadBox"); box.classList.remove("hidden");
    $("upName").textContent = file.name; $("upCancel").classList.add("hidden");
    x.upload.onprogress = (e) => { if (e.lengthComputable) { const p = e.loaded / e.total * 100; $("upBar").style.width = p + "%"; $("upPct").textContent = Math.round(p) + "%"; $("upPhase").textContent = "Sending to this app…"; } };
    x.onload = async () => {
      let j = {}; try { j = JSON.parse(x.responseText); } catch { }
      if (!j.ok) { toast(j.message || "Upload failed", "bad"); box.classList.add("hidden"); return resolve(false); }
      lastUploadPhase = null;
      const wait = setInterval(() => {            // wait for printer-side transfer to finish
        const u = S && S.upload;
        if (u && u.name === file.name && ["done", "error", "cancelled"].includes(u.phase)) { clearInterval(wait); resolve(u.phase === "done"); }
      }, 400);
    };
    x.onerror = () => { toast("Upload failed (connection)", "bad"); box.classList.add("hidden"); resolve(false); };
    x.send(fd);
  });
}
$("upCancel").onclick = () => act("/api/upload/cancel", {}, "Upload cancelled");

// ------------------------------------------------------------------ history
async function loadHistory() {
  $("historyList").innerHTML = '<div class="empty">Loading…</div>';
  const j = await api("/api/history");
  if (!j.ok) { $("historyList").innerHTML = `<div class="empty">${esc(j.message)}</div>`; return; }
  if (!j.items.length) { $("historyList").innerHTML = '<div class="empty">No history on the printer.</div>'; return; }
  $("historyList").innerHTML = j.items.map((h) => {
    const cls = h.status === 1 ? "good" : h.status === 2 ? "bad" : "";
    const tl = h.timelapse_url && h.timelapse_status === 1 ? `<a class="btn sm" target="_blank" href="/api/fetch?u=${encodeURIComponent(h.timelapse_url)}">🎞 Time-lapse</a>` : "";
    return `<div class="row-f" style="grid-template-columns:84px 1fr auto">${thumbHtml(h.thumbnail)}<div><div class="f-name">${esc(h.name)} <span class="chip ${cls}">${esc(h.status_text)}</span></div>
      <div class="f-meta"><span>Started <b>${fmtDate(h.begin)}</b></span><span>Duration <b>${fmtDur(h.duration)}</b></span><span>Layers printed <b>${h.layers_printed ?? "–"}</b></span>${h.volume_ml != null ? `<span>Resin <b>${h.volume_ml} ml</b></span>` : ""}${h.reason ? `<span>Reason <b>${esc(h.reason_text || h.reason)}</b></span>` : ""}</div></div><div class="f-act">${tl}</div></div>`;
  }).join("");
}
$("btnHistory").onclick = loadHistory;

// ------------------------------------------------------------------ camera
let camOn = false;
function camStart() {
  camOn = true; $("camMsg").textContent = "Connecting to camera…"; $("camMsg").hidden = false;
  const img = $("camImg"); img.hidden = false;
  img.onload = () => { $("camMsg").hidden = true; };
  img.onerror = async () => {
    if (!camOn) return;
    img.hidden = true; $("camMsg").hidden = false;
    let msg = "Camera unavailable";
    try { const r = await fetch("/snapshot.jpg"); if (!r.ok) msg = await r.text(); } catch { }
    $("camMsg").textContent = msg; camOn = false; render();
  };
  img.src = "/video?" + Date.now(); render();
}
function camStop() { camOn = false; const img = $("camImg"); img.removeAttribute("src"); img.hidden = true; $("camMsg").hidden = false; $("camMsg").textContent = "Camera is off. Press Start."; render(); }
$("camToggle").onclick = () => (camOn ? camStop() : camStart());
$("camFull").onclick = () => $("camBox").requestFullscreen && $("camBox").requestFullscreen();
$("camSnap").onclick = async () => {
  const r = await fetch("/snapshot.jpg");
  if (!r.ok) return toast(await r.text(), "bad");
  const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob());
  a.download = `saturn-${new Date().toISOString().replace(/[:.]/g, "-")}.jpg`; a.click();
};

// ------------------------------------------------------------------ controls
$("bPause").onclick = () => act("/api/pause", {}, "Pausing…");
$("bResume").onclick = () => act("/api/resume", {}, "Resuming…");
$("bStop").onclick = async () => { if (await confirmDialog("<h3>Stop the print?</h3><p>The print will be aborted and cannot be resumed.</p>", "Stop print", true)) act("/api/stop", {}, "Stopping…"); };
$("bSkip").onclick = () => act("/api/skip-preheat", {}, "Skipping preheat");
$("bFeed").onclick = () => act("/api/stop-feeding", {}, "Feeding stopped");
$("btnRefreshAll").onclick = async () => { await act("/api/refresh", {}); loadFiles(); };
$("btnRename").onclick = () => act("/api/rename", { name: $("inName").value }, "Renamed");
$("tlSwitch").onchange = () => act("/api/timelapse", { enable: $("tlSwitch").checked }, "Time-lapse " + ($("tlSwitch").checked ? "enabled" : "disabled"));
async function setLight(on) {
  $("lightNote").textContent = "Trying…";
  const j = await api("/api/light", { on });
  const tried = (j.results || []).map((r) => `${r.variant}: ${r.answered ? "Ack " + r.ack : "no answer"}`).join(", ");
  $("lightNote").textContent = tried;
  if (j.ok) toast("Light " + (on ? "on" : "off") + " (printer acknowledged)", "ok"); else toast(j.message || "Light command failed", "bad");
  return j;
}
$("btnLight").onclick = () => setLight(!(S && S.light === true));
$("lightOn").onclick = () => setLight(true);
$("lightOff").onclick = () => setLight(false);
const TW = { NormalExposureTime: { el: "twNormal", label: "Normal exposure", unit: "seconds", ms: true },
  BottomExposureTime: { el: "twBottom", label: "Bottom exposure", unit: "seconds", ms: true },
  BottomLayer: { el: "twLayers", label: "Bottom layers", unit: "layers", ms: false } };
function renderTweaks() {
  const pp = (S && S.print_params) || {};
  for (const [k, t] of Object.entries(TW)) {
    const v = pp[k];
    $(t.el).value = v == null ? "unknown" : t.ms ? (v / 1000) + " s" : String(v);
  }
}
$("tweaks").addEventListener("click", async (e) => {
  const key = e.target.dataset.tw; if (!key) return;
  const t = TW[key], cur = ((S && S.print_params) || {})[key];
  const d = await confirmDialog(`<h3>Edit ${t.label.toLowerCase()}</h3><p class="muted">Sent to the printer immediately when you press Apply. Value in ${t.unit}.</p><input class="input" id="twNew" type="number" min="0" step="${t.ms ? 0.1 : 1}" value="${cur == null ? "" : t.ms ? cur / 1000 : cur}" autofocus>`, "Apply");
  if (!d) return;
  const raw = $("twNew").value; if (raw === "") return toast("No value entered", "bad");
  const n = Number(raw); if (!isFinite(n) || n < 0) return toast("Invalid value", "bad");
  const value = t.ms ? Math.round(n * 1000) : Math.round(n);
  $("pxOut").textContent = "Sending…";
  const j = await api("/api/print-params", { params: { [key]: value } });
  $("pxOut").textContent = (j.results || []).map((r) => `${r.key}=${r.value}: ${r.answered ? "Ack " + r.ack : "no answer"}`).join("   ") || j.message || "";
  toast(j.ok ? `${t.label} changed` : (j.message || "Failed"), j.ok ? "ok" : "bad");
});
$("camRelease").onclick = async () => { camStop(); await act("/api/camera/release", {}, "Camera released"); };
$("btnDiscover").onclick = async () => {
  $("discoverOut").textContent = "Scanning…";
  const j = await api("/api/discover", {});
  $("discoverOut").textContent = j.printers && j.printers.length ? j.printers.map((p) => `${p.Name || p.MachineName} @ ${p.MainboardIP} (${p.FirmwareVersion || "?"})`).join("; ") : "No printers answered";
};

// ------------------------------------------------------------------ console
const logEntries = [];
const lv = $("logView");
const hhmmss = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false }) + "." + String(Math.floor((t % 1) * 1000)).padStart(3, "0");
function logVisible(e) {
  if (e.kind === "change" && $("lgHideState").checked && /PrintInfo\.Status: /.test(e.text)) return false;
  if (e.kind === "status" || e.kind === "attributes") return $("lgStatus").checked;
  if (e.kind === "debug") return $("lgDebug").checked;
  return true;
}
function logLine(e) {
  const arrow = e.dir === "out" ? "→" : e.dir === "in" ? "←" : "•";
  const cls = e.kind === "other" ? "other" : e.kind === "change" ? "change" : e.kind === "error" ? "error" : e.kind === "debug" ? "debug" : e.dir === "app" ? "app" : e.kind === "status" || e.kind === "attributes" ? "status" : (e.dir === "out" ? "sent" : "recv");
  const d = document.createElement("div"); d.className = cls;
  const t = document.createElement("span"); t.className = "t"; t.textContent = hhmmss(e.t) + " ";
  d.append(t, document.createTextNode(arrow + " " + e.text));
  if (e.msg && ($("lgRaw").checked || e.kind === "other")) { const p = document.createElement("pre"); p.textContent = JSON.stringify(e.msg, null, 1); d.append(p); }
  return d;
}
function logAdd(entries, init) {
  const stick = lv.scrollTop + lv.clientHeight >= lv.scrollHeight - 30;
  for (const e of entries) { logEntries.push(e); if (logVisible(e)) lv.appendChild(logLine(e)); }
  while (logEntries.length > 1500) logEntries.shift();
  while (lv.childElementCount > 1500) lv.firstChild.remove();
  if (stick || init) lv.scrollTop = lv.scrollHeight;
}
function logRebuild() { lv.textContent = ""; for (const e of logEntries) if (logVisible(e)) lv.appendChild(logLine(e)); lv.scrollTop = lv.scrollHeight; }
["lgStatus", "lgHideState", "lgDebug", "lgRaw"].forEach((id) => $(id).addEventListener("change", logRebuild));
$("lgClear").onclick = () => { logEntries.length = 0; lv.textContent = ""; };
const logText = () => logEntries.filter(logVisible).map((e) => `${new Date(e.t * 1000).toISOString()} ${e.dir === "out" ? "->" : e.dir === "in" ? "<-" : "**"} ${e.text}` + (e.msg && $("lgRaw").checked ? " " + JSON.stringify(e.msg) : "")).join("\n");
$("lgCopy").onclick = () => navigator.clipboard.writeText(logText()).then(() => toast("Log copied", "ok"), () => toast("Copy failed", "bad"));
$("lgSave").onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([logText()], { type: "text/plain" })); a.download = "saturn-log.txt"; a.click(); };
const hex32 = () => [...crypto.getRandomValues(new Uint8Array(16))].map((x) => x.toString(16).padStart(2, "0")).join("");
function envelope(cmd, data, extra) {
  const mb = S ? S.mainboard_id : "";
  return JSON.stringify({ Id: S ? S.brand_id : "", Data: { Cmd: cmd, ...(extra || {}), Data: data || {}, RequestID: hex32(), MainboardID: mb, TimeStamp: Math.floor(Date.now() / 1000), From: 0 }, Topic: "sdcp/request/" + mb }, null, 1);
}
const machineName = () => (S && S.attributes && S.attributes.MachineName) || "Saturn 4 Ultra 16K";
const RAW_PRESETS = {
  "Cmd 0 - status": () => envelope(0),
  "Cmd 1 - attributes": () => envelope(1),
  "Light ON (151)": () => envelope(151, { ControlState: 1 }),
  "Light OFF (151)": () => envelope(151, { ControlState: 0 }),
  "Light ON with Matrix fields": () => envelope(151, { ControlState: 1 }, { Belong: "LCD", ProVer: "0.0.1", MachineType: machineName() }),
  "Normal exposure 1.7 s (135)": () => envelope(135, { NormalExposureTime: 1700.0 }),
  "Bottom exposure 45 s (135)": () => envelope(135, { BottomExposureTime: 45000.0 }),
  "Bottom layers 7 (135)": () => envelope(135, { BottomLayer: 7 }),
  "Pause (129)": () => envelope(129),
  "Resume (131)": () => envelope(131),
  "File list /local/ (258)": () => envelope(258, { Url: "/local/" }),
  "Plain text: ping": () => "ping",
};
$("rawPreset").innerHTML += Object.keys(RAW_PRESETS).map((k) => `<option>${esc(k)}</option>`).join("");
$("rawPreset").onchange = () => { const f = RAW_PRESETS[$("rawPreset").value]; if (f) $("rawLine").value = f(); $("rawPreset").selectedIndex = 0; $("rawLine").focus(); };
$("rawFill").onclick = () => { const n = $("rawCmd").value; if (n === "") return toast("Type a command number first", "bad"); $("rawLine").value = envelope(Number(n)); $("rawLine").focus(); };
async function sendRaw() {
  const text = $("rawLine").value;
  if (!text.trim()) return;
  const j = await api("/api/raw", { text });
  if (!j.ok) return toast(j.message || "Send failed", "bad");
  if (j.reply) toast("Printer replied: " + (j.reply.Data && j.reply.Data.Ack != null ? "Ack " + j.reply.Data.Ack : "see Console"), "ok");
  else toast(j.note || "Sent", "");
}
$("rawSend").onclick = sendRaw;
$("rawLine").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendRaw(); } });

// ------------------------------------------------------------------ explorer (printer's HTTP file server)
let exLoaded = false, exCur = "/", exHist = [], exPos = -1, exEntries = [], exSort = { key: "name", asc: true };
const exCache = new Map(), exOpen = new Set(["/"]);
const EX_QUICK = [["Print files", "/media/mmcblk0p3/"], ["Thumbnails + file_info", "/media/mmcblk0p1/file_info/"], ["Logs", "/media/mmcblk0p2/"], ["Config", "/config/"], ["Customer", "/customer/"], ["Root", "/"]];
const TEXT_EXT = /\.(txt|log|ini|conf|cfg|json|xml|csv|md|sh|properties|yaml|yml|lua|py|js|html|htm|css)$/i, IMG_EXT = /\.(bmp|png|jpe?g|gif)$/i;
const exUrl = (p, dl) => "/api/fetch?u=" + encodeURIComponent(p) + (dl ? "&download=1" : "");
async function exList(path, fresh) {
  if (!fresh && exCache.has(path)) return exCache.get(path);
  const j = await api("/api/http/list?path=" + encodeURIComponent(path));
  if (j.ok) exCache.set(path, j);
  return j;
}
function exStart() {
  exLoaded = true;
  $("exQuick").innerHTML = EX_QUICK.map(([n, p]) => `<button class="btn sm" data-q="${esc(p)}">${esc(n)}</button>`).join("");
  exGo("/media/mmcblk0p3/");
}
async function exGo(path, mode) {          // mode: undefined = new navigation, "hist" = back/forward, "fresh" = refresh
  $("exList").innerHTML = '<div class="empty">Loading…</div>';
  const j = await exList(path, mode === "fresh");
  if (!j.ok) { $("exList").innerHTML = `<div class="empty">${esc(j.message)}</div>`; exEntries = []; $("exInfo").textContent = ""; return; }
  exCur = j.path;
  if (!mode) { exHist = exHist.slice(0, exPos + 1); if (exHist[exHist.length - 1] !== exCur) exHist.push(exCur); exPos = exHist.length - 1; }
  exEntries = j.entries;
  exOpenTo(exCur);
  renderEx(j);
}
function exOpenTo(path) { let acc = "/"; exOpen.add("/"); for (const seg of path.split("/").filter(Boolean)) { acc += seg + "/"; exOpen.add(acc); } renderTree(); }
function renderEx(j) {
  $("exBack").disabled = exPos <= 0; $("exFwd").disabled = exPos >= exHist.length - 1; $("exUp").disabled = exCur === "/";
  const segs = exCur.split("/").filter(Boolean); let acc = "/";
  $("exCrumbs").innerHTML = `<a data-p="/">printer</a>` + segs.map((sg) => { acc += sg + "/"; return ` / <a data-p="${esc(acc)}">${esc(sg)}</a>`; }).join("");
  const k = exSort.key, dir = exSort.asc ? 1 : -1;
  const list = [...exEntries].sort((x, y) => (x.is_dir === y.is_dir ? 0 : x.is_dir ? -1 : 1) || dir * (k === "name" ? x.name.localeCompare(y.name, undefined, { numeric: true, sensitivity: "base" }) : ((x[k] ?? -1) - (y[k] ?? -1))));
  document.querySelectorAll("#exHead span[data-sort]").forEach((e) => { e.className = e.dataset.sort === k ? "sorted" + (exSort.asc ? " asc" : "") : ""; });
  $("exList").innerHTML = list.length ? list.map((e) => {
    const i = exEntries.indexOf(e);
    const when = e.mtime ? fmtDate(e.mtime) : "–";
    const acts = e.is_dir ? "" : (e.readable
      ? `${TEXT_EXT.test(e.name) || IMG_EXT.test(e.name) ? `<button class="btn sm" data-view="${i}" title="View">👁</button>` : ""}<a class="btn sm" href="${esc(exUrl(e.path, true))}" title="Download">⬇</a>`
      : "");
    return `<div class="ex-row"><div class="ex-name ${e.is_dir ? "dir" : ""}" ${e.is_dir ? `data-go="${i}"` : ""}>${e.is_dir ? "📁" : "🗎"} ${esc(e.name)}</div><div class="when">${when}</div><div class="num">${e.is_dir ? "" : e.size == null ? "–" : fmtSize(e.size)}</div><div class="ex-act">${acts}</div></div>`;
  }).join("") : '<div class="empty">Empty folder.</div>';
  const files = exEntries.filter((e) => !e.is_dir), tot = files.reduce((a, e) => a + (e.size || 0), 0);
  $("exInfo").textContent = `${exEntries.length - files.length} folder(s), ${files.length} file(s), ${fmtSize(tot)}` + (j.virtual ? " · virtual system folder: viewing and downloading files here is blocked" : "") + (j.note ? " · " + j.note : "");
}
function renderTree() {
  const node = (path, name, depth) => {
    const open = exOpen.has(path), cached = exCache.get(path);
    let h = `<div class="tn ${path === exCur ? "cur" : ""}" data-tp="${esc(path)}" style="padding-left:${4 + depth * 2}px"><span class="tw" data-tt="${esc(path)}">${open ? "▾" : "▸"}</span><span data-tg="${esc(path)}">📁 ${esc(name)}</span></div>`;
    if (open && cached) h += '<div class="tkids">' + cached.entries.filter((e) => e.is_dir).map((e) => node(e.path, e.name, depth + 1)).join("") + "</div>";
    return h;
  };
  $("exTree").innerHTML = node("/", "printer", 0);
}
$("exTree").addEventListener("click", async (e) => {
  const t = e.target.dataset;
  if (t.tt != null) {
    if (exOpen.has(t.tt) && t.tt !== "/") exOpen.delete(t.tt); else { exOpen.add(t.tt); await exList(t.tt); }
    renderTree();
  } else if (t.tg != null) exGo(t.tg);
});
$("exCrumbs").addEventListener("click", (e) => { const a = e.target.closest("a"); if (a) exGo(a.dataset.p); });
$("exQuick").addEventListener("click", (e) => { if (e.target.dataset.q) exGo(e.target.dataset.q); });
$("exBack").onclick = () => { if (exPos > 0) { exPos--; exGo(exHist[exPos], "hist"); } };
$("exFwd").onclick = () => { if (exPos < exHist.length - 1) { exPos++; exGo(exHist[exPos], "hist"); } };
$("exUp").onclick = () => { const p = exCur.replace(/\/+$/, ""); exGo(p.slice(0, p.lastIndexOf("/") + 1) || "/"); };
$("exRefresh").onclick = async () => { exCache.delete(exCur); exCache.forEach((v, k) => { if (k !== exCur && exOpen.has(k)) exCache.delete(k); }); await exGo(exCur, "fresh"); for (const p of exOpen) if (!exCache.has(p)) await exList(p); renderTree(); };
$("exHead").addEventListener("click", (e) => { const k = e.target.dataset.sort; if (!k) return; exSort = { key: k, asc: exSort.key === k ? !exSort.asc : true }; renderEx({ virtual: false }); });
$("exList").addEventListener("click", async (e) => {
  const d = e.target.dataset;
  if (d.go != null) return exGo(exEntries[d.go].path);
  if (d.view == null) return;
  const en = exEntries[d.view];
  const dlg = ensureViewer(); $("vwTitle").textContent = en.path; $("vwBody").innerHTML = "Loading…"; dlg.showModal();
  if (IMG_EXT.test(en.name)) { $("vwBody").innerHTML = `<img src="${esc(exUrl(en.path))}" style="max-width:100%;max-height:60vh;display:block;margin:10px auto">`; return; }
  try {
    const r = await fetch(exUrl(en.path), { headers: { Range: "bytes=0-262143" } });
    const txt = await r.text();
    const pre = document.createElement("pre"); pre.textContent = r.ok || r.status === 206 ? txt : "Could not read: " + txt;
    $("vwBody").textContent = ""; $("vwBody").append(pre);
    if (en.size > 262144) { const n = document.createElement("div"); n.className = "muted"; n.textContent = `Showing the first 256 KB of ${fmtSize(en.size)}. Use download for the whole file.`; $("vwBody").append(n); }
  } catch (err) { $("vwBody").textContent = "Could not read the file: " + err; }
});
function ensureViewer() {
  let d = $("viewDlg");
  if (!d) {
    d = document.createElement("dialog"); d.id = "viewDlg"; d.className = "viewer";
    d.innerHTML = '<h3 id="vwTitle" class="mono" style="word-break:break-all"></h3><div id="vwBody"></div><menu><button class="btn" id="vwClose">Close</button></menu>';
    document.body.appendChild(d); d.querySelector("#vwClose").onclick = () => d.close();
  }
  return d;
}

// ------------------------------------------------------------------ boot
fetch("/api/config").then((r) => r.json()).then((c) => {
  cfgLight = !!c.light_enabled; if (S) render();
});
connectWS();
fetch("/api/state").then((r) => r.json()).then((s) => { S = s; render(); });
loadFiles();
