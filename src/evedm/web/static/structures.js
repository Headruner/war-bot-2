/* EVE Distance Meter - Structures page: player-owned structures found through every registered character's assets,
   nearest to your own characters first. Filters are remembered in this browser (prefs cookie). */
"use strict";

async function api(url) {
  const resp = await fetch(url, { credentials: "same-origin", headers: { "X-EVEDM": "1" } });
  if (resp.status === 401) { location.href = "/login"; return new Promise(() => {}); }  // signed out: leave quietly
  if (!resp.ok) throw new Error(resp.statusText);
  return resp.json();
}
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const PAGE = 100;
const SPACE = { high: "high-sec", low: "low-sec", null: "null-sec", wormhole: "wormhole", unknown: "unknown space" };
const STATUS = { ok: "readable", no_access: "structure details unavailable from ESI", unread: "not read yet" };
let offset = 0;

function secChip(security) {
  const chip = el("span", "sec", secText(security));
  chip.style.background = secColor(security);
  return chip;
}
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
function extLink(text, href) {
  if (!href) return el("span", null, text);
  const a = el("a", null, text);
  a.href = href; a.target = "_blank"; a.rel = "noopener";
  return a;
}
function fillSelect(sel, facets, keep, label = (x) => x) {
  sel.innerHTML = "";
  sel.append(new Option("all", ""));
  facets.forEach(([name, n]) => sel.append(new Option(`${label(name)} (${n})`, name)));
  if (keep && ![...sel.options].some((o) => o.value === keep)) sel.append(new Option(keep, keep));
  sel.value = keep || "";
}

function row(r, showSource = source) {
  const tr = el("tr");
  const jumps = el("td", "num jumps");
  jumps.append(el("b", null, r.jumps == null ? "—" : String(r.jumps)));
  if (r.nearest && r.nearest.characters.length) jumps.append(el("div", "small", r.nearest.characters[0]));
  else if (r.system.space === "wormhole") jumps.append(el("div", "small", "no gates"));
  const sys = el("td");
  if (r.system.space === "wormhole" || r.system.space === "unknown") {
    sys.append(el("span", "target-chip", r.system.name));
  } else {
    const a = el("a", "target-chip");
    a.href = `/?target=${encodeURIComponent(r.system.name)}`;
    a.title = `Rank my characters by jumps to ${r.system.name}`;
    a.append(secChip(r.system.security), document.createTextNode(` ${r.system.name}`));
    sys.append(a);
  }
  sys.append(el("div", "small", `${r.system.region} · ${SPACE[r.system.space] || r.system.space}`));
  const st = el("td");
  const prefix = `${r.system.name} - `;
  st.append(el("div", null, r.name.startsWith(prefix) ? r.name.slice(prefix.length) : r.name), el("div", "small", r.type));
  st.append(el("div", "small", `ID ${r.structure_id} · ${STATUS[r.status] || r.status}`));
  if (r.historical_source) {
    const date = r.historical_date ? " · last seen " + r.historical_date : "";
    st.append(el("div", "small", "Historical details: " + r.historical_source + date));
  }
  const owner = el("td");
  const line = el("div");
  line.append(extLink(r.owner, r.owner_zkill));
  owner.append(line);
  if (r.wars.length) {
    const w = el("div", "small");
    w.append(el("span", "badge state-fighting", "at war"), document.createTextNode(" "));
    r.wars.slice(0, 3).forEach((id, k) => {
      if (k) w.append(document.createTextNode(", "));
      w.append(extLink(`#${id}`, `https://zkillboard.com/war/${id}/`));
    });
    owner.append(w);
  }
  if (r.mail) st.append(el("div", "small", `Named in a mail · ${r.mail.date}${r.mail.owner ? " · owner from the mail" : ""}`));
  const seen = el("td", "small");
  seen.append(el("div", null, r.last_seen == null ? "public dataset" : ago(r.last_seen)));
  if (r.seen_by || showSource === "assets") seen.append(el("div", null, `${r.seen_by} character${r.seen_by === 1 ? "" : "s"}`));
  // who may dock is shown as a count only, and only when the page also lists structures without assets
  if (r.dock_by && showSource !== "assets") seen.append(el("div", null, `${r.dock_by} can dock`));
  if (!r.access && r.status === "ok") seen.append(el("div", null, "structure details currently unavailable from ESI"));
  tr.append(jumps, sys, st, owner, seen);
  return tr;
}

let source = "assets"; // the Source filter: assets | dock | all
const DEFAULTS = { q: "", space: "", region: "", type: "", war: false, sort: "jumps", source: "assets", tab: "list" };
const prefs = Prefs.get("structures", DEFAULTS);

async function load(append = false) {
  if (!append) offset = 0;
  const f = { q: $("#s-q").value.trim(), space: $("#s-space").value, region: $("#s-region").value,
    type: $("#s-type").value, war: $("#s-war").checked, sort: $("#s-sort").value, source: $("#s-source").value };
  source = f.source;
  Prefs.set("structures", f);
  const qs = new URLSearchParams({ q: f.q, space: f.space, region: f.region, type: f.type, at_war: f.war ? 1 : 0,
    source: f.source, sort: f.sort, limit: PAGE, offset });
  $("#s-info").textContent = "Loading…";
  const d = await api(`/api/structures?${qs}`);
  fillSelect($("#s-region"), d.facets.regions, f.region);
  fillSelect($("#s-type"), d.facets.types, f.type);
  const body = $("#s-rows");
  if (!append) body.innerHTML = "";
  d.rows.forEach((r) => body.append(row(r)));
  offset += d.rows.length;
  $("#s-more").hidden = offset >= d.matched;
  const parts = [`${d.matched} of ${d.facets.total} structures`,
    ...(d.facets.source === "assets" ? [] : [d.facets.source === "dock" ? "that characters can dock at, without assets"
      : d.facets.source === "mail" ? "named in mails"
      : `${d.facets.sources.assets} with assets, ${d.facets.sources.dock} dockable only, ${d.facets.sources.mail} named in mails`]),
    `${d.scout.contributing_characters} character${d.scout.contributing_characters === 1 ? "" : "s"} sharing`];
  if (d.scout.last_run) parts.push(`scouted ${ago(d.scout.last_run.at)}`);
  else if (d.scout.running) parts.push("first scout run in progress");
  if (!d.characters) parts.push("add characters to see jumps");
  $("#s-info").textContent = parts.join(" · ");
}

$("#s-q").value = prefs.q || "";
$("#s-space").value = prefs.space || "";
["#s-region", "#s-type"].forEach((id, k) => {
  const v = [prefs.region, prefs.type][k];
  if (v) $(id).append(new Option(v, v));
  $(id).value = v || "";
});
$("#s-war").checked = Boolean(prefs.war);
if ([...$("#s-sort").options].some((o) => o.value === prefs.sort)) $("#s-sort").value = prefs.sort;
if ([...$("#s-source").options].some((o) => o.value === prefs.source)) $("#s-source").value = prefs.source;
source = $("#s-source").value;
let typing = null;
$("#s-q").addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => load(), 300); });
["#s-space", "#s-region", "#s-type", "#s-war", "#s-sort", "#s-source"].forEach((id) => $(id).addEventListener("change", () => load()));
$("#s-reload").addEventListener("click", () => load());
// Resolve unknown is a background job on the server: the button starts it, this page polls its state every 5 s.
const num = (n) => Number(n || 0).toLocaleString("en-US").replace(/,/g, " ");
const STOPPED = { cancelled: "stopped on request", reauth: "no character with a valid token is left",
  esi_error: "ESI kept answering with errors, try again later", failed: "the job failed, see the server log" };
let polling = null, watched = false;

function jobLine(d) {
  const parts = [`${num(d.done)} of ${num(d.targets)} done — ${num(d.resolved)} resolved, ${num(d.no_access)} refused, ` +
    `${num(d.gone)} gone, ${num(d.skipped)} skipped`];
  let text = (d.running ? "Resolving: " : "Resolve unknown: ") + parts[0] + ".";
  if (d.running && d.paused_until) {
    text += ` Waiting ${Math.max(1, Math.round(d.paused_until - Date.now() / 1000))} s for ESI's error window.`;
  }
  if (!d.running && STOPPED[d.stopped]) text += ` Stopped: ${STOPPED[d.stopped]}.`;
  if (!d.running && d.stopped === "done") text += " Finished.";
  if (!d.running && d.remaining) text += ` ${num(d.remaining)} left.`;
  if (d.mails_read || d.mail_found) text += ` Mails: ${num(d.mails_read)} read, ${num(d.mail_found)} structures named.`;
  const h = d.history || {}, filled = Object.values(h).reduce((a, b) => a + b, 0);
  if (filled) text += ` Public datasets matched ${filled} (${Object.entries(h).map(([k, v]) => `${k} ${v}`).join(", ")}).`;
  return text;
}

function showJob(d) {
  $("#s-resolve").disabled = d.running;
  $("#s-resolve-stop").hidden = !d.running;
  if (d.running || watched) $("#s-resolve-info").textContent = jobLine(d);
}

async function poll() {
  clearTimeout(polling);
  let d;
  try {
    d = await api("/api/structures/resolve");
  } catch (err) {
    if (watched) polling = setTimeout(poll, 5000);  // a failed poll is retried while a job is being watched
    return;
  }
  showJob(d);
  if (d.running) {
    watched = true;
    polling = setTimeout(poll, 5000);
  } else if (watched) {
    watched = false;
    load();  // the job is over: the list has what it found
  }
}

async function jobRequest(url) {
  const resp = await fetch(url, { method: "POST", credentials: "same-origin", headers: { "X-EVEDM": "1" } });
  if (resp.status === 401) { location.href = "/login"; return null; }
  return { ok: resp.ok, status: resp.status, d: await resp.json() };
}

$("#s-resolve").addEventListener("click", async () => {
  const info = $("#s-resolve-info");
  $("#s-resolve").disabled = true;
  info.textContent = "Starting…";
  try {
    const r = await jobRequest("/api/structures/resolve");
    if (!r) return;
    if (!r.ok && r.status !== 409) { info.textContent = r.d.detail || r.d.error; $("#s-resolve").disabled = false; return; }
    watched = true;  // 409 busy: somebody else's job is running, shown the same way
    showJob(r.d);
    poll();
  } catch (err) {
    info.textContent = "Could not start: " + err.message;
    $("#s-resolve").disabled = false;
  }
});
$("#s-resolve-stop").addEventListener("click", async () => {
  $("#s-resolve-stop").disabled = true;
  try {
    await jobRequest("/api/structures/resolve/stop");
    await poll();
  } finally {
    $("#s-resolve-stop").disabled = false;
  }
});
$("#s-more").addEventListener("click", () => load(true));

// Search tab: citadels by place and owner, known and unknown. It searches on every change of a chip or a select.
const SEARCH_DEFAULTS = { state: "all", sort: "jumps", chips: [] };
const MAX_STORED_CHIPS = 12;  // the prefs cookie holds 3.5 KB: more chips than this would make it drop the write
const searchPrefs = Prefs.get("structures_search", SEARCH_DEFAULTS);
let searchOffset = 0, searchSeq = 0;
const places = MultiSelect($("#q-places"), { kind: "place", placeholder: "Jita, The Forge, …", onChange: () => search() });
const owners = MultiSelect($("#q-owners"), { kind: "owner", placeholder: "corporation or alliance", onChange: () => search() });

function pickedIds(chips, kind) { return chips.filter((c) => c.kind === kind).map((c) => c.id).join(","); }

async function search(append = false) {
  if (!append) searchOffset = 0;
  const p = places.value(), o = owners.value(), all = [...p, ...o];
  const state = $("#q-state").value, sort = $("#q-sort").value;
  Prefs.set("structures_search", { state, sort, chips: all.slice(0, MAX_STORED_CHIPS) });
  const body = $("#q-rows");
  if (!append) body.innerHTML = "";
  if (!all.length) {
    $("#q-more").hidden = true;
    $("#q-info").textContent = "Choose a system, region, corporation or alliance.";
    return;
  }
  const qs = new URLSearchParams({ systems: pickedIds(p, "system"), regions: pickedIds(p, "region"),
    corps: pickedIds(o, "corporation"), alliances: pickedIds(o, "alliance"), state, sort, limit: PAGE, offset: searchOffset });
  const seq = ++searchSeq;
  $("#q-info").textContent = "Searching…";
  let d;
  try {
    d = await api(`/api/structures/search?${qs}`);
  } catch (err) {
    if (seq === searchSeq) $("#q-info").textContent = "Search failed: " + err.message;
    return;
  }
  if (seq !== searchSeq) return;  // a newer search is under way
  d.rows.forEach((r) => body.append(row(r, "all")));
  searchOffset += d.rows.length;
  $("#q-more").hidden = searchOffset >= d.matched;
  const c = d.counts || { known: 0, unknown: 0 };
  const parts = [`${d.matched} citadel${d.matched === 1 ? "" : "s"}`, `${c.known} known, ${c.unknown} unknown`];
  if (!d.public_loaded) parts.push("public datasets not loaded yet");
  if ((d.unresolved_alliances || []).length) parts.push(`members of ${d.unresolved_alliances.length} alliance(s) could not be read`);
  if (!d.characters) parts.push("add characters to see jumps");
  $("#q-info").textContent = parts.join(" · ");
}

if ([...$("#q-state").options].some((x) => x.value === searchPrefs.state)) $("#q-state").value = searchPrefs.state;
if ([...$("#q-sort").options].some((x) => x.value === searchPrefs.sort)) $("#q-sort").value = searchPrefs.sort;
const stored = Array.isArray(searchPrefs.chips) ? searchPrefs.chips : [];
places.set(stored.filter((c) => c.kind === "system" || c.kind === "region"));
owners.set(stored.filter((c) => c.kind === "corporation" || c.kind === "alliance"));
["#q-state", "#q-sort"].forEach((id) => $(id).addEventListener("change", () => search()));
$("#q-more").addEventListener("click", () => search(true));

const tabLoaded = new Set();
function showTab(name, save = true) {
  if (name !== "search") name = "list";
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  document.querySelectorAll(".tab-panel").forEach((pn) => { pn.hidden = pn.id !== `tab-${name}`; });
  if (save) Prefs.set("structures", { tab: name });
  if (name === "search" && !tabLoaded.has(name)) { tabLoaded.add(name); search(); }
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
showTab(prefs.tab, false);
load();
poll();  // a job that is already running (page reloaded, or started by another member) is shown at once
