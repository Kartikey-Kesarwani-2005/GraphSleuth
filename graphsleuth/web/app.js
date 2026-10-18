/* GraphSleuth console — no dependencies, no CDN (works offline). */
"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("graph");
const ctx = canvas.getContext("2d");

/* ------------------------------------------------------------------ *
 * graph state: nodes keyed by id, edges as {from,to,type}
 * ------------------------------------------------------------------ */
const nodes = new Map();   // id -> {id,label,kind,x,y,vx,vy}
const edges = [];          // {from,to,type}
let focal = "";
const ring = new Set();
let highlight = null;

const LABEL_COLOR = {
  Account: "#4cc9f0", Device: "#b085ff", Transaction: "#37d67a", Person: "#7aa2ff",
  Merchant: "#ffa94d", Alert: "#ff6b6b", Case: "#ffd43b", MemoryNote: "#8a93a8",
  System: "#ffa94d", Project: "#c0e36b", Team: "#e0aaff", Doc: "#63d2c4",
  Ticket: "#f7b267", Chat: "#9be7ff", Decision: "#ffd166",
};

function color(node) {
  if (node.kind === "focal") return "#ff5c7a";
  if (node.kind === "ring" || ring.has(node.id)) return "#ffb020";
  return LABEL_COLOR[node.label] || "#6b7a99";
}

function addNode(id, label) {
  if (!id || id === "__ring__") return null;
  let n = nodes.get(id);
  if (!n) {
    const r = 260, a = Math.random() * Math.PI * 2;
    n = { id, label: label || id.split("-")[0], kind: "normal", x: Math.cos(a) * r, y: Math.sin(a) * r, vx: 0, vy: 0 };
    nodes.set(id, n);
  } else if (label && (!n.label || n.label === id.split("-")[0])) {
    n.label = label;
  }
  return n;
}

function addEdge(from, to, type) {
  if (!from || !to) return;
  addNode(from);
  addNode(to);
  const key = from + "|" + to + "|" + type;
  if (!edges.some((e) => e.from + "|" + e.to + "|" + e.type === key)) edges.push({ from, to, type });
}

/* Incrementally grow the graph from streamed tool results (mirrors explain.facts_from_entry). */
function ingest(tool, r) {
  if (!r || typeof r !== "object") return;
  if (tool === "graph_neighbors") {
    addNode(r.start, r.start ? r.start.split("-")[0] : "");
    (r.nodes || []).forEach((n) => addNode(String(n.id), (n.labels || [])[0]));
    (r.edges || []).forEach((e) => addEdge(String(e.from), String(e.to), String(e.type)));
  } else if (tool === "trace_money_flow") {
    ["outgoing", "incoming"].forEach((dir) => {
      (((r[dir] || {}).levels) || []).forEach((lvl) => (lvl.hops || []).forEach((h) => {
        addNode(h.from, "Account"); addNode(h.to, "Account"); addEdge(String(h.from), String(h.to), String(h.tx));
      }));
    });
  } else if (tool === "find_shared_links") {
    addNode(r.account, "Account");
    (r.links || []).forEach((l) => {
      addNode(l.account, "Account");
      (l.shared_devices || []).forEach((d) => { addNode(d, "Device"); addEdge(l.account, d, "SHARED_DEVICE"); });
    });
  } else if (tool === "shortest_connection") {
    const p = r.path || [];
    p.forEach((s) => addNode(String(s.node)));
    for (let i = 0; i < p.length - 1; i++) addEdge(String(p[i].node), String(p[i + 1].node), String(p[i + 1].via));
  } else if (tool === "detect_suspicious_cluster") {
    (r.clusters || []).forEach((c) => (c.members || []).forEach((m) => { addNode(m.account, "Account"); ring.add(m.account); }));
  } else if (tool === "what_if_removed") {
    addNode(r.entity, r.label || String(r.entity || "").split("-")[0]);
    (r.neighbours || []).forEach((n) => {
      if (!n.id) return;
      addNode(String(n.id), n.label || "");
      addEdge(String(n.id), String(r.entity), String(n.via || "LINKED"));
    });
    const mp = ((r.path || {}).money_path_before) || [];
    mp.forEach((s) => addNode(String(s.node)));
    for (let i = 0; i < mp.length - 1; i++) addEdge(String(mp[i].node), String(mp[i + 1].node), String(mp[i + 1].via));
  } else if (tool === "who_knows") {
    addNode(r.system, "System");
    (r.people || []).forEach((p) => { addNode(p.person, "Person"); addEdge(p.person, r.system, "WORKED_ON"); });
    (r.declared_experts || []).forEach((e) => { addNode(e.person, "Person"); addEdge(e.person, r.system, "KNOWS"); });
  } else if (tool === "owners_of") {
    addNode(r.entity);
    let firstOwner = "";
    (r.owners || []).forEach((o) => { addNode(o.id, o.kind || "Team"); addEdge(o.id, r.entity, "OWNS"); firstOwner = firstOwner || o.id; });
    (r.reachable_people || []).forEach((p) => { addNode(p.person, "Person"); if (firstOwner) addEdge(p.person, firstOwner, "MEMBER_OF"); });
  } else if (tool === "blast_radius") {
    addNode(r.system, "System");
    (r.affected_systems || []).forEach((s) => { addNode(s.id, "System"); addEdge(s.id, r.system, "DEPENDS_ON"); });
    (r.projects_at_risk || []).forEach((p) => { addNode(p.project, "Project"); addEdge(p.project, r.system, "AT_RISK"); });
    (r.teams_at_risk || []).forEach((t) => { addNode(t.team, "Team"); addEdge(t.team, r.system, "OWNS"); });
  } else if (tool === "decision_trail") {
    if (r.found) {
      addNode(r.decision, "Decision");
      (r.decided_by || []).forEach((d) => { addNode(d.person, "Person"); addEdge(d.person, r.decision, "DECIDED_BY"); });
      (r.affects || []).forEach((a) => { addNode(a.system, "System"); addEdge(r.decision, a.system, "AFFECTS"); });
      (r.sources || []).forEach((s) => { addNode(s.id, s.kind || "Doc"); addEdge(r.decision, s.id, "SOURCED_FROM"); });
      (r.supersedes || []).forEach((o) => { addNode(o.decision, "Decision"); addEdge(r.decision, o.decision, "SUPERSEDES"); });
    }
  } else if (tool === "knowledge_sources") {
    addNode(r.system, "System");
    (r.docs || []).forEach((d) => { addNode(d.id, "Doc"); addEdge(d.id, r.system, "ABOUT"); });
    (r.tickets || []).forEach((t) => { addNode(t.id, "Ticket"); addEdge(t.id, r.system, "ABOUT"); });
    (r.chats || []).forEach((c) => { addNode(c.id, "Chat"); addEdge(c.id, r.system, "MENTIONS"); });
  } else if (tool === "stale_knowledge") {
    (r.stale_docs || []).forEach((d) => addNode(d.id, "Doc"));
    (r.superseded_decisions || []).forEach((p) => { addNode(p.replaced_by, "Decision"); addNode(p.old, "Decision"); addEdge(p.replaced_by, p.old, "SUPERSEDES"); });
    (r.bus_factor_risks || []).forEach((b) => addNode(b.system, "System"));
  }
}

/* ------------------------------------------------------------------ *
 * force-directed layout (O(n^2), fine for a few hundred nodes)
 * ------------------------------------------------------------------ */
let W = 0, H = 0, DPR = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
let view = { scale: 1, tx: 0, ty: 0 };

function resize() {
  const rect = canvas.getBoundingClientRect();
  W = rect.width; H = rect.height;
  canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}

function simulate() {
  const list = [...nodes.values()];
  const n = list.length;
  if (n > 1) {
    for (let i = 0; i < n; i++) {
      const a = list[i];
      for (let j = i + 1; j < n; j++) {
        const b = list[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy || 0.01;
        if (d2 > 90000) continue;
        const f = 1400 / d2;
        const d = Math.sqrt(d2);
        const ux = dx / d, uy = dy / d;
        a.vx += ux * f; a.vy += uy * f;
        b.vx -= ux * f; b.vy -= uy * f;
      }
    }
  }
  const fmap = new Map(list.map((x) => [x.id, x]));
  for (const e of edges) {
    const a = fmap.get(e.from), b = fmap.get(e.to);
    if (!a || !b) continue;
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - 95) * 0.012;
    const ux = dx / d, uy = dy / d;
    a.vx += ux * f; a.vy += uy * f;
    b.vx -= ux * f; b.vy -= uy * f;
  }
  for (const nd of list) {
    nd.vx -= nd.x * 0.0016; nd.vy -= nd.y * 0.0016;   // gravity to centre
    nd.vx *= 0.86; nd.vy *= 0.86;
    nd.x += nd.vx; nd.y += nd.vy;
  }
}

function fit() {
  if (!nodes.size) return;
  let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
  for (const nd of nodes.values()) {
    minX = Math.min(minX, nd.x); maxX = Math.max(maxX, nd.x);
    minY = Math.min(minY, nd.y); maxY = Math.max(maxY, nd.y);
  }
  const pad = 70;
  const sw = (W - pad * 2) / Math.max(1, maxX - minX);
  const sh = (H - pad * 2) / Math.max(1, maxY - minY);
  view.scale = Math.max(0.25, Math.min(1.6, Math.min(sw, sh)));
  view.tx = W / 2 - ((minX + maxX) / 2) * view.scale;
  view.ty = H / 2 - ((minY + maxY) / 2) * view.scale;
}

function draw() {
  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(view.tx, view.ty);
  ctx.scale(view.scale, view.scale);

  const fmap = new Map([...nodes.values()].map((x) => [x.id, x]));
  const active = highlight ? new Set([highlight.id, ...neighbours(highlight.id)]) : null;

  for (const e of edges) {
    const a = fmap.get(e.from), b = fmap.get(e.to);
    if (!a || !b) continue;
    const on = !active || (active.has(e.from) && active.has(e.to));
    ctx.strokeStyle = on ? "rgba(120,160,220,0.42)" : "rgba(80,100,140,0.12)";
    ctx.lineWidth = (active && on) ? 1.6 : 1;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }

  ctx.font = "10px 'Segoe UI', sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  for (const nd of nodes.values()) {
    const isFocal = nd.id === focal;
    const r = isFocal ? 11 : nd.kind === "ring" || ring.has(nd.id) ? 8 : 6;
    const dim = active && !active.has(nd.id);
    ctx.beginPath();
    ctx.arc(nd.x, nd.y, r, 0, Math.PI * 2);
    ctx.fillStyle = dim ? "rgba(90,105,135,0.35)" : color(nd);
    ctx.fill();
    if (isFocal || nd.kind === "ring" || ring.has(nd.id) || (highlight && highlight.id === nd.id)) {
      ctx.strokeStyle = "rgba(255,255,255,0.85)"; ctx.lineWidth = 1.4; ctx.stroke();
    }
    if (view.scale > 0.55 && !dim) {
      ctx.fillStyle = "rgba(219,228,245,0.82)";
      ctx.fillText(nd.id, nd.x, nd.y - r - 8);
    }
  }
  ctx.restore();
}

function neighbours(id) {
  const out = [];
  for (const e of edges) {
    if (e.from === id) out.push(e.to);
    else if (e.to === id) out.push(e.from);
  }
  return out;
}

function loop() { simulate(); draw(); requestAnimationFrame(loop); }

/* ------------------------------------------------------------------ *
 * canvas interaction: pan + zoom + hover
 * ------------------------------------------------------------------ */
let dragging = false, last = null;
canvas.addEventListener("mousedown", (e) => { dragging = true; last = { x: e.clientX, y: e.clientY }; });
window.addEventListener("mouseup", () => { dragging = false; });
canvas.addEventListener("mousemove", (e) => {
  if (dragging && last) {
    view.tx += e.clientX - last.x; view.ty += e.clientY - last.y;
    last = { x: e.clientX, y: e.clientY };
    return;
  }
  const p = toWorld(e);
  let best = null, bd = 18 * 18;
  for (const nd of nodes.values()) {
    const d = (nd.x - p.x) ** 2 + (nd.y - p.y) ** 2;
    if (d < bd) { bd = d; best = nd; }
  }
  highlight = best;
});
canvas.addEventListener("mouseleave", () => { highlight = null; });
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const p = toWorld(e);
  const k = e.deltaY < 0 ? 1.12 : 0.89;
  const ns = Math.max(0.2, Math.min(4, view.scale * k));
  view.tx = e.offsetX - (e.offsetX - view.tx) * (ns / view.scale);
  view.ty = e.offsetY - (e.offsetY - view.ty) * (ns / view.scale);
  view.scale = ns;
}, { passive: false });

function toWorld(e) {
  return { x: (e.offsetX - view.tx) / view.scale, y: (e.offsetY - view.ty) / view.scale };
}

/* ------------------------------------------------------------------ *
 * UI: status, memory, streaming investigation
 * ------------------------------------------------------------------ */
async function loadStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    const f = s.falkordb || {}, llm = s.llm || {};
    const mode = llm.live ? `<span class="live">LIVE ${esc(llm.provider || "")}</span>` : `<span class="off">offline brain</span>`;
    $("status").innerHTML = `FalkorDB <b>${f.nodes ?? "?"}</b> nodes · tenant <b>${esc(f.tenant || "?")}</b> · ${mode}`;
  } catch (err) {
    $("status").innerHTML = `<span class="off">FalkorDB unreachable</span>`;
  }
}

async function loadMemory(tenant) {
  $("memtenant").textContent = tenant ? "· " + tenant : "";
  try {
    const m = await (await fetch("/api/memory?tenant=" + encodeURIComponent(tenant))).json();
    const rows = [];
    (m.cases || []).forEach((c) => rows.push(memRow("case", c.case_id, c.title || c.summary)));
    (m.notes || []).forEach((n) => rows.push(memRow(n.kind, n.note_id, n.text)));
    (m.alerts || []).forEach((a) => rows.push(memRow("alert", a.alert_id || a.account, (a.rule || "") + " " + (a.account || ""))));
    $("memlist").innerHTML = rows.length ? rows.join("") : `<p class="muted">No memory yet for this tenant.</p>`;
  } catch (err) {
    $("memlist").innerHTML = `<p class="muted">Could not load memory.</p>`;
  }
}

function memRow(kind, id, text) {
  return `<div class="memitem"><span class="tag ${esc(kind)}">${esc(kind)}</span><b>${esc(id || "")}</b> ${esc(String(text || "").slice(0, 140))}</div>`;
}

function step(tool, argsText, resText, cls) {
  const li = document.createElement("li");
  if (cls) li.className = cls;
  li.innerHTML = `<span class="tool">${esc(tool)}</span> <span class="args">${esc(argsText || "")}</span>` +
    (resText ? `<div class="res">${esc(resText)}</div>` : "");
  $("steps").appendChild(li);
  $("steps").parentElement.parentElement.scrollTop = 99999;
  $("steps").scrollTop = 99999;
}

async function investigate(target, tenant, offline, mode, question) {
  $("steps").innerHTML = "";
  $("verdict").classList.add("hidden");
  $("empty").classList.add("hidden");
  nodes.clear(); edges.length = 0; ring.clear(); focal = target || question || (mode === "company" ? "SYS-ledger-db" : "ACC-9003");
  document.querySelector("#controls button").disabled = true;
  $("rounds").textContent = "";

  const payload = { target: target || question || focal, tenant: tenant || (mode === "company" ? "company" : "default"), offline, mode };
  if (mode === "company" && question) payload.question = question;
  const res = await fetch("/api/investigate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) handle(JSON.parse(line));
    }
  }
  document.querySelector("#controls button").disabled = false;
}

function handle(ev) {
  if (ev.type === "start") {
    $("rounds").textContent = ev.provider ? "· " + ev.provider : "";
  } else if (ev.type === "tool_call") {
    step(ev.tool, shortArgs(ev.arguments), "", "call");
  } else if (ev.type === "tool_result") {
    ingest(ev.tool, ev.result);
    const last = $("steps").lastElementChild;
    if (last) {
      const id = ev.result && (ev.result.error ? "error" : "");
      if (id) last.classList.add("error");
      last.insertAdjacentHTML("beforeend", `<div class="res">→ ${esc(ev.summary || "")}</div>`);
    }
  } else if (ev.type === "skipped") {
    step(ev.tool, "(duplicate — skipped)", "", "error");
  } else if (ev.type === "learned") {
    step("memory", "learned playbook " + (ev.playbook.note_id || ""), (ev.playbook.steps || []).join(" > "), "call");
  } else if (ev.type === "done") {
    renderVerdict(ev);
    focal = ev.graph.focal || focal;
    (ev.graph.ring || []).forEach((r) => ring.add(r));
    (ev.graph.nodes || []).forEach((n) => { const x = addNode(n.id, n.label); if (x) x.kind = n.kind; });
    (ev.graph.edges || []).forEach((e) => addEdge(e.from, e.to, e.type));
    setTimeout(fit, 60);
    loadMemory(document.getElementById("tenant").value.trim());
  } else if (ev.type === "error") {
    step("stream", "", ev.error, "error");
  }
}

function renderVerdict(ev) {
  const parts = [`<h3>Verdict <span class="muted">· ${ev.rounds} rounds · ${esc(ev.brain)}</span></h3>`, md(ev.answer || "")];
  if (ev.actions && ev.actions.length) {
    parts.push(`<p class="muted">Verified actions: ${ev.actions.map((a) => `<code>${esc(a)}</code>`).join(" ")}</p>`);
  }
  const v = $("verdict");
  v.innerHTML = parts.join("");
  v.classList.remove("hidden");
  v.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ------------------------------------------------------------------ *
 * tiny markdown renderer + helpers
 * ------------------------------------------------------------------ */
function md(text) {
  const lines = esc(text).split(/\r?\n/);
  let html = "", inList = false;
  for (const raw of lines) {
    const line = raw.trim();
    const m = line.match(/^[-*]\s+(.*)$/);
    if (m) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(m[1])}</li>`;
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      if (line) html += `<p>${inline(line)}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html;
}
function inline(s) {
  return s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`(.+?)`/g, "<code>$1</code>");
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function shortArgs(a) {
  if (!a || typeof a !== "object") return "";
  return Object.entries(a).map(([k, v]) => `${k}=${String(v).slice(0, 40)}`).join(", ");
}

/* ------------------------------------------------------------------ *
 * boot
 * ------------------------------------------------------------------ */
function applyMode() {
  const company = $("mode").value === "company";
  $("target").value = company ? "SYS-ledger-db" : "ACC-9003";
  $("target").placeholder = company ? "SYS-ledger-db" : "ACC-9003";
  $("tenant").value = company ? "company" : "default";
  document.querySelector("#controls button").textContent = company ? "Ask the Company Brain" : "Investigate";
  const q = $("question");
  if (q) {
    q.classList.toggle("hidden", !company);
    if (!company) q.value = "";
  }
}
$("mode").addEventListener("change", applyMode);

$("controls").addEventListener("submit", (e) => {
  e.preventDefault();
  const mode = $("mode").value;
  const company = mode === "company";
  const q = company ? ($("question").value || "").trim() : "";
  const target = company ? (q || $("target").value.trim() || "SYS-ledger-db") : ($("target").value.trim() || "ACC-9003");
  const tenant = company ? ($("tenant").value.trim() || "company") : ($("tenant").value.trim() || "default");
  if (company && $("tenant").value.trim() === "") $("tenant").value = "company";
  investigate(target, tenant, $("offline").checked, mode, q).catch((err) => step("stream", "", String(err), "error"));
});

resize();
window.addEventListener("resize", resize);
loadStatus();
loadMemory($("tenant").value.trim());
requestAnimationFrame(loop);
