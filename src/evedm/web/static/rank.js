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

/* Largest ship that fits through a wormhole (EVE-Scout max_ship_size): a hull icon that grows with the class. */
const SIZE_LABEL = { small: "Small", medium: "Medium", large: "Large", xlarge: "X-large", capital: "Capital" };
const SIZE_SCALE = { small: 0.5, medium: 0.64, large: 0.78, xlarge: 0.9, capital: 1 };
function sizeIcon(size, what) {
  if (!SIZE_LABEL[size]) return null;
  const ns = "http://www.w3.org/2000/svg";
  const wrap = el("span", `wh-size sz-${size}`);
  wrap.title = `${what || "Largest ship"}: ${SIZE_LABEL[size]}`;
  wrap.setAttribute("role", "img");
  wrap.setAttribute("aria-label", wrap.title);
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  const hull = document.createElementNS(ns, "path");
  const k = SIZE_SCALE[size], c = 10;  // a ship seen from above, scaled around the centre
  const pts = [[0, -9], [4, -2], [9, 5], [4, 4], [3, 9], [0, 7], [-3, 9], [-4, 4], [-9, 5], [-4, -2]];
  hull.setAttribute("d", "M" + pts.map(([x, y]) => `${(c + x * k).toFixed(1)} ${(c + y * k).toFixed(1)}`).join("L") + "Z");
  hull.setAttribute("class", "hull");
  svg.append(hull);
  if (size === "capital" || size === "xlarge") {  // a second ring for the two biggest classes
    const ring = document.createElementNS(ns, "circle");
    ring.setAttribute("cx", "10"); ring.setAttribute("cy", "10"); ring.setAttribute("r", size === "capital" ? "9.4" : "8.4");
    ring.setAttribute("fill", "none"); ring.setAttribute("stroke", "currentColor"); ring.setAttribute("stroke-width", "1");
    ring.setAttribute("opacity", "0.55");
    svg.append(ring);
  }
  wrap.append(svg);
  return wrap;
}

/* The signatures to look for: InSig is in the k-space system, OutSig is the other end in Thera / Turnur. */
function sigTag(end, hub) {
  if (!end || (!end.in_signature && !end.out_signature)) return null;
  const tag = el("span", "sig");
  tag.title = `Wormhole signatures: InSig is in ${end.name}, OutSig is the same wormhole in ${hub}`;
  if (end.in_signature) { const s = el("span"); s.append("InSig ", el("b", null, end.in_signature)); tag.append(s); }
  if (end.out_signature) { const s = el("span"); s.append("OutSig ", el("b", null, end.out_signature)); tag.append(s); }
  return tag;
}

const state = { target: null, result: null, selected: null, timer: null, busy: false, tab: "live",
  hub: $("#hub-toggle") ? String(Prefs.get("rank", { hub: "" }).hub || "") : "" };  // "" = gates only
const main = document.querySelector("main.split");
const autoRefresh = Number(main.dataset.autoRefresh || 0);
const map = new StarMap($("#map"), $("#tooltip"));

/* ---------------------------------------------------------------- ranking */
async function rank(target, { refresh = 1, keepSelection = false } = {}) {
  if (state.busy) return;
  state.busy = true;
  $("#status").textContent = "Updating…";
  try {
    const result = await api(`/api/rank?target=${encodeURIComponent(target)}&refresh=${refresh}` +
      (state.hub ? `&hub=${encodeURIComponent(state.hub)}` : ""));
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
    rosterChanged();  // the roster is ranked against the same target (ROS-19)
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

  renderThera(r);
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
  if (row.error) {
    const badge = el("span", "badge err", row.error);
    if (row.error === "esi:420") badge.title = "ESI's error limit was reached: calls pause and resume by themselves " +
      "(about a minute). Location and online state shown are the last known ones.";
    name.append(badge);
  }
  if (row.stale) name.append(el("span", "badge stale", "Stale"));
  const axNote = ansiblexNote(row);
  if (axNote) name.append(axNote);
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
  if (row.reachable) {
    jumps.append(el("b", null, String(row.jumps)), el("span", null, row.jumps === 1 ? "jump" : "jumps"));
    if (row.wormhole && row.wormhole.used) jumps.append(el("span", "badge wh", `via ${row.wormhole.hub}`));
    if (row.ansiblex && row.ansiblex.used) jumps.append(el("span", "badge ax", "via Ansiblex"));
  } else jumps.append(el("b", null, "No gate route"));

  li.append(el("div", "rank", String(n)), who, jumps);
  li.addEventListener("click", () => select(row.character_id));
  li.addEventListener("keydown", (ev) => { if (ev.key === "Enter") select(row.character_id); });
  return li;
}

const AX_STATUS = {
  no_scope: ["no Ansiblex data", "Re-authorize this character (Add character) to let the app search the Ansiblex gates it can use"],
  pending: ["reading gates…", "The gates this character can use are being read from ESI; refresh in a moment"],
  error: ["gate read failed", "ESI refused the Ansiblex search for this character; it is retried later"],
};
function ansiblexNote(row) {
  const a = row.ansiblex;
  if (!a || !AX_STATUS[a.status]) return null;
  const badge = el("span", "badge stale", AX_STATUS[a.status][0]);
  badge.title = AX_STATUS[a.status][1];
  return badge;
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
  const wh = row && row.wormhole && row.wormhole.used ? row.wormhole : null;  // two gate legs joined by a wormhole
  const ax = row && row.ansiblex && row.ansiblex.used ? row.ansiblex : null;  // gate legs joined by Ansiblex jumps
  map.setScene({ targetId: r.target.id, routeIds: row && row.route, originName: row && row.name, markers,
    legs: wh || ax ? row.route_legs : null });
  $("#legend-wh").hidden = !wh;
  $("#legend-ax").hidden = !ax;
  const bar = $("#route-bar");
  bar.innerHTML = "";
  if (!row) { bar.hidden = true; return; }
  const title = el("div", "title");
  if (row.reachable) {
    const s = row.route_summary;
    title.append(document.createTextNode(`${row.name}: ${row.system.name} → ${r.target.name} · ${row.jumps} jumps `),
      el("span", "muted", `(${s.high} high · ${s.low} low · ${s.null} null)`));
    if (wh) {
      const direct = wh.direct_jumps == null ? "no gate route" : `${wh.direct_jumps} jumps by gates`;
      title.append(el("div", "wh-note",
        `via ${wh.hub}: ${wh.gates_to_entry} gates to ${wh.entry.name}, wormhole in and out, ` +
        `${wh.gates_from_exit} gates from ${wh.exit.name} (${direct} otherwise)`));
    } else if (row.wormhole) {
      title.append(el("div", "wh-note",
        `${row.wormhole.hub} would take ${row.wormhole.jumps} jumps; the gate route is not longer, so it is kept`));
    }
    if (ax) {
      const direct = ax.direct_jumps == null ? "no gate route" : `${ax.direct_jumps} jumps by stargates`;
      title.append(el("div", "ax-note",
        `${ax.hops.length} Ansiblex jump${ax.hops.length === 1 ? "" : "s"}: ` +
        ax.hops.map((h) => `${h.from.name} » ${h.to.name}`).join(", ") + ` (${direct} otherwise)`));
    } else if (row.ansiblex && row.ansiblex.status === "ok") {
      title.append(el("div", "ax-note", row.ansiblex.hops.length
        ? `Ansiblex would take ${row.ansiblex.jumps} jumps; the stargate route is not longer, so it is kept`
        : `No Ansiblex gate shortens this route (${row.ansiblex.gates} gate${row.ansiblex.gates === 1 ? "" : "s"} usable)`));
    }
    const path = el("div", "path");
    const hopAt = (a, b) => ax && ax.hops.find((h) => h.from.id === a && h.to.id === b);
    row.route.forEach((id, k) => {
      const i = map.indexOf(id);
      if (k) path.append(el("span", "arrow", "›"));
      if (wh && k === wh.gates_to_entry + 1) {  // the wormhole sits between the two gate legs
        const hop = el("span", "hop wh", `${wh.hub} wormhole`);
        hop.title = `${wh.entry.name} → ${wh.hub} → ${wh.exit.name}`;
        const icon = sizeIcon(wh.min_size, "Largest ship through both wormholes");
        if (icon) hop.append(icon);
        path.append(hop, el("span", "arrow", "›"));
      }
      if (k && hopAt(row.route[k - 1], id)) {  // an Ansiblex jump between two systems
        const hop = el("span", "hop ax", "Ansiblex");
        hop.title = `${map.names[map.indexOf(row.route[k - 1])]} » ${map.names[i]}`;
        path.append(hop, el("span", "arrow", "›"));
      }
      const hop = el("span", "hop");
      hop.append(secChip(map.sec[i]), el("span", null, map.names[i]));
      path.append(hop);
      // the systems next to the hub: where to find the wormhole (InSig) and its other end (OutSig)
      const sig = wh && k === wh.gates_to_entry ? sigTag(wh.entry, wh.hub)
        : wh && k === wh.gates_to_entry + 1 ? sigTag(wh.exit, wh.hub) : null;
      if (sig) path.append(sig);
    });
    bar.append(title, path, routeButton(row, r.target));
  } else {
    title.textContent = row.system
      ? `${row.name} is in ${row.system.name} (${row.system.region}): no stargate route to ${r.target.name}.`
      : `${row.name}: location unknown.`;
    bar.append(title);
  }
  bar.hidden = false;
}

function renderThera(r) {
  const info = $("#thera-info");
  if (!info) return;
  const e = r.evescout;
  if (r.ansiblex && !e) {
    const a = r.ansiblex;
    info.hidden = false;
    info.textContent = `Ansiblex: ${a.gates_known} gate${a.gates_known === 1 ? "" : "s"} known` +
      (a.reading ? " · reading the gates your characters can use…" : "") + (a.error ? ` · last read failed (${a.error})` : "");
    return;
  }
  info.hidden = !e;
  if (!e) return;
  const shown = Object.entries(e.hubs).filter(([name]) => !e.selected || e.selected.includes(name));
  const hubs = shown.map(([name, n]) => `${name} ${n}`).join(" · ");
  const total = shown.reduce((sum, [, n]) => sum + n, 0);
  const when = e.fetched_at ? new Date(e.fetched_at * 1000).toLocaleTimeString() : "never";
  info.textContent = total
    ? `EVE-Scout: ${total} listed systems (${hubs}), read ${when}` + (e.demo ? " · demo sample" : "")
    : `EVE-Scout: no connections listed${e.selected ? ` for ${e.selected.join(", ")}` : ""}`;
  if (e.error) info.textContent += ` · last read failed (${e.error})`;
}

const hubRadios = document.querySelectorAll('#hub-toggle input[name="hub"]');
if (hubRadios.length) {
  if (![...hubRadios].some((r) => r.value === state.hub)) state.hub = "";  // a hub that is no longer configured
  hubRadios.forEach((radio) => {
    radio.checked = radio.value === state.hub;
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      state.hub = radio.value;
      Prefs.set("rank", { hub: state.hub });
      if (state.target) rank(state.target.name, { refresh: 1, keepSelection: true });
    });
  });
}

/* ---------------------------------------------------------------- set the in-game destination */
function routeButton(row, target) {
  const wrap = el("div", "set-route");
  const btn = el("button", "btn small-btn", `Set destination: ${target.name}`);
  btn.type = "button";
  const note = el("span", "small");
  if (!row.can_set_route) {
    btn.disabled = true;
    note.textContent = " re-authorize this character (Add character) to allow it";
  } else if (!row.online) {
    btn.disabled = true;
    note.textContent = " only for an online character";
  }
  btn.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    btn.disabled = true;
    try {
      const res = await api(`/api/characters/${row.character_id}/destination`,
        { method: "POST", body: JSON.stringify({ destination_id: target.id }) });
      note.textContent = res.simulated ? " demo: destination not sent" : ` autopilot destination set to ${res.destination}`;
    } catch (err) {
      note.textContent = ` ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });
  wrap.append(btn, note);
  return wrap;
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

/* map options and auto-refresh, remembered in this browser (prefs cookie) */
const mapPrefs = Prefs.get("map", { labels: true, chars: true, gates: true });
Object.assign(map.opts, mapPrefs);
document.querySelectorAll("#map-opts input[data-opt]").forEach((box) => {
  box.checked = Boolean(map.opts[box.dataset.opt]);
  box.addEventListener("change", () => {
    map.opts[box.dataset.opt] = box.checked;
    Prefs.set("map", { [box.dataset.opt]: box.checked });
    map.draw();
  });
});
$("#map-opts-btn").addEventListener("click", () => {
  const panel = $("#map-opts");
  panel.hidden = !panel.hidden;
  $("#map-opts-btn").setAttribute("aria-expanded", String(!panel.hidden));
});

const refreshPrefs = Prefs.get("rank", { every: autoRefresh });
let refreshTimer = null;
function setAutoRefresh(seconds) {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
  const select = $("#auto-refresh");
  if (![...select.options].some((o) => Number(o.value) === seconds)) {
    select.append(new Option(`${seconds} s`, String(seconds)));
  }
  select.value = String(seconds);
  if (seconds > 0) {
    refreshTimer = setInterval(() => {
      if (document.visibilityState === "visible" && state.target && !state.busy) {
        rank(state.target.name, { keepSelection: true });
      }
    }, seconds * 1000);
  }
}
setAutoRefresh(Math.max(0, Number(refreshPrefs.every) || 0));
$("#auto-refresh").addEventListener("change", (ev) => {
  const seconds = Number(ev.target.value) || 0;
  Prefs.set("rank", { every: seconds });
  setAutoRefresh(seconds);
});

(async () => {
  await map.load("/api/universe/map");
  loadRecent();
  const t = new URLSearchParams(location.search).get("target");
  if (t) rank(t);
})();

/* ---------------------------------------------------------------- static roster (UC-12, ROS-16..ROS-21)
   Uploaded char_name + star_system_name entries: ranked against the same target, never sent to ESI. */
const roster = { target: null, result: null, selected: null, stats: null, busy: false };

function rosterCount() {
  const span = $("#roster-count");
  span.textContent = roster.stats && roster.stats.entries ? ` ${roster.stats.entries}` : "";
}

async function loadRosterStats() {
  try {
    roster.stats = (await api("/api/roster")).stats;
    rosterCount();
  } catch (_) { /* optional */ }
}

function rosterChanged() {
  roster.result = null;
  if (state.tab === "roster") rankRoster();
}

async function rankRoster() {
  if (!state.target || roster.busy) return;
  if (roster.result && roster.target === state.target.name) { renderRoster(); return; }
  roster.busy = true;
  $("#roster-status").textContent = "Updating…";
  try {
    const result = await api(`/api/roster?target=${encodeURIComponent(state.target.name)}`);
    roster.result = result;
    roster.target = state.target.name;
    roster.stats = result.stats;
    rosterCount();
    if (!result.rows.some((r) => r.key === roster.selected)) {
      const pick = result.rows.find((r) => r.recommended) || result.rows[0];
      roster.selected = pick ? pick.key : null;
    }
    renderRoster();
    const st = result.stats;
    $("#roster-status").textContent = `${st.ranked} of ${st.entries} roster characters · ${st.reachable} reachable` +
      `${st.unresolved ? ` · ${st.unresolved} unresolved` : ""} · ${st.total_ms} ms`;
  } catch (err) {
    $("#roster-status").textContent = `Error: ${err.message}`;
  } finally {
    roster.busy = false;
  }
}

function renderRoster() {
  const list = $("#roster-rows");
  list.innerHTML = "";
  const rows = roster.result ? roster.result.rows : [];
  $("#roster-empty").hidden = rows.length > 0;
  rows.forEach((row, n) => list.append(renderRosterRow(row, n + 1)));
  showRosterSelection();
}

function renderRosterRow(row, n) {
  const li = el("li", "row static");
  li.dataset.key = row.key;
  if (!row.reachable) li.classList.add("unreachable");
  if (row.key === roster.selected) li.classList.add("selected");
  li.tabIndex = 0;

  const who = el("div", "who");
  const name = el("div", "name", row.name);
  if (row.recommended) name.append(el("span", "badge closest", "Closest"));
  name.append(el("span", "badge static", "Static"));
  if (row.shadowed) name.append(el("span", "badge shadowed", "Also linked"));
  if (row.resolve_state !== "resolved") name.append(el("span", "badge unresolved", row.reason));
  who.append(name);

  const where = el("div", "where");
  if (row.system) {
    where.append(secChip(row.system.security), el("span", null, `${row.system.name} · ${row.system.region}`));
  } else {
    where.append(el("span", null, `"${row.system_name}" — ${row.reason}`));
    if (row.suggestions && row.suggestions.length) {
      where.append(el("span", "suggest-fix", `did you mean ${row.suggestions.join(", ")}?`));
    }
  }
  who.append(where);

  const extra = [row.tags.join(", "), row.note].filter(Boolean).join(" · ");
  who.append(el("div", "meta", extra || "static entry · no ship or online state"));

  const jumps = el("div", "jumps");
  if (row.reachable) {
    jumps.append(el("b", null, String(row.jumps)), el("span", null, row.jumps === 1 ? "jump" : "jumps"));
  } else {
    jumps.append(el("b", null, row.system ? "No gate route" : "Unknown system"));
  }

  li.append(el("div", "rank", String(n)), who, jumps);
  li.addEventListener("click", () => selectStatic(row.key));
  li.addEventListener("keydown", (ev) => { if (ev.key === "Enter") selectStatic(row.key); });
  return li;
}

function selectStatic(key) {
  roster.selected = key;
  document.querySelectorAll("#roster-rows .row").forEach((li) => li.classList.toggle("selected", li.dataset.key === key));
  showRosterSelection();
}

function showRosterSelection() {
  const r = roster.result;
  if (!r) return;
  const row = r.rows.find((x) => x.key === roster.selected);
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
    // no Set destination: a static entry has no token (D10)
    bar.append(title, path, el("div", "small", "Roster entry: jumps only, no ship, online state or destination."));
  } else {
    title.textContent = row.system
      ? `${row.name} is in ${row.system.name} (${row.system.region}): no stargate route to ${r.target.name}.`
      : `${row.name}: "${row.system_name}" is not a known solar system — fix it on the Roster page.`;
    bar.append(title);
  }
  bar.hidden = false;
}

/* ---------------------------------------------------------------- tabs (remembered in this browser) */
function setTab(tab) {
  state.tab = tab;
  Prefs.set("rank", { tab });
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  $("#live-pane").hidden = tab !== "live";
  $("#roster-pane").hidden = tab !== "roster";
  if (tab === "roster") rankRoster();
  else showSelection();
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
setTab(Prefs.get("rank", { tab: "live" }).tab === "roster" ? "roster" : "live");
loadRosterStats();


// The mobile drawer uses the same live controls, selections and event handlers as the desktop layout.
function setupMobileControls() {
  const mobile = window.matchMedia("(max-width: 820px)");
  const drawer = $("#mobile-map-controls");
  const gear = $("#mobile-map-menu");
  const sections = [".topbar", ".demo-banner", ".map-tools", "#map-opts", "#route-bar", ".list-pane"]
    .map(selector => $(selector)).filter(Boolean)
    .map(node => {
      const slot = document.createComment("desktop position");
      node.before(slot);
      return { node, slot };
    });

  function updateLayout() {
    if (!mobile.matches && drawer.open) drawer.close();
    for (const { node, slot } of sections) {
      if (mobile.matches) drawer.append(node);
      else slot.after(node);
    }
  }

  gear.addEventListener("click", () => drawer.showModal());
  mobile.addEventListener("change", updateLayout);
  updateLayout();
}
setupMobileControls();
