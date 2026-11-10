/* EVE Distance Meter - Notifications page (UC-13): ESI notifications of every registered character, newest first,
   with the system, its region and the closest live and roster characters. Filters live in the prefs cookie. */
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
const HUB_CLASS = { Thera: "thera", Turnur: "turnur" };  // violet and orange (see app.css)
const GROUP_CLASS = { asset_safety: "state-ending", structure_attack: "state-fighting", tower: "state-fighting" };

const pollS = Number($(".page").dataset.pollS || 60);
const prefs = Prefs.get("notifications", { types: null, hours: 168 });
const state = { types: Array.isArray(prefs.types) ? prefs.types : null, hours: Number(prefs.hours) || 168,
  groups: [], defaults: [], busy: false };
$("#ntf-hours").value = String(state.hours);

// Enable audio during a user gesture so mobile browsers can play later updates.
const sound = $("#ntf-sound");
const audioStatus = $("#ntf-audio-status");
const AudioEngine = window.AudioContext || window.webkitAudioContext;
let audio = null;
let previousIds = null;
sound.checked = false;
sound.disabled = !AudioEngine;
if (!AudioEngine) audioStatus.textContent = "Sound unavailable in this browser.";

sound.addEventListener("change", async () => {
  audioStatus.textContent = "";
  if (!sound.checked) return;
  try {
    audio ??= new AudioEngine();
    await audio.resume();
  } catch {
    sound.checked = false;
    audioStatus.textContent = "Sound could not start. Tap Sound to retry.";
  }
});

function checkNewNotifications(items) {
  const ids = new Set(items.map(n => n.character_id + ":" + n.notification_id));
  const fresh = previousIds && [...ids].some(id => !previousIds.has(id));
  previousIds = ids;
  if (!fresh || !sound.checked || document.hidden || !audio) return;
  if (audio.state !== "running") {
    sound.checked = false;
    audioStatus.textContent = "Audio paused. Tap Sound to resume.";
    return;
  }
  const tone = audio.createOscillator();
  const gain = audio.createGain();
  const now = audio.currentTime;
  tone.frequency.value = 880;
  gain.gain.setValueAtTime(0, now);
  gain.gain.linearRampToValueAtTime(0.12, now + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.001, now + 0.32);
  tone.connect(gain);
  gain.connect(audio.destination);
  tone.onended = () => { tone.disconnect(); gain.disconnect(); };
  tone.start(now);
  tone.stop(now + 0.33);
}

function secChip(security) {
  const chip = el("span", "sec", secText(security));
  chip.style.background = secColor(security);
  return chip;
}
function eveTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const utc = d.toISOString().slice(0, 16).replace("T", " ");
  return `EVE Time: ${utc}`;
}

function listQuery() {
  const params = new URLSearchParams({ hours: String(state.hours) });
  if (state.types) params.set("types", state.types.join(","));
  return params.toString();
}

async function load() {
  if (state.busy) return;
  state.busy = true;
  const query = listQuery();
  try {
    const data = await api(`/api/notifications?${query}`);
    if (query !== listQuery()) return;
    if (query !== state.lastQuery) previousIds = null;  // newly selected filters can reveal older items
    state.lastQuery = query;
    checkNewNotifications(data.items);
    state.version = data.version;
    // redraw only what changed: an unchanged answer leaves the list, the scroll position and open filters alone
    const groupsKey = JSON.stringify([data.groups, data.defaults]);
    const itemsKey = JSON.stringify(data.items);
    state.groups = data.groups;
    state.defaults = data.defaults;
    if (groupsKey !== state.groupsKey) { state.groupsKey = groupsKey; renderFilters(); }
    if (itemsKey !== state.itemsKey) { state.itemsKey = itemsKey; renderList(data); }
    const c = data.characters;
    const errors = c.errors.length ? ` · ${c.errors.length} character(s) could not be read` : "";
    $("#ntf-status").textContent =
      `${data.items.length} shown · reading ${c.reading} of ${c.linked} characters · updated ` +
      `${new Date().toLocaleTimeString()}${errors}`;
  } catch (err) {
    $("#ntf-status").textContent = `Error: ${err.message}`;
  } finally {
    state.busy = false;
    if (query !== listQuery()) load();
  }
}

function renderList(data) {
  const list = $("#ntf-list");
  list.innerHTML = "";
  $("#ntf-empty").hidden = data.items.length > 0;
  data.items.forEach((n) => list.append(card(n)));
}

/* The closer of Thera and Turnur: gates to the nearest system with a wormhole to it, then the wormhole. */
function hubLabel(h) {
  const label = el("span", `badge hub-label ${HUB_CLASS[h.hub] || ""}`,
    `${h.hub} · ${h.jumps} jump${h.jumps === 1 ? "" : "s"}`);
  const via = h.gates === 0 ? `a wormhole to ${h.hub} in this system`
    : `${h.gates} gate${h.gates === 1 ? "" : "s"} to ${h.entry.name}, which has a wormhole to ${h.hub}`;
  label.title = `Closest hub: ${via}`;
  return label;
}

/* How a character would travel: stargates only, or through Thera / Turnur when that is shorter. */
function routeBadge(route) {
  const kind = route && route !== "gates" ? route : null;
  const badge = el("span", `badge route-type ${kind ? HUB_CLASS[kind] || "" : "gates"}`, kind || "gates");
  badge.title = kind ? `Shortest route goes through ${kind} (EVE-Scout wormholes)` : "Shortest route by stargates";
  return badge;
}

function card(n) {
  const li = el("li", `card ntf ${n.group}`);
  const head = el("div", "ntf-head");
  head.append(el("span", `badge ${GROUP_CLASS[n.group] || "state-pending"}`, n.type));
  head.append(el("span", "ntf-title", n.title));
  li.append(head);

  const who = el("div", "small", `Received by ${n.character}` +
    (n.main && n.main !== n.character ? ` (main ${n.main})` : ""));
  li.append(who);

  const where = el("div", "where-line");
  where.append(el("b", null, "System"));
  if (n.system) {
    where.append(secChip(n.system.security), el("span", null, n.system.name),
      el("span", "small", `· ${n.system.region}`));
    const route = window.findRoute(n.system.name, n.system.space);
    if (route) where.append(route);
    if (n.hub) where.append(hubLabel(n.hub));
  } else {
    where.append(el("span", "small", "not named in this notification"));
  }
  li.append(where);
  li.append(el("div", "small when", eveTime(n.timestamp)));

  const box = el("div", "closest");
  if (!n.system) {
    box.append(el("div", "small", "No system, so no distances."));
  } else if (!n.closest.length && !n.closest_static.length) {
    box.append(el("div", "small", "None of your characters can reach it by gates (or no location yet)."));
  } else {
    if (n.closest.length) {
      box.append(el("div", "small", "Your closest characters:"));
      const ul = el("ul");
      n.closest.forEach((c) => {
        const item = el("li");
        item.append(el("b", null, c.name),
          document.createTextNode(` · ${c.jumps} jump${c.jumps === 1 ? "" : "s"} from ${c.system} `),
          routeBadge(c.route));
        item.append(c.online ? el("span", "badge online", "Online") : el("span", "small", "offline"));
        ul.append(item);
      });
      box.append(ul);
    }
    if (n.closest_static.length) {
      const line = el("div", "small static-closest");
      line.append(el("b", null, "Roster: "));
      line.append(document.createTextNode(
        n.closest_static.map((c) => `${c.name} (${c.jumps} jump${c.jumps === 1 ? "" : "s"})`).join(", ")));
      box.append(line);
    }
  }
  li.append(box);
  return li;
}

/* ---------------------------------------------------------------- filters (types from the ESI list) */
function selected() {
  return state.types || state.defaults;
}

function renderFilters() {
  const box = $("#ntf-groups");
  box.innerHTML = "";
  const chosen = new Set(selected());
  state.groups.forEach((g) => {
    const section = el("div", "ntf-group");
    const title = el("label", "ntf-group-title");
    const all = el("input");
    all.type = "checkbox";
    all.checked = g.types.every((t) => chosen.has(t.type));
    all.addEventListener("change", () => {
      const next = new Set(selected());
      g.types.forEach((t) => (all.checked ? next.add(t.type) : next.delete(t.type)));
      setTypes([...next]);
    });
    title.append(all, el("b", null, g.label));
    section.append(title);
    g.types.forEach((t) => {
      const line = el("label", "ntf-type");
      const box2 = el("input");
      box2.type = "checkbox";
      box2.checked = chosen.has(t.type);
      box2.addEventListener("change", () => {
        const next = new Set(selected());
        if (box2.checked) next.add(t.type); else next.delete(t.type);
        setTypes([...next]);
      });
      line.append(box2, el("span", null, t.type));
      if (t.seen) line.append(el("span", "small", `· ${t.seen} seen`));
      section.append(line);
    });
    box.append(section);
  });
}

function setTypes(types) {
  state.types = types;
  Prefs.set("notifications", { types });
  state.groupsKey = null;
  renderFilters();
  load();
}

$("#ntf-filters-btn").addEventListener("click", () => {
  const panel = $("#ntf-filters");
  panel.hidden = !panel.hidden;
  $("#ntf-filters-btn").setAttribute("aria-expanded", String(!panel.hidden));
});
$("#ntf-defaults").addEventListener("click", () => setTypes(state.defaults.slice()));
$("#ntf-all").addEventListener("click", () =>
  setTypes(state.groups.flatMap((g) => g.types.map((t) => t.type))));
$("#ntf-hours").addEventListener("change", (ev) => {
  state.hours = Number(ev.target.value) || 168;
  Prefs.set("notifications", { hours: state.hours });
  load();
});
$("#ntf-refresh").addEventListener("click", () => load());

/* Pushed from the server: this request stays open until a new notification is stored, then the list is
   reloaded at once. The timer below stays as the fallback (and keeps the distances current). */
async function follow() {
  for (;;) {
    if (document.visibilityState !== "visible" || state.version == null) {
      await new Promise((r) => setTimeout(r, 2000));
      continue;
    }
    try {
      const resp = await api(`/api/notifications/wait?since=${state.version}`);
      if (resp.changed) { state.version = resp.version; await load(); }
    } catch (_) {
      await new Promise((r) => setTimeout(r, 10000));  // server restarting or offline: try again shortly
    }
  }
}

load().then(follow);
setInterval(() => { if (document.visibilityState === "visible") load(); }, Math.min(Math.max(15, pollS), 30) * 1000);
