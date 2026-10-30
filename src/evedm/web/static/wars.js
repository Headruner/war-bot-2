/* EVE Distance Meter - Wars page: new wars, statistics of every active war, high-sec structures of both sides.
   The chosen tab and filters are remembered in this browser (prefs cookie). */
"use strict";

async function api(url) {
  const resp = await fetch(url, { credentials: "same-origin", headers: { "X-EVEDM": "1" } });
  if (resp.status === 401) { location.href = "/login"; return new Promise(() => {}); }  // signed out: leave quietly
  if (!resp.ok) throw new Error(resp.statusText);
  return resp.json();
}
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const TYPE_TEXT = { none: "War declared", mutual: "Mutual", open_for_allies: "Open for allies" };
const PAGE = 30;
let offset = 0;

function secChip(security) {
  const chip = el("span", "sec", secText(security));
  chip.style.background = secColor(security);
  return chip;
}
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
function isk(v) {
  v = Number(v || 0);
  for (const [u, n] of [["t", 1e12], ["b", 1e9], ["m", 1e6], ["k", 1e3]]) if (v >= n) return `${(v / n).toFixed(1)}${u}`;
  return v.toFixed(0);
}
const ZKB = "https://zkillboard.com";
function extLink(text, href, cls) {
  if (!href) return el("span", cls, text);
  const a = el("a", cls, text);
  a.href = href; a.target = "_blank"; a.rel = "noopener";
  return a;
}
const zkbWar = (id) => `${ZKB}/war/${id}/`;

function rankLink(sys) {
  const a = el("a", "target-chip");
  a.href = `/?target=${encodeURIComponent(sys.name)}`;
  a.title = `Rank my characters by jumps to ${sys.name}`;
  a.append(secChip(sys.security), document.createTextNode(` ${sys.name} `));
  if (sys.why && sys.why.length) a.append(el("span", "small", sys.why.join(", ")));
  return a;
}
function party(p, label) {
  const div = el("div", "party");
  if (p.logo) { const img = el("img"); img.src = p.logo; img.alt = ""; img.width = 32; img.height = 32; img.loading = "lazy"; div.append(img); }
  const txt = el("div");
  const a = el("a", null, p.name); a.href = p.dotlan_url; a.target = "_blank"; a.rel = "noopener";
  const line = el("div"); line.append(el("span", "small", `${label} `), a);
  if (p.ticker) line.append(el("span", "small", ` [${p.ticker}]`));
  txt.append(line);
  const extra = [];
  if (p.members != null) extra.push(`${p.members.toLocaleString()} members`);
  if (p.corporations) extra.push(`${p.corporations} corps`);
  if (extra.length) txt.append(el("div", "small", extra.join(", ")));
  div.append(txt);
  return div;
}
function structures(side, label) {
  if (!side) return null;
  const d = el("details", "structs");
  d.append(el("summary", null, `${label}: ${side.total ? `${side.total} public structure(s)` : "no public structures known"}`));
  if (side.total) {
    const types = Object.entries(side.by_type).map(([t, n]) => `${n}× ${t}`).join(", ");
    d.append(el("div", "small", types));
    const ul = el("ul");
    for (const s of side.structures) {
      const li = el("li");
      if (s.system) li.append(rankLink({ ...s.system, why: [] }));
      li.append(document.createTextNode(` ${s.name} `), el("span", "small", s.type || ""));
      ul.append(li);
    }
    d.append(ul);
  }
  return d;
}
function card(w) {
  const li = el("li", "card war");
  li.id = `war-${w.war_id}`;
  const head = el("div", "war-head");
  const title = el("a", "war-title", `${w.aggressor.name} → ${w.defender.name}`);
  title.href = w.report_url; title.target = "_blank"; title.rel = "noopener";
  head.append(title, el("span", `badge war-${w.type}`, TYPE_TEXT[w.type] || w.type));
  if (w.demo) head.append(el("span", "badge stale", "demo"));
  const when = el("span", "small when", `declared ${ago(w.declared)} · `);
  when.append(extLink(`war #${w.war_id} on zKillboard`, zkbWar(w.war_id)));
  head.append(when);
  li.append(head);
  const sides = el("div", "parties");
  sides.append(party(w.aggressor, "Aggressor"), party(w.defender, "Defender"));
  li.append(sides);

  const r = w.report;
  if (r && (r.kills_total || (r.aggressor || {}).ships_killed || (r.defender || {}).ships_killed)) {
    li.append(el("div", "small", `Score: aggressor ${r.aggressor.ships_killed || 0} kills / ${isk(r.aggressor.isk_destroyed)} ISK · `
      + `defender ${r.defender.ships_killed || 0} kills / ${isk(r.defender.isk_destroyed)} ISK`));
  }
  const targets = el("div", "targets");
  if (w.targets && w.targets.length) {
    targets.append(el("div", "small", "Rank my characters to:"));
    const chips = el("div", "chips");
    w.targets.forEach((t) => chips.append(rankLink(t)));
    targets.append(chips);
  } else {
    targets.append(el("div", "small", "No known structure or kill systems for this war."));
  }
  li.append(targets);
  if (w.structures) {
    [structures(w.structures.defender, "Defender"), structures(w.structures.aggressor, "Aggressor")]
      .filter(Boolean).forEach((d) => li.append(d));
  }
  const links = el("div", "small links");
  links.append(extLink("zKillboard war page", zkbWar(w.war_id)), document.createTextNode(" · "),
    extLink("Dotlan", w.report_url));
  li.append(links);
  return li;
}

async function load(append = false) {
  if (!append) offset = 0;
  savePrefs();
  const type = $("#war-type").value;
  const data = await api(`/api/wars?limit=${PAGE}&offset=${offset}&type=${encodeURIComponent(type)}`);
  const list = $("#wars");
  if (!append) list.innerHTML = "";
  const only = $("#only-targets").checked;
  data.wars.filter((w) => !only || (w.targets && w.targets.length)).forEach((w) => list.append(card(w)));
  offset += data.wars.length;
  $("#more").hidden = data.wars.length < PAGE;
  $("#empty").hidden = list.children.length > 0;
  const st = data.status;
  const bits = [];
  if (st.last_cycle_at) bits.push(`checked ${ago(new Date(st.last_cycle_at * 1000).toISOString())}`);
  else if (st.running) bits.push("first check running");
  bits.push(`every ${st.settings.poll_interval_min} min`);
  if (st.settings.discord) bits.push(st.settings.discord_dry_run ? "Discord: dry run" : "posting to Discord");
  if (st.last_error) bits.push(`last error: ${st.last_error}`);
  $("#watcher").textContent = bits.join(" · ");
  if (location.hash && !append) { const t = document.querySelector(location.hash); if (t) t.scrollIntoView(); }
}

/* ---------------------------------------------------------------- preferences + tabs */
const DEFAULTS = { tab: "feed", type: "", only: false, s_type: "", s_region: "", s_side: "", s_state: "",
  s_sec: "0.5", s_sort: "jumps" };
const prefs = Prefs.get("wars", DEFAULTS);
const tabs = [...document.querySelectorAll(".tab")].map((b) => b.dataset.tab);
const loaded = new Set();

function savePrefs() {
  const v = (id) => ($(id) ? $(id).value : undefined);
  Prefs.set("wars", {
    type: v("#war-type"), only: $("#only-targets").checked, s_type: v("#f-type"), s_region: v("#f-region"),
    s_side: v("#f-side"), s_state: v("#f-state"), s_sec: v("#f-sec"), s_sort: v("#f-sort"),
  });
}

function showTab(name, save = true) {
  if (!tabs.includes(name)) name = "feed";
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
  if (save) Prefs.set("wars", { tab: name });
  if (!loaded.has(name)) {
    loaded.add(name);
    ({ feed: () => load(), stats: loadStats, loot: () => loadLoot() })[name]();
  }
}

/* ---------------------------------------------------------------- statistics */
function tableRows(table, head, rows) {
  table.innerHTML = "";
  const tr = el("tr");
  head.forEach((h, i) => tr.append(el("th", i ? "num" : null, h)));
  const thead = el("thead"); thead.append(tr); table.append(thead);
  const body = el("tbody");
  if (!rows.length) { const r = el("tr"); const td = el("td", "small", "none"); td.colSpan = head.length; r.append(td); body.append(r); }
  rows.forEach((cells) => {
    const r = el("tr");
    cells.forEach((c, i) => r.append(c instanceof Node ? (() => { const td = el("td"); td.append(c); return td; })() : el("td", i ? "num" : null, String(c))));
    body.append(r);
  });
  table.append(body);
}

async function loadStats() {
  const d = await api("/api/wars/stats");
  const t = d.totals;
  const k = $("#kpis");
  k.innerHTML = "";
  [["Active wars", t.active], ["Fighting", t.fighting], ["Starting within 24 h", t.pending], ["Ending", t.ending],
    ["Mutual", t.mutual], ["Declared in 24 h", t.declared_24h], ["Declared in 7 days", t.declared_7d],
    ["Ships killed", t.ships_killed.toLocaleString()], ["ISK destroyed", isk(t.isk_destroyed)],
    ["High-sec structures at war", d.structures.highsec]].forEach(([label, value]) => {
    const c = el("div", "kpi");
    c.append(el("b", null, String(value)), el("span", null, label));
    k.append(c);
  });
  const who = (e) => extLink(`${e.name}${e.type === "alliance" ? " (alliance)" : ""}`, e.zkill);
  tableRows($("#top-aggressors"), ["Aggressor", "Wars"], d.top_aggressors.map((e) => [who(e), e.wars]));
  tableRows($("#top-defenders"), ["Defender", "Wars"], d.top_defenders.map((e) => [who(e), e.wars]));
  tableRows($("#top-isk"), ["War", "Kills", "ISK"],
    d.top_isk.map((w) => [extLink(`${w.aggressor} → ${w.defender}`, w.zkill), w.ships_killed, isk(w.isk_destroyed)]));
  tableRows($("#by-type"), ["Structure type", "Count"], d.structures.by_type);
  tableRows($("#by-region"), ["Region", "Count"], d.structures.by_region);
  const c = d.census;
  const scan = c.complete ? "scan complete" : `scan running: ${c.examined_ids.toLocaleString()} of ${c.max_ids.toLocaleString()} newest war IDs examined`;
  const when = c.last_run ? `, updated ${ago(new Date(c.last_run.at * 1000).toISOString())}` : "";
  $("#census").textContent = `${c.known_wars.toLocaleString()} wars known, ${scan}${when}.` +
    (d.structures.loaded ? "" : " Structure data not loaded yet.");
}

/* ---------------------------------------------------------------- high-sec structures */
const LOOT_PAGE = 100;
let lootOffset = 0;

function fillSelect(sel, facets, keep) {
  const current = keep || sel.value;
  sel.innerHTML = "";
  sel.append(new Option("all", ""));
  facets.forEach(([name, n]) => sel.append(new Option(`${name} (${n})`, name)));
  if (current && ![...sel.options].some((o) => o.value === current)) sel.append(new Option(current, current));
  sel.value = current;
}

function until(iso) {
  const s = (Date.parse(iso) - Date.now()) / 1000;
  if (!(s > 0)) return "now";
  if (s < 3600) return `in ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 172800) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} days`;
}

function warCell(r) {
  const td = el("td", "wars-cell");
  r.wars.slice(0, 3).forEach((w) => {
    const line = el("div", "small");
    const label = w.state === "pending" ? `starts ${until(w.started)}` : w.state === "ending" ? `ends ${until(w.finished)}` : "fighting";
    line.append(el("span", `badge state-${w.state}`, label), document.createTextNode(` ${w.side} vs ${w.opponent} · `),
      extLink(`war #${w.war_id}`, w.zkill || zkbWar(w.war_id)));
    line.title = `war #${w.war_id}` + (w.started ? `, fighting from ${new Date(w.started).toLocaleString()}` : "") +
      (w.finished ? `, ends ${new Date(w.finished).toLocaleString()}` : "");
    td.append(line);
  });
  if (r.wars.length > 3) td.append(el("div", "small", `+${r.wars.length - 3} more wars`));
  return td;
}

function lootRow(r) {
  const tr = el("tr");
  const jumps = el("td", "num jumps");
  jumps.append(el("b", null, r.jumps == null ? "—" : String(r.jumps)));
  if (r.nearest && r.nearest.characters.length) jumps.append(el("div", "small", r.nearest.characters[0]));
  const sys = el("td");
  sys.append(rankLink({ ...r.system, why: [] }), el("div", "small", r.system.region));
  const st = el("td");
  st.append(el("div", null, r.name), el("div", "small", r.type));
  const owner = el("td");
  const ownerLine = el("div");
  ownerLine.append(extLink(r.owner, r.owner_zkill));
  owner.append(ownerLine);
  const via = r.entities.filter((e) => e.id !== r.owner_id).map((e) => e.name);
  owner.append(el("div", "small", via.length ? `of ${via.join(", ")}` : r.sides.join(", ")));
  tr.append(jumps, sys, st, owner, warCell(r));
  return tr;
}

async function loadLoot(append = false) {
  if (!append) lootOffset = 0;
  savePrefs();
  const q = new URLSearchParams({ type: $("#f-type").value, region: $("#f-region").value, side: $("#f-side").value,
    state: $("#f-state").value, min_sec: $("#f-sec").value, sort: $("#f-sort").value, limit: LOOT_PAGE, offset: lootOffset });
  $("#loot-info").textContent = "Loading…";
  const d = await api(`/api/wars/structures?${q}`);
  fillSelect($("#f-type"), d.facets.types, $("#f-type").value);
  fillSelect($("#f-region"), d.facets.regions, $("#f-region").value);
  const body = $("#loot-rows");
  if (!append) body.innerHTML = "";
  d.rows.forEach((r) => body.append(lootRow(r)));
  lootOffset += d.rows.length;
  $("#loot-more").hidden = lootOffset >= d.matched;
  const parts = [`${d.matched} of ${d.facets.total} structures`];
  if (!d.characters) parts.push("no characters with a known location: add characters to see jumps");
  if (!d.structures_loaded) parts.push("structure data not loaded yet");
  $("#loot-info").textContent = parts.join(" · ");
}

/* ---------------------------------------------------------------- wiring */
$("#war-type").value = prefs.type;
$("#only-targets").checked = Boolean(prefs.only);
$("#war-type").addEventListener("change", () => load());
$("#only-targets").addEventListener("change", () => load());
$("#reload").addEventListener("click", () => load());
$("#more").addEventListener("click", () => load(true));
if ($("#f-type")) {
  [["#f-type", prefs.s_type], ["#f-region", prefs.s_region]].forEach(([id, v]) => {
    if (v) $(id).append(new Option(v, v));
    $(id).value = v;
  });
  [["#f-side", prefs.s_side], ["#f-state", prefs.s_state], ["#f-sec", prefs.s_sec], ["#f-sort", prefs.s_sort]]
    .forEach(([id, v]) => { if ([...$(id).options].some((o) => o.value === v)) $(id).value = v; });
  ["#f-type", "#f-region", "#f-side", "#f-state", "#f-sec", "#f-sort"].forEach((id) => $(id).addEventListener("change", () => loadLoot()));
  $("#loot-reload").addEventListener("click", () => loadLoot());
  $("#loot-more").addEventListener("click", () => loadLoot(true));
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
setInterval(() => {
  if (document.hidden) return;
  if (!$("#tab-feed").hidden && offset <= PAGE) load();
  if ($("#tab-stats") && !$("#tab-stats").hidden) loadStats();
}, 60000);
showTab(location.hash.startsWith("#war-") ? "feed" : prefs.tab, false);
