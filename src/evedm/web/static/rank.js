/* EVE Distance Meter - ranking page: target input, ranked list (left) and route on the star map (right). */
"use strict";

async function api(url, opts = {}) {
  const resp = await fetch(url, { credentials: "same-origin", ...opts,
    headers: { "X-EVEDM": "1", ...(opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
      ...(opts.headers || {}) } });
  if (resp.status === 401) { location.href = "/login"; return new Promise(() => {}); }  // signed out: leave quietly
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return resp.json();
}

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

function secChip(security) {
  const chip = el("span", "sec", secText(security));
  chip.style.background = secColor(security);
  return chip;
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

const state = { target: null, result: null, selected: null, timer: null, busy: false };
const main = document.querySelector("main.split");
const autoRefresh = Number(main.dataset.autoRefresh || 0);
const map = new StarMap($("#map"), $("#tooltip"));

/* ---------------------------------------------------------------- ranking */
async function rank(target, { refresh = 1, keepSelection = false } = {}) {
  if (state.busy) return;
  state.busy = true;
  $("#status").textContent = "Updating…";
  try {
    const result = await api(`/api/rank?target=${encodeURIComponent(target)}&refresh=${refresh}`);
    state.result = result;
    state.target = result.target;
    if (!keepSelection || !result.rows.some((r) => r.character_id === state.selected)) {
      const pick = result.rows.find((r) => r.recommended) || result.rows[0];
      state.selected = pick ? pick.character_id : null;
    }
    const url = new URL(location.href);
    url.searchParams.set("target", result.target.name);
    history.replaceState(null, "", url);
    $("#target").value = result.target.name;
    render();
    loadRecent();
    const st = result.stats;
    $("#status").textContent = `Updated ${new Date().toLocaleTimeString()} · ${st.characters} characters · ` +
      `${st.total_ms} ms`;
  } catch (err) {
    $("#status").textContent = `Error: ${err.message}`;
  } finally {
    state.busy = false;
  }
}

function render() {
  const r = state.result;
  const info = $("#target-info");
  info.innerHTML = "";
  const h = el("h1");
  h.append(el("span", null, r.target.name), secChip(r.target.security));
  info.append(h, el("div", "sub", `${r.target.region} · ${r.stats.reachable} of ${r.stats.characters} characters can reach it by gates`));

  const list = $("#rows");
  list.innerHTML = "";
  r.rows.forEach((row, n) => list.append(renderRow(row, n + 1)));

  const ex = $("#excluded");
  ex.textContent = r.excluded.length
    ? "Not ranked: " + r.excluded.map((e) => `${e.name} (${e.reason})`).join(", ") : "";
  showSelection();
}

function renderRow(row, n) {
  const li = el("li", "row");
  li.dataset.id = row.character_id;
  if (!row.reachable) li.classList.add("unreachable");
  if (row.character_id === state.selected) li.classList.add("selected");
  li.tabIndex = 0;

  const who = el("div", "who");
  const name = el("div", "name", row.name);
  if (row.recommended) name.append(el("span", "badge closest", "Closest"));
  if (row.in_pod) name.append(el("span", "badge pod", "In pod"));
  if (row.status === "LIMITED") name.append(el("span", "badge limited", "Limited"));
  if (row.error) name.append(el("span", "badge err", row.error));
  if (row.stale) name.append(el("span", "badge stale", "Stale"));
  who.append(name);

  const ship = el("div", "ship");
  if (row.ship_type) {
    ship.append(el("b", null, row.ship_type));
    if (row.ship_name) ship.append(document.createTextNode(` · ${row.ship_name}`));
  } else {
    ship.textContent = "Ship: n/a";
  }
  who.append(ship);

  const where = el("div", "where");
  if (row.system) {
    where.append(secChip(row.system.security), el("span", null, `${row.system.name} · ${row.system.region}`),
      el("span", null, row.docked ? "· Docked" : "· In space"));
  } else {
    where.textContent = "Location unknown";
  }
  who.append(where);

  const meta = el("div", "meta");
  if (row.online) meta.append(el("span", "badge online", "Online"));
  else if (row.last_logout) meta.textContent = `Offline · last seen ${ago(row.last_logout)}`;
  who.append(meta);

  const jumps = el("div", "jumps");
  if (row.reachable) jumps.append(el("b", null, String(row.jumps)), el("span", null, row.jumps === 1 ? "jump" : "jumps"));
  else jumps.append(el("b", null, "No gate route"));

  li.append(el("div", "rank", String(n)), who, jumps);
  li.addEventListener("click", () => select(row.character_id));
  li.addEventListener("keydown", (ev) => { if (ev.key === "Enter") select(row.character_id); });
  return li;
}

function select(id) {
  state.selected = id;
  document.querySelectorAll(".row").forEach((li) => li.classList.toggle("selected", Number(li.dataset.id) === id));
  showSelection();
}

function showSelection() {
  const r = state.result;
  if (!r) return;
  const row = r.rows.find((x) => x.character_id === state.selected);
  const markers = r.rows.filter((x) => x.system).map((x) => ({ systemId: x.system.id, name: x.name }));
  map.setScene({ targetId: r.target.id, routeIds: row && row.route, originName: row && row.name, markers });
  const bar = $("#route-bar");
  bar.innerHTML = "";
  if (!row) { bar.hidden = true; return; }
  const title = el("div", "title");
  if (row.reachable) {
    const s = row.route_summary;
    title.append(document.createTextNode(`${row.name}: ${row.system.name} → ${r.target.name} · ${row.jumps} jumps `),
      el("span", "muted", `(${s.high} high · ${s.low} low · ${s.null} null)`));
    const path = el("div", "path");
    row.route.forEach((id, k) => {
      const i = map.indexOf(id);
      if (k) path.append(el("span", "arrow", "›"));
      const hop = el("span", "hop");
      hop.append(secChip(map.sec[i]), el("span", null, map.names[i]));
      path.append(hop);
    });
    bar.append(title, path);
  } else {
    title.textContent = row.system
      ? `${row.name} is in ${row.system.name} (${row.system.region}): no stargate route to ${r.target.name}.`
      : `${row.name}: location unknown.`;
    bar.append(title);
  }
  bar.hidden = false;
}

/* ---------------------------------------------------------------- recent targets */
async function loadRecent() {
  try {
    const items = await api("/api/targets/recent");
    const box = $("#recent");
    box.innerHTML = "";
    items.slice(0, 12).forEach((t) => {
      const chip = el("button", "chip", t.name);
      chip.type = "button";
      chip.addEventListener("click", () => rank(t.name));
      box.append(chip);
    });
  } catch (_) { /* optional */ }
}

/* ---------------------------------------------------------------- autocomplete (INP-01) */
let suggestTimer = null, active = -1, items = [];
const input = $("#target"), suggest = $("#suggest");
input.addEventListener("input", () => {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(async () => {
    const q = input.value.trim();
    if (q.length < 2) { suggest.hidden = true; return; }
    items = await api(`/api/systems/search?q=${encodeURIComponent(q)}`);
    active = -1;
    suggest.innerHTML = "";
    items.forEach((s, k) => {
      const li = el("li");
      li.append(secChip(s.security), el("span", null, s.name), el("span", "region", s.region));
      li.addEventListener("mousedown", (ev) => { ev.preventDefault(); choose(k); });
      suggest.append(li);
    });
    suggest.hidden = !items.length;
  }, 120);
});
input.addEventListener("keydown", (ev) => {
  if (suggest.hidden) return;
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    active = (active + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    [...suggest.children].forEach((li, k) => li.classList.toggle("active", k === active));
  } else if (ev.key === "Enter" && active >= 0) {
    ev.preventDefault();
    choose(active);
  } else if (ev.key === "Escape") {
    suggest.hidden = true;
  }
});
input.addEventListener("blur", () => setTimeout(() => (suggest.hidden = true), 150));
function choose(k) { suggest.hidden = true; input.value = items[k].name; rank(items[k].name); }

$("#target-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  suggest.hidden = true;
  if (input.value.trim()) rank(input.value.trim());
});
$("#refresh-btn").addEventListener("click", () => state.target && rank(state.target.name, { refresh: 2, keepSelection: true }));

/* ---------------------------------------------------------------- paste text (INP-02, INP-03) */
const dlg = $("#paste-dialog");
$("#paste-btn").addEventListener("click", () => { $("#paste-result").innerHTML = ""; dlg.showModal(); $("#paste-text").focus(); });
$("#paste-find").addEventListener("click", async (ev) => {
  ev.preventDefault();
  const out = $("#paste-result");
  out.innerHTML = "";
  const { candidates } = await api("/api/target/resolve", { method: "POST", body: JSON.stringify({ text: $("#paste-text").value }) });
  if (!candidates.length) { out.textContent = "No solar system found."; return; }
  if (candidates.length === 1) { dlg.close(); rank(candidates[0].name); return; }
  out.append(el("div", "small", "Several systems found — pick one:"));
  candidates.forEach((c) => {
    const b = el("button", "btn");
    b.type = "button";
    b.append(secChip(c.security), document.createTextNode(` ${c.name} `), el("span", "small", c.region));
    b.addEventListener("click", () => { dlg.close(); rank(c.name); });
    out.append(b);
  });
});

/* ---------------------------------------------------------------- map tools + auto refresh */
$("#zoom-in").addEventListener("click", () => map.zoomBy(1.6));
$("#zoom-out").addEventListener("click", () => map.zoomBy(1 / 1.6));
$("#zoom-fit").addEventListener("click", () => map.fitScene());

if (autoRefresh > 0) {
  setInterval(() => {
    if (document.visibilityState === "visible" && state.target && !state.busy) {
      rank(state.target.name, { keepSelection: true });
    }
  }, autoRefresh * 1000);
}

(async () => {
  await map.load("/api/universe/map");
  loadRecent();
  const t = new URLSearchParams(location.search).get("target");
  if (t) rank(t);
})();
