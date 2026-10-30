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
const SPACE = { high: "high-sec", low: "low-sec", null: "null-sec", wormhole: "wormhole" };
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
  if (r.system.space === "wormhole") {
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
  if (!r.access) seen.append(el("div", null, "no docking access now"));
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
$("#s-more").addEventListener("click", () => load(true));
load();
