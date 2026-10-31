/* EVE Distance Meter - Alerts page: structure alerts from zKillboard's live feed, closest characters, set destination.
   Filters, notification and sound choices and the last alert seen are remembered in this browser (prefs cookie). */
"use strict";

async function api(url, opts = {}) {
  const resp = await fetch(url, { credentials: "same-origin", ...opts,
    headers: { "X-EVEDM": "1", ...(opts.body ? { "Content-Type": "application/json" } : {}) } });
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
const SPACE = { high: "high-sec", low: "low-sec", null: "null-sec", wormhole: "wormhole" };
const STATE = { destroyed: "Destroyed", combat: "Combat", firing: "Firing" };

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
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
function isk(v) {
  v = Number(v || 0);
  for (const [u, n] of [["t", 1e12], ["b", 1e9], ["m", 1e6], ["k", 1e3]]) if (v >= n) return `${(v / n).toFixed(1)}${u}`;
  return v.toFixed(0);
}
function extLink(text, href) {
  if (!href) return el("span", null, text);
  const a = el("a", null, text);
  a.href = href; a.target = "_blank"; a.rel = "noopener";
  return a;
}

/* ---------------------------------------------------------------- set destination */
async function setDestination(c, system, btn, note) {
  btn.disabled = true;
  try {
    const res = await api(`/api/characters/${c.character_id}/destination`,
      { method: "POST", body: JSON.stringify({ destination_id: system.id }) });
    note.textContent = res.simulated ? "demo: not sent" : `destination set to ${res.destination}`;
  } catch (err) {
    note.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

function closestList(g) {
  const box = el("div", "closest");
  if (g.system.space === "wormhole") { box.append(el("div", "small", "Wormhole space: no stargate route.")); return box; }
  if (!g.closest.length) { box.append(el("div", "small", "None of your characters can reach it by gates (or no location yet).")); return box; }
  box.append(el("div", "small", "Your closest characters:"));
  const ul = el("ul");
  g.closest.forEach((c) => {
    const li = el("li");
    li.append(el("b", null, c.name), document.createTextNode(` · ${c.jumps} jump${c.jumps === 1 ? "" : "s"} from ${c.system} `));
    li.append(c.online ? el("span", "badge online", "Online") : el("span", "small", "offline"));
    const btn = el("button", "btn small-btn", "Set destination");
    btn.type = "button";
    const note = el("span", "small");
    if (!c.can_set_route) { btn.disabled = true; btn.title = "Re-authorize this character (Add character) to allow it"; }
    else if (!c.online) { btn.disabled = true; btn.title = "Only for an online character"; }
    btn.addEventListener("click", () => setDestination(c, g.system, btn, note));
    li.append(document.createTextNode(" "), btn, document.createTextNode(" "), note);
    ul.append(li);
  });
  box.append(ul);
  return box;
}

function card(g) {
  const li = el("li", `card war alert-${g.state}`);
  li.id = `alert-${g.alerts[0].alert_id}`;
  li.dataset.alerts = g.alerts.map((a) => a.alert_id).join(" ");
  const head = el("div", "war-head");
  head.append(el("span", `badge state-${g.state === "destroyed" ? "fighting" : g.state === "combat" ? "pending" : "ending"}`, STATE[g.state]));
  if (g.core_drop) head.append(el("span", "badge closest", "Core drop"));
  head.append(el("span", "war-title", g.structure.name),
    el("span", "small", g.structure.type));
  head.append(el("span", "small when", ago(g.latest)));
  li.append(head);

  const where = el("div", "where-line");
  if (g.system.space === "wormhole") where.append(el("span", "target-chip", g.system.name));
  else {
    const a = el("a", "target-chip");
    a.href = `/?target=${encodeURIComponent(g.system.name)}`;
    a.title = `Rank all my characters by jumps to ${g.system.name}`;
    a.append(secChip(g.system.security), document.createTextNode(` ${g.system.name}`));
    where.append(a);
  }
  where.append(el("span", "small", ` ${g.system.region || ""} · ${SPACE[g.system.space] || ""} · owner `), extLink(g.owner.name, g.owner.zkill));
  li.append(where);

  const counts = Object.entries(g.counts).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(" · ");
  li.append(el("div", "small", counts));
  const kills = el("ul", "kills");
  g.alerts.forEach((a) => {
    const k = el("li", "small");
    const what = a.kind === "destroyed" ? `${a.structure.type} destroyed (${isk(a.isk)} ISK)`
      : a.kind === "combat" ? `${a.victim.type} of ${a.victim.corp} killed ${a.distance_km} km away`
      : `firing on ${a.victim.type} of ${a.victim.corp}`;
    k.append(document.createTextNode(`${ago(a.time)}: `), extLink(what, a.zkill));
    kills.append(k);
  });
  li.append(kills, closestList(g));
  return li;
}

/* ---------------------------------------------------------------- preferences, notifications */
const DEFAULTS = { kinds: ["destroyed", "combat", "firing"], core: false, space: "", hours: "24", notify: false,
  sound: false, seen: 0 };
const prefs = Prefs.get("alerts", DEFAULTS);
let firstLoad = true;

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = 880; g.gain.value = 0.08;
    o.connect(g); g.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + 0.25);
  } catch (_) { /* no audio */ }
}

function announce(groups, newest) {
  const fresh = groups.filter((g) => g.alerts.some((a) => a.alert_id > prefs.seen));
  if (!fresh.length) return;
  if (prefs.notify && "Notification" in window && Notification.permission === "granted") {
    const g = fresh[0];
    const n = new Notification(`${STATE[g.state]}: ${g.structure.name}`, {
      body: `${g.system.name} · ${g.closest[0] ? `closest: ${g.closest[0].name} (${g.closest[0].jumps} j)` : "no character nearby"}` +
        (fresh.length > 1 ? ` · +${fresh.length - 1} more` : ""),
      tag: "evedm-alerts",
    });
    n.onclick = () => { window.focus(); const t = document.getElementById(`alert-${g.alerts[0].alert_id}`); if (t) t.scrollIntoView(); };
  }
  if (prefs.sound) beep();
}

function notifyLabel() {
  const on = prefs.notify && "Notification" in window && Notification.permission === "granted";
  $("#a-notify").textContent = `Desktop notifications: ${on ? "on" : "off"}`;
}

async function load() {
  const kinds = [...document.querySelectorAll(".a-kind:checked")].map((x) => x.value);
  Prefs.set("alerts", { kinds, core: $("#a-core").checked, space: $("#a-space").value, hours: $("#a-hours").value,
    sound: $("#a-sound").checked });
  const qs = new URLSearchParams({ kinds: kinds.join(","), core_only: $("#a-core").checked ? 1 : 0,
    space: $("#a-space").value, hours: $("#a-hours").value });
  const d = kinds.length ? await api(`/api/alerts?${qs}`) : { groups: [], newest_alert_id: 0, feed: {} };
  const list = $("#a-groups");
  list.innerHTML = "";
  d.groups.forEach((g) => list.append(card(g)));
  $("#a-empty").hidden = d.groups.length > 0;
  const f = d.feed;
  const parts = [`${d.groups.length} structure${d.groups.length === 1 ? "" : "s"}`];
  if (f.running) parts.push(`live feed on${f.last_kill ? `, last killmail ${ago(f.last_kill)}` : ""}`);
  else parts.push("live feed not running (demo or disabled)");
  if (f.last_error) parts.push(`last error: ${f.last_error}`);
  $("#a-info").textContent = parts.join(" · ");
  if (!firstLoad) announce(d.groups, d.newest_alert_id);
  firstLoad = false;
  if (d.newest_alert_id > prefs.seen) { prefs.seen = d.newest_alert_id; Prefs.set("alerts", { seen: d.newest_alert_id }); }
  focusHash();
}

function focusHash() {  // a link from Discord (#alert-<id>): the card that holds this alert
  if (!location.hash.startsWith("#alert-")) return;
  const id = location.hash.slice(7);
  const cards = [...document.querySelectorAll("#a-groups > li")];
  cards.forEach((x) => x.classList.remove("flash"));
  const t = cards.find((x) => x.dataset.alerts.split(" ").includes(id));
  if (t) { t.scrollIntoView(); t.classList.add("flash"); }
}

document.querySelectorAll(".a-kind").forEach((b) => { b.checked = (prefs.kinds || []).includes(b.value); b.addEventListener("change", load); });
$("#a-core").checked = Boolean(prefs.core);
$("#a-space").value = prefs.space || "";
if ([...$("#a-hours").options].some((o) => o.value === String(prefs.hours))) $("#a-hours").value = String(prefs.hours);
$("#a-sound").checked = Boolean(prefs.sound);
["#a-core", "#a-space", "#a-hours", "#a-sound"].forEach((id) => $(id).addEventListener("change", load));
$("#a-reload").addEventListener("click", load);
$("#a-notify").addEventListener("click", async () => {
  if (!("Notification" in window)) { $("#a-notify").textContent = "Desktop notifications: not supported"; return; }
  if (prefs.notify && Notification.permission === "granted") { prefs.notify = false; }
  else { prefs.notify = (await Notification.requestPermission()) === "granted"; }
  Prefs.set("alerts", { notify: prefs.notify });
  notifyLabel();
});
notifyLabel();
window.addEventListener("hashchange", focusHash);
setInterval(() => { if (!document.hidden) load(); }, 30000);
load();
