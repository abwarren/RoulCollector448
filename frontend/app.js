/* RoulCollector448 — dashboard logic (vanilla JS, no deps) */

"use strict";

/* ---------------- wheel / neighbours ---------------- */

const WHEEL = [0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,
               10,5,24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26];

function nnCluster(n) {
  const i = WHEEL.indexOf(n);
  return [WHEEL[(i + 35) % 37], WHEEL[(i + 36) % 37], n, WHEEL[(i + 1) % 37], WHEEL[(i + 2) % 37]];
}

/* ---------------- state ---------------- */

const ROWS = 40;          // initial 40 rows
const SPINS_PER_ROW = 50; // 50 per row
const BATCH = ROWS * SPINS_PER_ROW; // 2000 per "show more" click
const POLL_MS = 5000;

const state = {
  spins: [],      // chronological, oldest first (all loaded)
  total: 0,
  mode: "number", // "number" | "neighbors"
  sel: null,      // selected number or null
};

const $ = (id) => document.getElementById(id);

/* ---------------- API ---------------- */

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

/* ---------------- grid ---------------- */

function cellClass(n) {
  if (n === 0) return "g";
  return [1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36].includes(n) ? "r" : "b";
}

function renderGrid() {
  const grid = $("grid");
  const hl = highlightSet();
  const frag = document.createDocumentFragment();
  for (let i = 0; i < state.spins.length; i += SPINS_PER_ROW) {
    const row = document.createElement("div");
    row.className = "row50";
    for (const s of state.spins.slice(i, i + SPINS_PER_ROW)) {
      const b = document.createElement("button");
      b.className = "cell " + cellClass(s.number);
      b.textContent = s.number;
      b.dataset.n = s.number;
      if (hl && hl.hit.has(s.number)) b.classList.add("hl-hit");
      if (hl && hl.nb.has(s.number)) b.classList.add("hl-nb");
      row.appendChild(b);
    }
    frag.appendChild(row);
  }
  grid.replaceChildren(frag);
  $("shownCount").textContent = state.spins.length;
  $("totalCount").textContent = state.total;
}

function highlightSet() {
  if (state.sel === null) return null;
  const hit = new Set([state.sel]);
  const nb = new Set();
  if (state.mode === "neighbors") {
    for (const n of nnCluster(state.sel)) if (n !== state.sel) nb.add(n);
  }
  return { hit, nb };
}

function applyHighlightToTables() {
  document.querySelectorAll("#zTable tbody tr, #sleeperTable tbody tr").forEach((tr) => {
    tr.classList.toggle("hl-row", state.sel !== null && +tr.dataset.n === state.sel);
  });
}

function onGridClick(e) {
  const b = e.target.closest(".cell");
  if (!b) return;
  const n = +b.dataset.n;
  state.sel = (state.sel === n) ? null : n; // toggle off on re-click
  renderGrid();
  applyHighlightToTables();
}

$("grid").addEventListener("click", onGridClick);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.sel !== null) {
    state.sel = null;
    renderGrid();
    applyHighlightToTables();
  }
});
$("clearHl").addEventListener("click", () => {
  state.sel = null;
  renderGrid();
  applyHighlightToTables();
});

$("modeNumber").addEventListener("click", () => setMode("number"));
$("modeNeighbors").addEventListener("click", () => setMode("neighbors"));

function setMode(m) {
  state.mode = m;
  $("modeNumber").classList.toggle("active", m === "number");
  $("modeNeighbors").classList.toggle("active", m === "neighbors");
  renderGrid();
  applyHighlightToTables();
}

/* ---------------- loading / pagination ---------------- */

async function loadInitial() {
  const [count, spins] = await Promise.all([
    api("/api/spins/count"),
    api("/api/spins?offset=0&limit=" + BATCH),
  ]);
  state.total = count.total;
  state.spins = spins.spins;
  renderGrid();
}

$("showMore").addEventListener("click", async () => {
  const offset = state.spins.length;
  const d = await api(`/api/spins?offset=${offset}&limit=${BATCH}`);
  if (!d.spins.length) return;
  state.total = d.total;
  state.spins = d.spins.concat(state.spins); // older batch prepends
  renderGrid();
  applyHighlightToTables();
});

/* ---------------- live ticker ---------------- */

async function tickHealth() {
  try {
    const h = await api("/api/health");
    $("liveDot").className = "dot " + (h.collector_alive ? "ok" : "bad");
    $("liveLabel").textContent = h.collector_alive ? "LIVE" : "STALLED";
    $("totalSpins").textContent = h.total_spins;
    const live = h.live_last_spin || h.last_spin;
    if (live) {
      $("lastSpin").textContent = `${live.number} ${live.color}`;
      const age = h.live_age_seconds ?? h.db_age_seconds;
      $("lastAge").textContent = age !== null && age !== undefined ? `${Math.round(age)}s ago` : "—";
      if (h.live_age_seconds !== null && h.live_age_seconds !== undefined) {
        $("lastAge").title = `DB commit lag: ${h.db_age_seconds ?? "—"}s`;
      }
    }
    if (h.total_spins > state.spins.length) {
      const d = await api("/api/spins?offset=0&limit=" + BATCH);
      const known = new Set(state.spins.slice(0, BATCH).map((s) => s.id));
      state.total = d.total;
      const fresh = d.spins.filter((s) => !known.has(s.id));
      state.spins = fresh.concat(state.spins);
      renderGrid();
      applyHighlightToTables();
    }
  } catch (err) {
    $("liveDot").className = "dot bad";
    $("liveLabel").textContent = "ERR";
  }
}

/* ---------------- stats ---------------- */

async function loadZTable() {
  const d = await api("/api/stats/numbers");
  const t = $("zTable");
  t.innerHTML = `<thead><tr><th>#</th><th>hits</th><th>exp</th><th>z</th><th>last100</th></tr></thead><tbody>`;
  for (const x of d.numbers) {
    const tr = document.createElement("tr");
    tr.dataset.n = x.number;
    tr.innerHTML = `<td><span class="sw ${cellClass(x.number)}"></span>${x.number}</td>
      <td>${x.hits}</td><td>${x.expected}</td>
      <td style="color:${x.z >= 2 ? "#ffb74d" : x.z <= -2 ? "#64b5f6" : "#e0e0e0"}">${x.z > 0 ? "+" : ""}${x.z}</td>
      <td>${x.hits_100}</td>`;
    tr.addEventListener("click", () => {
      state.sel = (state.sel === x.number) ? null : x.number;
      renderGrid();
      applyHighlightToTables();
    });
    t.querySelector("tbody").appendChild(tr);
  }
  applyHighlightToTables();
}

async function loadSleepers() {
  const d = await api("/api/stats/sleepers");
  const t = $("sleeperTable");
  t.innerHTML = `<thead><tr><th>#</th><th>gap</th><th>last hit</th></tr></thead><tbody>`;
  for (const x of d.sleepers.slice(0, 15)) {
    const tr = document.createElement("tr");
    tr.dataset.n = x.number;
    const at = x.last_hit_at ? x.last_hit_at.slice(11, 19) : "—";
    tr.innerHTML = `<td><span class="sw ${cellClass(x.number)}"></span>${x.number}</td>
      <td><b>${x.gap}</b></td><td>${at}</td>`;
    tr.addEventListener("click", () => {
      state.sel = (state.sel === x.number) ? null : x.number;
      renderGrid();
      applyHighlightToTables();
    });
    t.querySelector("tbody").appendChild(tr);
  }
  applyHighlightToTables();
}

async function loadStreaks() {
  const d = await api("/api/stats/streaks");
  const box = $("streakBox");
  const span = (c) => {
    const s = d.longest_span[c];
    return s ? ` #${s[0]}–#${s[1]}` : "";
  };
  box.innerHTML = `
    <div class="row"><span>Longest Red</span><b>${d.longest.Red}${span("Red")}</b></div>
    <div class="row"><span>Longest Black</span><b>${d.longest.Black}${span("Black")}</b></div>
    <div class="row"><span>Longest Green</span><b>${d.longest.Green}</b></div>
    <div class="row"><span>Current run</span><b>${d.current.length}× ${d.current.color}</b></div>
    <div class="row"><span>Longest number repeat</span><b>${d.longest_number_repeat}</b></div>
    <div class="row"><span>Doubles / triples / 4+</span><b>${d.repeat_counts.doubles} / ${d.repeat_counts.triples} / ${d.repeat_counts.quads_plus}</b></div>`;
}

/* ---------------- SVG charts (hand-rolled, no deps) ---------------- */

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

function barChart(el, items, colorFn) {
  const W = 900, H = 130, PAD = 4;
  el.setAttribute("viewBox", `0 0 ${W} ${H}`);
  el.replaceChildren();
  if (!items.length) return;
  const max = Math.max(...items.map((d) => d.value), 1);
  const bw = W / items.length;
  items.forEach((d, i) => {
    const h = Math.max(1, (d.value / max) * (H - 22));
    const bar = svgEl("rect", {
      x: i * bw + 1, y: H - 8 - h, width: Math.max(1, bw - 2), height: h, fill: colorFn(d),
    });
    el.appendChild(bar);
    if (i % 5 === 0) {
      const lbl = svgEl("text", { x: i * bw + bw / 2, y: H - 2, "font-size": 8, fill: "#666", "text-anchor": "middle" });
      lbl.textContent = d.label;
      el.appendChild(lbl);
    }
  });
}

function lineChart(el, series, labels) {
  const W = 900, H = 130, PAD = 6;
  el.setAttribute("viewBox", `0 0 ${W} ${H}`);
  el.replaceChildren();
  if (!series.length) return;
  const max = Math.max(...series, 100), min = Math.min(...series, 0);
  const span = max - min || 1;
  const x = (i) => PAD + (i / (series.length - 1 || 1)) * (W - 2 * PAD);
  const y = (v) => H - PAD - ((v - min) / span) * (H - 2 * PAD - 8);
  const pts = series.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  el.appendChild(svgEl("polyline", { points: pts, fill: "none", stroke: "#00e5ff", "stroke-width": 1.5 }));
  const lbl = svgEl("text", { x: PAD, y: 10, "font-size": 8, fill: "#666" });
  lbl.textContent = `${labels.min}% – ${labels.max}%`;
  el.appendChild(lbl);
}

async function loadRolling() {
  const w = $("windowSel").value;
  const d = await api(`/api/stats/rolling?window=${w}`);
  barChart($("rollChart"), d.numbers.map((x) => ({ label: x.number, value: x.hits, color: x.color })),
    (item) => item.color === "Red" ? "#c62828" : item.color === "Green" ? "#1b5e20" : "#4a4a4a");
  // red/black balance over time in batches of 50 from loaded spins
  const spins = state.spins;
  const series = [], mn = [100, 0];
  for (let i = 0; i < spins.length; i += 50) {
    const batch = spins.slice(i, i + 50);
    if (batch.length < 25) break;
    const red = batch.filter((s) => s.color === "Red").length;
    const pct = Math.round((red / batch.length) * 100);
    series.push(pct);
    if (pct < mn[0]) mn[0] = pct;
    if (pct > mn[1]) mn[1] = pct;
  }
  lineChart($("balChart"), series, { min: mn[0], max: mn[1] });
}

$("windowSel").addEventListener("change", loadRolling);

/* ---------------- audit ---------------- */

async function loadAudit() {
  try {
    const d = await api("/api/audit");
    const a = d.audit || {};
    $("auditMeta").textContent = `last ${d.generated_at ? d.generated_at.slice(0, 19).replace("T", " ") : "—"} · every ${Math.round(d.interval_seconds / 3600)}h · vs last ${d.window}`;
    const rows = (a.drift || []).slice(0, 6);
    $("auditBody").innerHTML = `
      <div class="audit-row">
        <div><span class="kv">all-time</span> ${a.all_time ? a.all_time.total : "—"} spins · top ${(a.all_time?.top || []).join(", ")}</div>
        <div><span class="kv">last ${d.window}</span> · hot: <span class="hot">${(a.rotated_hot || []).join(", ") || "—"}</span>
        <span class="kv">cold:</span> <span class="cold">${(a.rotated_cold || []).join(", ") || "—"}</span></div>
      </div>
      <table><thead><tr><th>#</th><th>all z</th><th>last${d.window} z</th><th>Δz</th></tr></thead><tbody>
      ${rows.map((x) => `<tr><td>${x.number}</td><td>${x.all_z > 0 ? "+" : ""}${x.all_z}</td>
        <td>${x.last500_z > 0 ? "+" : ""}${x.last500_z}</td>
        <td style="color:${x.delta > 0 ? "#ffb74d" : "#64b5f6"}">${x.delta > 0 ? "+" : ""}${x.delta}</td></tr>`).join("")}
      </tbody></table>`;
  } catch {
    $("auditBody").innerHTML = `<span class="kv">audit unavailable</span>`;
  }
}

/* ---------------- boot ---------------- */

async function boot() {
  await loadInitial();
  loadZTable();
  loadSleepers();
  loadStreaks();
  loadRolling();
  loadAudit();
  tickHealth();
  setInterval(tickHealth, POLL_MS);
  setInterval(loadAudit, 3600 * 1000); // keep the audit panel fresh hourly
}

boot();
