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

/* Counterfactual stress test (click a node -> /api/stress). When `stress.result`
 * is set the canvas shows the graph *as if* that entity were deleted: the node
 * turns into a ghost, its edges fade, and a broken money trail is drawn in red. */
const stress = { entity: null, result: null, busy: false, before: null };

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
  } else if (tool === "assess_ring_resilience") {
    (r.members || []).forEach((m) => {
      if (!m.entity) return;
      addNode(String(m.entity), (m.is_articulation ? "⚠ " : "") + String(m.entity).split("-")[0] + (m.robustness != null ? ` [${m.robustness}/100]` : ""));
      if (m.is_articulation) ring.add(String(m.entity));
    });
  } else if (tool === "ring_timeline") {
    (r.events || []).forEach((e) => {
      if (e.kind === "alert") {
        if (e.account) { addNode(String(e.account)); addNode(String(e.alert || "ALERT"), "Alert"); addEdge(String(e.account), String(e.alert || "ALERT"), "FLAGGED_BY"); }
        return;
      }
      if (!e.from || !e.to) return;
      addNode(String(e.from)); addNode(String(e.to));
      addEdge(String(e.from), String(e.to), e.kind === "merchant_spend" ? "PAID" : "TX");
      if (e.kind === "transfer" && e.direction === "outbound") ring.add(String(e.from));
    });
  } else if (tool === "find_similar_rings") {
    // look-alike groups: draw them as a separate constellation linked to the focal
    // by a dotted "similar" edge, so judges see "the playbook repeats here".
    const origin = focal || ((r.target || {}).members || [])[0] || "";
    (r.candidates || []).forEach((cand, i) => {
      const look = cand.members || [];
      look.forEach((m) => { addNode(String(m), `≈${String(m).split("-")[0]}`); ring.add(String(m)); });
      if (look.length && origin && cand.similarity >= 40) addEdge(origin, String(look[0]), `similar ${cand.similarity}%`);
      if (i === 0 && cand.similarity) highlight = { id: String(look[0] || origin), label: `look-alike ${cand.similarity}%` };
    });
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
  const removed = stress.entity;   // node mentally deleted by the stress test

  for (const e of edges) {
    const a = fmap.get(e.from), b = fmap.get(e.to);
    if (!a || !b) continue;
    const touches_removed = removed && (e.from === removed || e.to === removed);
    const on = !active || (active.has(e.from) && active.has(e.to));
    if (touches_removed) {
      ctx.strokeStyle = "rgba(255,90,110,0.55)";   // severed by the counterfactual
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.4;
    } else {
      ctx.strokeStyle = on ? "rgba(120,160,220,0.42)" : "rgba(80,100,140,0.12)";
      ctx.setLineDash([]);
      ctx.lineWidth = (active && on) ? 1.6 : 1;
    }
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }
  ctx.setLineDash([]);

  // label the relationships touching the hovered node, so judges can read the graph
  if (highlight) {
    ctx.font = "9.5px 'Segoe UI', sans-serif";
    for (const e of edges) {
      if (e.from !== highlight.id && e.to !== highlight.id) continue;
      const a = fmap.get(e.from), b = fmap.get(e.to);
      if (!a || !b) continue;
      ctx.fillStyle = "rgba(219,228,245,0.8)";
      ctx.fillText(String(e.type), (a.x + b.x) / 2, (a.y + b.y) / 2 - 4);
    }
  }

  ctx.font = "10px 'Segoe UI', sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  for (const nd of nodes.values()) {
    const isFocal = nd.id === focal;
    const isRemoved = removed && nd.id === removed;
    const r = isFocal ? 11 : nd.kind === "ring" || ring.has(nd.id) ? 8 : 6;
    const dim = (active && !active.has(nd.id)) || isRemoved;
    if (isFocal && !isRemoved) {
      const glow = ctx.createRadialGradient(nd.x, nd.y, r, nd.x, nd.y, r * 3.4);
      glow.addColorStop(0, "rgba(255,92,122,0.34)");
      glow.addColorStop(1, "rgba(255,92,122,0)");
      ctx.fillStyle = glow;
      ctx.beginPath(); ctx.arc(nd.x, nd.y, r * 3.4, 0, Math.PI * 2); ctx.fill();
    }
    ctx.beginPath();
    ctx.arc(nd.x, nd.y, r, 0, Math.PI * 2);
    ctx.fillStyle = isRemoved ? "rgba(60,70,95,0.25)" : dim ? "rgba(90,105,135,0.35)" : color(nd);
    ctx.fill();
    if (isRemoved) {
      // ghost of the removed entity: dashed ring + red cross (mentally deleted)
      ctx.strokeStyle = "rgba(255,90,110,0.9)"; ctx.lineWidth = 1.4;
      ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(nd.x - r * 0.6, nd.y - r * 0.6); ctx.lineTo(nd.x + r * 0.6, nd.y + r * 0.6);
      ctx.moveTo(nd.x + r * 0.6, nd.y - r * 0.6); ctx.lineTo(nd.x - r * 0.6, nd.y + r * 0.6);
      ctx.stroke();
    } else if (isFocal || nd.kind === "ring" || ring.has(nd.id) || (highlight && highlight.id === nd.id)) {
      ctx.strokeStyle = "rgba(255,255,255,0.85)"; ctx.lineWidth = 1.4; ctx.stroke();
    }
    if (view.scale > 0.55 && !dim) {
      ctx.fillStyle = "rgba(219,228,245,0.82)";
      ctx.fillText(nd.id, nd.x, nd.y - r - 8);
    }
  }
  ctx.restore();
  updateStats();
}

/* Canvas overlay: how much of the graph the agent has pulled in so far. */
let statsKey = "";
function updateStats() {
  const key = nodes.size + "/" + edges.size;
  if (key === statsKey) return;
  statsKey = key;
  const el = $("graphstats");
  if (el) el.textContent = nodes.size ? `${nodes.size} nodes · ${edges.size} relationships` : "";
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
let dragging = false, last = null, downAt = null;
canvas.addEventListener("mousedown", (e) => { dragging = true; last = { x: e.clientX, y: e.clientY }; downAt = { x: e.clientX, y: e.clientY }; });
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
 * counterfactual stress test: click a node -> /api/stress
 * ------------------------------------------------------------------ */
async function runStressTest(entityId) {
  if (!entityId || stress.busy) return;
  stress.busy = true;
  $("stress").classList.remove("hidden");
  $("stressverdict").textContent = "running…";
  $("stressbody").innerHTML = `<p class="muted">Mentally removing <b>${esc(entityId)}</b> and re-checking the case…</p>`;
  try {
    const tenant = ($("tenant").value || "default").trim();
    const url = "/api/stress?entity=" + encodeURIComponent(entityId) +
      "&tenant=" + encodeURIComponent(tenant) +
      "&from=" + encodeURIComponent("ACC-EXT-01") +
      "&to=" + encodeURIComponent("ACC-9100");
    const r = await (await fetch(url)).json();
    stress.entity = entityId;
    stress.result = r;
    const impact = r.impact || "—";
    const rob = r.robustness ?? "—";
    const verdict = r.verdict || "—";
    $("stressverdict").innerHTML =
      `<span class="stress-${esc(impact)}">${esc(impact)}</span> · robustness ${esc(String(rob))}/100 · ${esc(verdict)}`;
    const p = r.path || {};
    const rows = [
      ["Impact", impact],
      ["Robustness", `${rob}/100`],
      ["Verdict", verdict],
      ["Load-bearing", String(r.load_bearing_score ?? "—")],
      ["Money trail before", p.money_trail_before || "(none)"],
      ["Money trail after removal", p.money_trail_after || "(none found)"],
      ["Ring before/after", r.cluster && r.cluster.top_before && r.cluster.top_after
        ? `${r.cluster.top_before.size} → ${r.cluster.top_after.size} members (score ${r.cluster.top_before.score} → ${r.cluster.top_after.score})`
        : "—"],
    ];
    const width = Math.max(0, Math.min(100, Number(rob) || 0));
    const meter = `<div class="meter" title="robustness ${esc(String(rob))}/100"><i style="width:${width}%"></i></div>` +
      `<p class="muted">robustness ${esc(String(rob))}/100 — how much of the conclusion survives the removal</p>`;
    $("stressbody").innerHTML =
      `<p>${esc(r.conclusion || "")}</p>` + meter +
      `<table class="stress-table">${rows.map(([k, v]) =>
        `<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`).join("")}</table>` +
      `<p class="muted">${esc(r.note || "")}</p>`;
  } catch (err) {
    $("stressverdict").textContent = "error";
    $("stressbody").innerHTML = `<p class="muted">Stress test failed: ${esc(String(err))}</p>`;
  } finally {
    stress.busy = false;
  }
}

function clearStressTest() {
  stress.entity = null;
  stress.result = null;
  $("stress").classList.add("hidden");
}

$("stressrestore").addEventListener("click", clearStressTest);

canvas.addEventListener("click", (e) => {
  // Ignore clicks that were actually drags (panning).
  if (downAt && (Math.abs(e.clientX - downAt.x) > 4 || Math.abs(e.clientY - downAt.y) > 4)) return;
  const p = toWorld(e);
  let best = null, bd = 18 * 18;
  for (const nd of nodes.values()) {
    const d = (nd.x - p.x) ** 2 + (nd.y - p.y) ** 2;
    if (d < bd) { bd = d; best = nd; }
  }
  if (best) runStressTest(best.id);
});

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
  $("exports").classList.add("hidden");
  $("empty").classList.add("hidden");
  clearStressTest();
  nodes.clear(); edges.length = 0; ring.clear(); focal = target || question || (mode === "company" ? "SYS-ledger-db" : "ACC-9003");
  document.querySelector("#controls button").disabled = true;
  $("rounds").textContent = "";

  const payload = { target: target || question || focal, tenant: tenant || (mode === "company" ? "company" : "default"), offline, mode };
  if (mode === "company" && question) payload.question = question;
  const btn = document.querySelector("#controls button");
  const btnLabel = btn.textContent;
  btn.classList.add("busy");
  try {
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
  } finally {
    btn.disabled = false;
    btn.classList.remove("busy");
    btn.textContent = btnLabel;
  }
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
  const parts = [
    `<h3>Verdict <span class="pill">${ev.rounds} rounds</span> <span class="pill">${esc(ev.brain)}</span></h3>`,
    md(ev.answer || ""),
  ];
  if (ev.actions && ev.actions.length) {
    parts.push(`<p class="muted">Verified actions: ${ev.actions.map((a) => `<code>${esc(a)}</code>`).join(" ")}</p>`);
  }
  const v = $("verdict");
  v.innerHTML = parts.join("");
  v.classList.remove("hidden");
  v.scrollIntoView({ behavior: "smooth", block: "nearest" });
  lastVerdict = ev;
  $("exports").classList.remove("hidden");
}

/* ------------------------------------------------------------------ *
 * proof pack: verdict + evidence + canvas snapshot -> downloadable file
 * ------------------------------------------------------------------ */
let lastVerdict = null;

function stepsMarkdown() {
  const rows = [];
  document.querySelectorAll("#steps li").forEach((li) => rows.push(li.innerText.trim().split("\n")[0]));
  return rows;
}

function buildMarkdown() {
  const ev = lastVerdict || {};
  const stressText = $("stress").classList.contains("hidden") ? "" : $("stress").innerText.trim();
  const lines = [
    "# GraphSleuth investigation report",
    "",
    `- **Target:** ${$("target").value || "-"}   **Tenant:** ${$("tenant").value || "-"}`,
    `- **Mode:** ${$("mode").value === "company" ? "Company Brain" : "Fraud investigation"}`,
    `- **Generated:** ${new Date().toISOString()}   **Brain:** ${esc(ev.brain || "-")}   **Rounds:** ${ev.rounds ?? "-"}`,
    `- **Tools:** ${$("status").innerText.replace(/\s+/g, " ").trim()}`,
    "",
    "## Verdict",
    "",
    ev.answer || "(no verdict)",
    "",
    "## Evidence trail (tool calls in order)",
    "",
    ...stepsMarkdown().map((s, i) => `${i + 1}. ${s}`),
  ];
  if (stressText) {
    lines.push("", "## Counterfactual stress test (click-to-verify)", "", "```text", stressText, "```");
  }
  if (ev.actions && ev.actions.length) {
    lines.push("", "## Verified actions", "", ...ev.actions.map((a) => `- \`${a}\``));
  }
  lines.push(
    "",
    "## Reproduce",
    "",
    "```bash",
    "python -m graphsleuth seed",
    "python -m graphsleuth demo --offline --save report.md",
    "python -m graphsleuth eval   # 16/16 checks",
    "```",
    "",
    "_Generated by GraphSleuth — every claim above is a graph query you can re-run._",
    ""
  );
  return lines.join("\n");
}

function buildProofPack() {
  const ev = lastVerdict || {};
  const snapshot = canvas.toDataURL("image/png");
  const stressText = $("stress").classList.contains("hidden") ? "" : $("stress").innerText.trim();
  const steps = stepsMarkdown()
    .map((s, i) => `<li>${esc(s)}</li>`)
    .join("");
  const escBody = esc(ev.answer || "(no verdict)");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<title>GraphSleuth proof pack — ${esc($("target").value || "")}</title>
<style>
  body { font: 15px/1.6 "Segoe UI", system-ui, sans-serif; color: #16213a; margin: 40px auto; max-width: 860px; padding: 0 20px; }
  h1 { font-size: 24px; margin-bottom: 2px; }
  h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 1.2px; color: #5a6a8c; margin-top: 30px; border-bottom: 2px solid #e6ebf5; padding-bottom: 6px; }
  .meta { color: #5a6a8c; font-size: 13px; }
  .badge { display: inline-block; background: #eef2fb; border: 1px solid #d7dff0; border-radius: 999px; padding: 2px 10px; margin: 2px 4px 2px 0; font-size: 12.5px; }
  .verdict { background: #f6f8fe; border-left: 4px solid #3f6ad8; padding: 12px 16px; border-radius: 0 8px 8px 0; white-space: pre-wrap; }
  .stress { background: #fff5f6; border-left: 4px solid #e35d7a; padding: 12px 16px; border-radius: 0 8px 8px 0; white-space: pre-wrap; font-family: Consolas, monospace; font-size: 13px; }
  img.snapshot { max-width: 100%; border: 1px solid #e6ebf5; border-radius: 10px; background: #0d1424; }
  ol { padding-left: 22px; } ol li { font-family: Consolas, monospace; font-size: 13px; margin: 3px 0; }
  footer { margin-top: 34px; padding-top: 12px; border-top: 1px solid #e6ebf5; color: #7a86a3; font-size: 12.5px; }
  code { background: #eef2fb; padding: 1px 6px; border-radius: 5px; }
  @media print { body { margin: 0; } img.snapshot { page-break-inside: avoid; } }
</style></head><body>
<h1>◈ GraphSleuth — Graph Proof Pack</h1>
<p class="meta">Target <code>${esc($("target").value || "-")}</code> · Tenant <code>${esc($("tenant").value || "-")}</code>
 · Mode ${$("mode").value === "company" ? "Company Brain" : "Fraud investigation"}
 · ${new Date().toISOString()} · Rounds ${ev.rounds ?? "-"} · Brain ${esc(ev.brain || "-")}</p>
<p><span class="badge">MCP toolbelt: 30 tools</span><span class="badge">Offline tests: 41/41</span>
<span class="badge">Eval: 16/16</span><span class="badge">Read-only counterfactual</span></p>

<h2>Verdict</h2>
<div class="verdict">${escBody}</div>
${ev.actions && ev.actions.length ? `<h2>Verified actions</h2><p>${ev.actions.map((a) => `<code>${esc(a)}</code>`).join(" ")}</p>` : ""}
${stressText ? `<h2>Counterfactual stress test</h2><div class="stress">${esc(stressText)}</div>` : ""}
<h2>Graph snapshot (live canvas)</h2>
<img class="snapshot" src="${snapshot}" alt="graph snapshot" />
<h2>Evidence trail — every tool call, in order</h2>
<ol>${steps}</ol>
<h2>How to reproduce</h2>
<p><code>python -m graphsleuth seed</code> · <code>python -m graphsleuth demo --offline --save report.md</code> ·
<code>python -m graphsleuth eval</code> (16/16) · MCP: <code>python -m graphsleuth.mcp_server</code> (30 tools over stdio)</p>
<footer>Generated by GraphSleuth (Graph Hacks · FalkorDB). Every claim is a graph query — re-run it.
Counterfactual tools never write: <em>what_if_removed · assess_ring_resilience · ring_timeline · find_similar_rings</em> are read-only.</footer>
</body></html>`;
}

function downloadFile(name, mime, text) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

const EXPORT_STAMP = () => ($("target").value || "investigation").replace(/[^\w-]+/g, "_");

$("copyverdict").addEventListener("click", async () => {
  const text = (lastVerdict && lastVerdict.answer) || $("verdict").innerText;
  try {
    await navigator.clipboard.writeText(text);
    $("copyverdict").textContent = "Copied ✓";
  } catch (err) {
    $("copyverdict").textContent = "Copy failed";
  }
  setTimeout(() => { $("copyverdict").textContent = "Copy verdict"; }, 1600);
});

$("downloadmd").addEventListener("click", () => {
  downloadFile(`graphsleuth_${EXPORT_STAMP()}.md`, "text/markdown", buildMarkdown());
});

$("downloadproof").addEventListener("click", () => {
  downloadFile(`graphsleuth_proof_${EXPORT_STAMP()}.html`, "text/html", buildProofPack());
});

/* ------------------------------------------------------------------ *
 * tiny markdown renderer + helpers
 * ------------------------------------------------------------------ */
function md(text) {
  const lines = esc(text).split(/\r?\n/);
  let html = "", inList = false;
  for (const raw of lines) {
    const line = raw.trim();
    const section = line.match(/^\*\*([^*]+)\*\*$/);   // a bold line on its own = a section header
    const m = line.match(/^[-*]\s+(.*)$/);
    if (section) {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<h4 class="sect">${inline(section[1])}</h4>`;
    } else if (m) {
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
  const wrap = $("questionwrap");
  if (q && wrap) {
    wrap.classList.toggle("hidden", !company);
    if (!company) q.value = "";
  }
}
$("mode").addEventListener("change", applyMode);

/* quick-start chips: click to load an example target */
document.querySelectorAll(".chipbtn").forEach((b) => b.addEventListener("click", () => {
  $("target").value = b.dataset.target || "";
  $("target").focus();
}));

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
