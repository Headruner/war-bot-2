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

function row(r) {
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
  const seen = el("td", "small");
  seen.append(el("div", null, ago(r.last_seen)), el("div", null, `${r.seen_by} character${r.seen_by === 1 ? "" : "s"}`));
  if (!r.access && r.status === "ok") seen.append(el("div", null, "structure details currently unavailable from ESI"));
  tr.append(jumps, sys, st, owner, seen);
  return tr;
}

const DEFAULTS = { q: "", space: "", region: "", type: "", war: false, sort: "jumps" };
const prefs = Prefs.get("structures", DEFAULTS);

async function load(append = false) {
  if (!append) offset = 0;
  const f = { q: $("#s-q").value.trim(), space: $("#s-space").value, region: $("#s-region").value,
    type: $("#s-type").value, war: $("#s-war").checked, sort: $("#s-sort").value };
  Prefs.set("structures", f);
  const qs = new URLSearchParams({ q: f.q, space: f.space, region: f.region, type: f.type, at_war: f.war ? 1 : 0,
    sort: f.sort, limit: PAGE, offset });
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
let typing = null;
$("#s-q").addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => load(), 300); });
["#s-space", "#s-region", "#s-type", "#s-war", "#s-sort"].forEach((id) => $(id).addEventListener("change", () => load()));
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
load();
poll();  // a job that is already running (page reloaded, or started by another member) is shown at once
