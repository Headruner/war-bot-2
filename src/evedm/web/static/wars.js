/* EVE Distance Meter - Wars page: war declarations with the systems to rank characters against. */
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
  head.append(el("span", "small when", `declared ${ago(w.declared)} · #${w.war_id}`));
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
  if (r && r.zkill_war_url) { const a = el("a", null, "zKillboard"); a.href = r.zkill_war_url; a.target = "_blank"; a.rel = "noopener"; links.append(a); }
  if (links.childNodes.length) li.append(links);
  return li;
}

async function load(append = false) {
  if (!append) offset = 0;
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

$("#war-type").addEventListener("change", () => load());
$("#only-targets").addEventListener("change", () => load());
$("#reload").addEventListener("click", () => load());
$("#more").addEventListener("click", () => load(true));
setInterval(() => { if (!document.hidden && offset <= PAGE) load(); }, 60000);
load();
