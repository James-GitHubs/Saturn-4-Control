"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let S = null;                        // last printer state
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

  // light + timelapse
  $("btnLight").classList.toggle("on", S.light === true);
  $("btnLight").textContent = S.light === true ? "💡 Light on" : "💡 Light";
  $("lightNote").textContent = S.light == null ? "Current state unknown until first toggled from here" : "";
  const tl = (S.status || {}).TimeLapseStatus;
  if (tl != null && document.activeElement !== $("tlSwitch")) $("tlSwitch").checked = !!tl;

  renderInfo(a);
  renderUpload(S.upload);
  renderMessages();
  $("camToggle").textContent = camOn ? "Stop" : "Start";
  $("btnDeleteSel").disabled = !selected.size;
}
function kv(k, v) { return `<div><label>${esc(k)}</label><b>${esc(v ?? "–")}</b></div>`; }
function renderInfo(a) {
  const st = S.status || {};
  $("machineStats").innerHTML = [
    kv("UV LED temp", st.TempOfUVLED != null ? Number(st.TempOfUVLED).toFixed(2) + " °C" : null),
    kv("Chamber temp", st.TempOfBox != null ? Number(st.TempOfBox).toFixed(2) + " °C" : null),
    kv("Target chamber", st.TempTargetBox ? st.TempTargetBox + " °C" : null),
    kv("Screen use", st.PrintScreen != null ? fmtDur(st.PrintScreen) : null),
    kv("Release film cycles", st.ReleaseFilm != null ? `${st.ReleaseFilm}${a.ReleaseFilmMax ? " / " + a.ReleaseFilmMax : ""}` : null),
    kv("Time-lapse", st.TimeLapseStatus ? "On" : "Off"),
  ].join("");
  const d = a.DevicesStatus || {};
  const ok = (v, labels) => labels[v] ?? v;
  $("deviceStats").innerHTML = [
    kv("UV LED sensor", ok(d.TempSensorStatusOfUVLED, ["Disconnected", "Normal", "Abnormal"])),
    kv("LCD", ok(d.LCDStatus, ["Disconnected", "Connected"])),
    kv("Strain gauge", ok(d.SgStatus, ["Disconnected", "Normal", "Calibration failed"])),
    kv("Z motor", ok(d.ZMotorStatus, ["Disconnected", "Connected"])),
    kv("X motor", ok(d.XMotorStatus, ["Disconnected", "Connected"])),
    kv("Rotary motor", ok(d.RotateMotorStatus, ["Disconnected", "Connected"])),
    kv("Release film", ok(d.RelaseFilmState, ["Abnormal", "Normal"])),
    kv("Camera", ok(a.CameraStatus, ["Disconnected", "Connected"])),
  ].join("");
  $("attrStats").innerHTML = [
    kv("Model", a.MachineName), kv("Brand", a.BrandName), kv("Firmware", a.FirmwareVersion),
    kv("Protocol", a.ProtocolVersion), kv("Resolution", a.Resolution), kv("Build volume (mm)", a.XYZsize),
    kv("IP", a.MainboardIP), kv("Mainboard ID", S.mainboard_id), kv("Network", a.NetworkStatus),
    kv("USB drive", a.UsbDiskStatus ? "Connected" : "Not connected"),
    kv("Camera streams", a.MaximumVideoStreamAllowed != null ? `${a.NumberOfVideoStreamConnected} / ${a.MaximumVideoStreamAllowed}` : null),
    kv("File types", (a.SupportFileType || []).join(", ")), kv("Capabilities", (a.Capabilities || []).join(", ")),
    kv("Max UV LED temp", a.TempOfUVLEDMax ? a.TempOfUVLEDMax + " °C" : null),
    kv("Free space", a.RemainingMemory ? fmtSize(a.RemainingMemory / 8) : null),
    kv("Time-lapse start/height/interval", a.TLPStartCapPos != null ? `${a.TLPStartCapPos}mm / ${a.TLPNoCapPos}mm / ${a.TLPInterLayers} layers` : null),
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
  if (b.dataset.tab === "explorer" && !exLoaded) exGo($("exPath").value);
});

// ------------------------------------------------------------------ files
async function loadFiles() {
  $("fileList").innerHTML = '<div class="empty">Loading…</div>';
  const j = await api("/api/files?path=" + encodeURIComponent(curPath));
  if (!j.ok) { $("fileList").innerHTML = `<div class="empty">${esc(j.message)}</div>`; return; }
  fileItems = j.files; selected.clear();
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
  const items = fileItems.filter((f) => f.name.toLowerCase().includes(q));
  if (!items.length) { $("fileList").innerHTML = '<div class="empty">No files here.</div>'; return; }
  $("fileList").innerHTML = items.map((f, i) => {
    const idx = fileItems.indexOf(f);
    if (f.is_dir) return `<div class="row-f"><input type="checkbox" data-i="${idx}"><div class="thumb">📁</div><div><div class="f-name dir" data-open="${idx}">${esc(f.name)}</div><div class="f-meta">Folder</div></div><div class="f-act"></div></div>`;
    const meta = [];
    if (f.layer_height != null) meta.push(`<span>Layer height <b>${Number(f.layer_height).toFixed(2)} mm</b></span>`);
    if (f.layers != null) meta.push(`<span>Layers <b>${f.layers}</b></span>`);
    if (f.est_seconds != null) meta.push(`<span>Est. time <b>${fmtDur(f.est_seconds)}</b></span>`);
    if (f.size != null) meta.push(`<span>Size <b>${fmtSize(f.size)}</b></span>`);
    if (f.create_time) meta.push(`<span>Created <b>${esc(typeof f.create_time === "number" ? (ago(f.create_time) + " · " + fmtDate(f.create_time)) : f.create_time)}</b></span>`);
    meta.push(f.last_printed ? `<span>Last printed <b>${ago(f.last_printed)}</b>${f.print_count > 1 ? ` (${f.print_count}×)` : ""}</span>` : `<span>Never printed</span>`);
    return `<div class="row-f"><input type="checkbox" data-i="${idx}">${thumbHtml(f.thumbnail)}<div><div class="f-name">${esc(f.name)}</div><div class="f-meta">${meta.join("")}</div></div>
      <div class="f-act"><button class="btn sm primary" data-print="${idx}">▶ Print</button><button class="btn sm danger" data-del="${idx}">🗑</button></div></div>`;
  }).join("");
}
$("fileFilter").addEventListener("input", renderFiles);
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
async function setLight(on) { const j = await act("/api/light", { on }, "Light " + (on ? "on" : "off")); return j; }
$("btnLight").onclick = () => setLight(!(S && S.light === true));
$("lightOn").onclick = () => setLight(true);
$("lightOff").onclick = () => setLight(false);
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
["lgStatus", "lgDebug", "lgRaw"].forEach((id) => $(id).addEventListener("change", logRebuild));
$("lgClear").onclick = () => { logEntries.length = 0; lv.textContent = ""; };
const logText = () => logEntries.filter(logVisible).map((e) => `${new Date(e.t * 1000).toISOString()} ${e.dir === "out" ? "->" : e.dir === "in" ? "<-" : "**"} ${e.text}` + (e.msg && $("lgRaw").checked ? " " + JSON.stringify(e.msg) : "")).join("\n");
$("lgCopy").onclick = () => navigator.clipboard.writeText(logText()).then(() => toast("Log copied", "ok"), () => toast("Copy failed", "bad"));
$("lgSave").onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([logText()], { type: "text/plain" })); a.download = "saturn-log.txt"; a.click(); };
async function sendRaw() {
  const m = $("rawLine").value.trim().match(/^(\d+)\s*(.*)$/);
  if (!m) return toast("Format: <cmd number> [JSON data]", "bad");
  let data = {}; if (m[2]) { try { data = JSON.parse(m[2]); } catch { return toast("Data is not valid JSON", "bad"); } }
  const j = await api("/api/raw", { cmd: +m[1], data });
  if (!j.ok) toast(j.message, "bad");
  $("rawLine").value = "";
}
$("rawSend").onclick = sendRaw;
$("rawLine").addEventListener("keydown", (e) => { if (e.key === "Enter") sendRaw(); });

// ------------------------------------------------------------------ explorer
let exLoaded = false; const exReport = [];
const joinPath = (base, name) => (base.replace(/\/+$/, "") + "/" + name.replace(/^\/+/, ""));
async function exGo(path) {
  exLoaded = true; $("exPath").value = path;
  $("exList").innerHTML = '<div class="empty">Loading…</div>';
  const j = await api("/api/explore?path=" + encodeURIComponent(path));
  exReport.push(`LIST ${path}\n${JSON.stringify(j, null, 1)}`);
  $("exRaw").textContent = JSON.stringify(j, null, 2);
  const list = (j.reply && j.reply.FileList) || [];
  if (!j.ok && !list.length) { $("exList").innerHTML = `<div class="empty">${esc(j.message || "Printer refused this path (Ack " + (j.reply && j.reply.Ack) + ")")}</div>`; return; }
  if (!list.length) { $("exList").innerHTML = '<div class="empty">Empty.</div>'; return; }
  $("exList").innerHTML = list.map((e, i) => {
    const dir = e.type === 0 || e.type === "0"; const nm = e.name || e.Name || "?";
    const extra = Object.keys(e).filter((k) => !["name", "type"].includes(k)).map((k) => `${esc(k)}=${esc(JSON.stringify(e[k]))}`).join("  ");
    return `<div class="row-f" style="grid-template-columns:28px 1fr auto"><div>${dir ? "📁" : "🗎"}</div><div><div class="f-name ${dir ? "dir" : ""}" ${dir ? `data-go="${i}"` : ""}>${esc(nm)}</div><div class="f-meta mono">${extra || "type=" + e.type}</div></div><div class="f-act">${dir ? "" : `<button class="btn sm" data-probe="${i}">Probe HTTP</button>`}</div></div>`;
  }).join("");
  $("exList")._list = list;
}
$("exList").addEventListener("click", (e) => {
  const l = $("exList")._list || [];
  if (e.target.dataset.go != null) exGo((l[e.target.dataset.go].name || "").replace(/\/?$/, "/"));
  if (e.target.dataset.probe != null) { $("prPath").value = l[e.target.dataset.probe].name; prGo(); }
});
$("exGo").onclick = () => exGo($("exPath").value);
$("exPath").addEventListener("keydown", (e) => { if (e.key === "Enter") exGo($("exPath").value); });
$("exUp").onclick = () => { const p = $("exPath").value.replace(/\/+$/, ""); exGo(p.slice(0, p.lastIndexOf("/") + 1) || "/"); };
$("exQuick").innerHTML = ["/", "/local/", "/usb/", "/local/.", "/local/..", "/sdcard/", "/mnt/", "/etc/"].map((p) => `<button class="btn sm" data-q="${p}">${p}</button>`).join("");
$("exQuick").addEventListener("click", (e) => { if (e.target.dataset.q) exGo(e.target.dataset.q); });
async function prGo() {
  const j = await api(`/api/probe?port=${$("prPort").value}&path=${encodeURIComponent($("prPath").value)}`);
  exReport.push(`PROBE ${$("prPort").value} ${$("prPath").value}\n${JSON.stringify(j, null, 1)}`);
  $("prOut").textContent = JSON.stringify({ ...j, links: undefined }, null, 2);
  $("prLinks").innerHTML = (j.links || []).map((l, i) => `<button class="btn sm" data-l="${i}">${esc(l)}</button>`).join("");
  $("prLinks")._links = j.links || []; $("prLinks")._base = $("prPath").value;
}
$("prLinks").addEventListener("click", (e) => {
  if (e.target.dataset.l == null) return;
  const l = $("prLinks")._links[e.target.dataset.l], base = $("prLinks")._base;
  $("prPath").value = l.startsWith("/") ? l : base.replace(/\/?$/, "/") + l; prGo();
});
$("prGo").onclick = prGo;
$("exReport").onclick = () => navigator.clipboard.writeText(exReport.join("\n\n")).then(() => toast("Report copied (" + exReport.length + " results)", "ok"), () => toast("Copy failed", "bad"));

// ------------------------------------------------------------------ boot
fetch("/api/config").then((r) => r.json()).then((c) => {
  if (!c.light_enabled) { $("btnLight").classList.add("hidden"); document.querySelectorAll(".lightRow").forEach((e) => e.classList.add("hidden")); }
});
connectWS();
fetch("/api/state").then((r) => r.json()).then((s) => { S = s; render(); });
loadFiles();
