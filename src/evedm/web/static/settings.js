/* EVE Distance Meter - Settings page (administrator only): edit settings saved on top of the configuration files. */
"use strict";

const send = (url, method, body) => api(url, { method, body: JSON.stringify(body) });

let data = null;               // last server state
let refs = [];                 // per field: { f, box, input, resetBtn, mark }
const resets = new Set();      // keys queued for reset

/* ---------------------------------------------------------------- pending changes */
function listOf(text) { return text.split(",").map((s) => s.trim()).filter(Boolean); }

function editedValue(r) {
  const { f, input } = r;
  switch (f.type) {
    case "bool": return input.checked === !!f.value ? undefined : input.checked;
    case "int": case "float":
      return input.value.trim() === "" || Number(input.value) === f.value ? undefined : Number(input.value);
    case "list": return input.value.trim() === (f.value || []).join(", ") ? undefined : listOf(input.value);
    case "secret": return input.value === "" ? undefined : input.value;
    default: return input.value === String(f.value ?? "") ? undefined : input.value;
  }
}

function pending() {
  const set = {}, reset = [];
  for (const r of refs) {
    if (r.f.locked || r.f.type === "readonly") continue;
    if (resets.has(r.f.key)) { reset.push(r.f.key); continue; }
    const v = editedValue(r);
    if (v !== undefined) set[r.f.key] = v;
  }
  if (rulesF) {
    if (resets.has(RULES_KEY)) reset.push(RULES_KEY);
    else if (rulesDirty()) set[RULES_KEY] = rules.map((e) => canon(e.rule));
  }
  if (channelsF) {
    if (resets.has(CHANNELS_KEY)) reset.push(CHANNELS_KEY);
    else if (channelsDirty()) set[CHANNELS_KEY] = canonChannels();
  }
  return { set, reset };
}

function refresh() {
  const { set, reset } = pending();
  const n = Object.keys(set).length + reset.length;
  const save = $("#s-save");
  save.disabled = n === 0;
  save.textContent = n ? `Save changes (${n})` : "Save changes";
  const rulesQueued = resets.has(RULES_KEY), rulesReset = $("#s-rules-reset");
  $("#s-rules .s-rlist")?.classList.toggle("queued", rulesQueued);
  if (rulesReset) rulesReset.textContent = rulesQueued ? "Undo reset" : "Reset rules";
  const chQueued = resets.has(CHANNELS_KEY), chReset = $("#s-channels-reset");
  $("#s-channels .s-chlist")?.classList.toggle("queued", chQueued);
  if (chReset) chReset.textContent = chQueued ? "Undo reset" : "Reset channels";
  for (const r of refs) {
    const queued = resets.has(r.f.key);
    r.box.classList.toggle("dirty", queued || r.f.key in set);
    r.box.classList.toggle("queued", queued);
    if (r.resetBtn) r.resetBtn.textContent = queued ? "Undo reset" : "Reset";
  }
}

function applyFilter() {
  const q = $("#s-filter").value.trim().toLowerCase(), only = $("#s-changed").checked;
  for (const r of refs) {
    const text = `${r.f.label} ${r.f.key} ${r.f.help || ""}`.toLowerCase();
    r.box.hidden = !text.includes(q) || (only && !r.f.overridden);
  }
  for (const g of document.querySelectorAll("#s-groups details")) {
    g.hidden = !g.querySelector(".s-field:not([hidden])");
  }
}

/* ---------------------------------------------------------------- rendering */
function control(f, id) {
  const input = el("input");
  input.id = id;
  if (f.type === "bool") { input.type = "checkbox"; input.checked = !!f.value; }
  else if (f.type === "int" || f.type === "float") {
    input.type = "number"; input.step = f.type === "int" ? "1" : "any"; input.value = f.value ?? "";
  } else if (f.type === "secret") {
    input.type = "password"; input.autocomplete = "new-password";
    input.placeholder = f.is_set ? "set — type to replace" : "not set";
  } else {
    input.type = "text";
    input.value = f.type === "list" ? (f.value || []).join(", ") : (f.value ?? "");
  }
  return input;
}

function fieldBox(f, i) {
  const box = el("div", "s-field");
  const head = el("div", "s-head");
  const label = el("label", "s-label", f.label);
  label.htmlFor = `s-f${i}`;
  label.title = f.key;
  head.append(label);
  if (f.restart) {
    const b = el("span", "badge stale", "restart");
    b.title = "takes effect after a restart";
    head.append(b);
  }
  const r = { f, box, input: control(f, `s-f${i}`) };
  const locked = f.locked || f.type === "readonly";
  r.input.disabled = locked;
  if (f.type === "readonly") r.input.value = f.value ?? "";
  box.append(head, r.input);
  if (locked) box.append(el("div", "small", "fixed in the configuration files"));
  if (f.overridden && !locked) {
    const row = el("div", "small s-over");
    row.append(el("span", "s-mark", "changed here"));
    r.resetBtn = el("button", "link s-reset", "Reset");
    r.resetBtn.type = "button";
    r.resetBtn.title = "go back to the value from the configuration files";
    r.resetBtn.addEventListener("click", () => {
      if (!resets.delete(f.key)) resets.add(f.key);
      refresh();
    });
    row.append(r.resetBtn);
    box.append(row);
  }
  if (f.help) box.append(el("div", "small", f.help));
  if (f.env) box.append(el("div", "small s-env", `env: ${f.env}`));
  r.input.addEventListener("input", () => { resets.delete(f.key); refresh(); });
  r.input.addEventListener("change", () => { resets.delete(f.key); refresh(); });
  refs.push(r);
  return box;
}

function renderGroups() {
  const root = $("#s-groups");
  root.replaceChildren();
  refs = [];
  let i = 0;
  for (const g of data.groups) {
    const fields = g.fields.filter((f) => f.type !== "rules" && f.type !== "channels");
    if (!fields.length) continue;
    const det = el("details", "s-group");
    det.open = true;
    const sum = el("summary");
    sum.append(el("strong", null, g.title));
    if (g.source) sum.append(" ", el("span", "small", g.source));
    const grid = el("div", "s-grid");
    for (const f of fields) grid.append(fieldBox(f, i++));
    det.append(sum, grid);
    root.append(det);
  }
}

const DISCORD = [["wars", "Wars"], ["notifications", "Notifications"], ["alerts", "Alerts"]];

function renderDiscord() {
  const card = $("#s-discord");
  card.replaceChildren(el("h2", null, "Discord"));
  const bot = data.discord_bot;
  const line = el("div", "s-drow");
  if (bot) {
    const text = bot.online
      ? `online as ${bot.user} in ${bot.servers} server(s)`
      : bot.last_error || "connecting…";
    line.append(el("strong", null, "Bot"), el("span", "small", text));
    const allowed = bot.allowed_guild;
    for (const g of bot.server_list || []) {
      const mine = allowed && g.id === allowed;
      const tag = mine ? " (the configured server)" : allowed ? " (not the configured server)" : "";
      line.append(el("span", mine ? "small" : "small warn", `${g.name} [${g.id}]${tag}`));
    }
    if (allowed && (bot.server_list || []).length > 1) {
      line.append(el("span", "small warn", "The bot is in more servers than discord.guild_id: remove it from the others (Server Settings > Integrations)."));
    }
    if (bot.invite_url && !bot.servers) {
      const a = el("a", null, "Invite the bot to a server");
      a.href = bot.invite_url;
      a.target = "_blank";
      a.rel = "noopener";
      line.append(a);
    }
  } else {
    line.append(el("strong", null, "Bot"), el("span", "small", "offline: no token, or discord.online is off"));
  }
  card.append(line);
  for (const [target, name] of DISCORD) {
    const st = (data.discord || {})[target];
    if (!st) continue;
    const row = el("div", "s-drow");
    const result = el("span", "small");
    const btn = el("button", "btn small-btn", "Send test message");
    btn.type = "button";
    btn.disabled = !st.ready;
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      result.textContent = "sending…";
      try {
        result.textContent = (await send("/api/settings/discord/test", "POST", { target })).detail || "sent";
      } catch (err) {
        result.textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    });
    row.append(el("strong", null, name), el("span", "small", st.detail || (st.enabled ? "on" : "off")), btn, result);
    card.append(row);
  }
}

/* ---------------------------------------------------------------- Discord rules editor */
const RULES_KEY = "evedm:discord.rules";
const RULES_HINT = "Rules for structure alerts. Every empty list filter means “any”. An alert that matches several "
  + "rules is posted to each of their channels. While there is no rule, the single-channel alert settings below are "
  + "used; with at least one rule (even switched off), only the rules are used. Notifications have their own table above.";
// [key, label, kind, only for source, hint]
const RULE_FIELDS = [
  ["name", "Name", "text"], ["enabled", "Enabled", "bool"],
  ["channel_id", "Channel ID", "text", null, "digits only"],
  ["mention_role_id", "Role ID to mention", "text", null, "optional; pinged with every message of this rule"],
  ["space", "Space", "multi"],
  ["regions", "Regions", "list", null, "comma-separated"], ["exclude_regions", "Exclude regions", "list"],
  ["systems", "Systems", "list", null, "comma-separated"], ["exclude_systems", "Exclude systems", "list"],
  ["kinds", "Kinds", "multi", "alerts"], ["min_isk", "Minimum ISK", "num", "alerts", "0 = any"],
  ["core_drops_only", "Core drops only", "bool", "alerts"], ["capitals_only", "Capitals only", "bool", "alerts"],
  ["owners", "Owner IDs", "list", "alerts", "corporation or alliance IDs, comma-separated"],
  ["exclude_owners", "Exclude owner IDs", "list", "alerts"],
  ["groups", "Notification groups", "multi", "notifications"],
  ["types", "Notification types", "list", "notifications", "ESI type names, comma-separated"],
];
const RULE_CHOICES = { space: (o) => o.spaces, kinds: (o) => o.kinds, groups: (o) => Object.keys(o.groups) };
let rulesF = null;             // the "rules" field from the server
let rules = [];                // editor model: { uid, rule, orig, open, det, sum, test, hint }
let loadedRules = "";          // canonical JSON of the rules as loaded
let uidSeq = 0;

const strs = (v) => (v || []).map((s) => String(s).trim()).filter(Boolean);
const canon = (r) => ({
  name: String(r.name ?? "").trim(), source: r.source, enabled: !!r.enabled,
  channel_id: String(r.channel_id ?? "").trim(), mention_role_id: String(r.mention_role_id ?? "").trim(),
  space: strs(r.space), regions: strs(r.regions), exclude_regions: strs(r.exclude_regions),
  systems: strs(r.systems), exclude_systems: strs(r.exclude_systems), kinds: strs(r.kinds),
  min_isk: Number(r.min_isk) || 0, core_drops_only: !!r.core_drops_only, capitals_only: !!r.capitals_only,
  owners: strs(r.owners), exclude_owners: strs(r.exclude_owners), groups: strs(r.groups), types: strs(r.types),
});
const newEntry = (rule, orig, open) => ({ uid: ++uidSeq, rule, orig, open });
const ruleOptions = () => ({ kinds: [], spaces: [], groups: {}, regions: [], ...data.rule_options });
const rulesDirty = () => JSON.stringify(rules.map((e) => canon(e.rule))) !== loadedRules;

function loadRules() {
  rulesF = data.groups.flatMap((g) => g.fields).find((f) => f.type === "rules") || null;
  rules = (rulesF ? rulesF.value : []).map((r) => {
    const rule = canon(r);
    return newEntry(rule, JSON.stringify(rule), false);
  });
  loadedRules = JSON.stringify(rules.map((e) => e.rule));
}

function typesHint(rule) {
  const o = ruleOptions(), types = rule.groups.flatMap((g) => o.groups[g] || []);
  return types.length ? `Types of the chosen groups: ${types.join(", ")}` : "No group chosen: every notification";
}

/* summary line, dirty marker and test button follow the model while typing (no re-render) */
function mark(e) {
  const r = e.rule;
  e.sum.replaceChildren(el("span", "badge", r.source), el("strong", null, r.name.trim() || "unnamed rule"),
    el("span", "small", r.channel_id.trim() || "no channel"));
  if (!r.enabled) e.sum.append(el("span", "badge stale", "off"));
  e.det.classList.toggle("dirty", e.orig !== JSON.stringify(canon(r)));
  e.test.disabled = !r.channel_id.trim();
  if (e.hint) e.hint.textContent = typesHint(r);
}

function touch(e) {
  resets.delete(RULES_KEY);
  mark(e);
  refresh();
}

function restructure() {
  resets.delete(RULES_KEY);
  renderRules();
  refresh();
}

function choiceBox(e, key, id, box) {
  const row = el("div", "s-checks"), checks = [];
  for (const opt of RULE_CHOICES[key](ruleOptions())) {
    const lab = el("label"), cb = el("input");
    cb.type = "checkbox";
    cb.checked = e.rule[key].includes(opt);
    cb.addEventListener("input", () => {
      e.rule[key] = checks.filter((c) => c.checked).map((c) => c.value);
      touch(e);
    });
    cb.value = opt;
    checks.push(cb);
    lab.append(cb, " ", opt);
    row.append(lab);
  }
  box.setAttribute("role", "group");
  box.setAttribute("aria-labelledby", id);
  return row;
}

function ruleField(e, [key, label, kind, , help]) {
  const id = `s-r${e.uid}-${key}`, box = el("div", "s-field"), r = e.rule;
  const lab = el(kind === "multi" ? "span" : "label", "s-label", label);
  box.append(lab);
  if (kind === "multi") {
    lab.id = id;
    box.append(choiceBox(e, key, id, box));
  } else {
    const input = el("input");
    lab.htmlFor = input.id = id;
    if (kind === "bool") { input.type = "checkbox"; input.checked = r[key]; }
    else if (kind === "num") { input.type = "number"; input.min = "0"; input.step = "1"; input.value = r[key]; }
    else { input.type = "text"; input.value = kind === "list" ? r[key].join(", ") : r[key]; }
    if (key.endsWith("_id")) input.inputMode = "numeric";
    if (key.endsWith("regions")) input.setAttribute("list", "s-regions");
    input.addEventListener("input", () => {
      r[key] = kind === "bool" ? input.checked : kind === "num" ? Number(input.value) || 0
        : kind === "list" ? listOf(input.value) : input.value;
      touch(e);
    });
    box.append(input);
  }
  if (help) box.append(el("div", "small", help));
  if (key === "types") box.append(e.hint = el("div", "small"));
  return box;
}

const button = (text, cls, onClick) => {
  const b = el("button", cls, text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
};

function ruleBlock(e, i) {
  const det = el("details", "s-rule"), grid = el("div", "s-grid"), bar = el("div", "s-drow");
  const result = el("span", "small");
  det.open = e.open;
  det.addEventListener("toggle", () => { e.open = det.open; });
  e.det = det;
  e.sum = el("summary");
  e.hint = null;
  for (const def of RULE_FIELDS) if (!def[3] || def[3] === e.rule.source) grid.append(ruleField(e, def));
  e.test = button("Send test message", "btn small-btn", async () => {
    e.test.disabled = true;
    result.textContent = "sending…";
    try {
      result.textContent = (await send("/api/settings/discord/test", "POST",
        { channel_id: e.rule.channel_id.trim() })).detail || "sent";
    } catch (err) {
      result.textContent = err.message;
    } finally {
      e.test.disabled = !e.rule.channel_id.trim();
    }
  });
  const move = (d) => {
    const at = rules.indexOf(e);
    [rules[at], rules[at + d]] = [rules[at + d], rules[at]];
    restructure();
  };
  const up = button("Move up", "btn small-btn", () => move(-1));
  const down = button("Move down", "btn small-btn", () => move(1));
  up.disabled = i === 0;
  down.disabled = i === rules.length - 1;
  bar.append(e.test, button("Duplicate", "btn small-btn", () => {
    rules.splice(rules.indexOf(e) + 1, 0,
      newEntry({ ...canon(e.rule), name: `${e.rule.name.trim()} copy`.trim() }, null, true));
    restructure();
  }), up, down, button("Delete", "btn small-btn danger", () => {
    rules.splice(rules.indexOf(e), 1);
    restructure();
  }), result);
  det.append(e.sum, grid, bar);
  mark(e);
  return det;
}

function addRule(source) {
  const rule = canon({ name: "", source, enabled: true });
  rules.push(newEntry(rule, null, true));
  restructure();
}

/* a preset adds its rules (nothing is saved until Save); a rule whose name is already there is not added twice */
function addPreset(preset) {
  const have = new Set(rules.map((e) => e.rule.name.trim().toLowerCase()));
  const fresh = (preset.rules || []).filter((r) => !have.has(String(r.name).trim().toLowerCase()));
  fresh.forEach((r) => rules.push(newEntry(canon(r), null, false)));
  restructure();
  status("ok", fresh.length
    ? `Added ${fresh.length} rule(s) from the preset: check the channels, then Save.`
    : "Every rule of this preset is already there.");
}

function renderRules() {
  const card = $("#s-rules");
  card.hidden = !rulesF;
  if (!rulesF) return;
  const regions = el("datalist"), list = el("div", "s-rlist"), bar = el("div", "s-drow");
  regions.id = "s-regions";
  for (const name of ruleOptions().regions) regions.append(Object.assign(el("option"), { value: name }));
  rules.forEach((e, i) => list.append(ruleBlock(e, i)));
  bar.append(button("Add alerts rule", "btn small-btn", () => addRule("alerts")));
  if (rulesF.overridden) {
    const reset = button("Reset rules", "btn small-btn", () => {
      if (!resets.delete(RULES_KEY)) resets.add(RULES_KEY);
      refresh();
    });
    reset.id = "s-rules-reset";
    reset.title = "go back to the rules from the configuration files";
    bar.append(reset);
  }
  const presets = el("div", "s-drow");
  for (const p of data.rule_presets || []) {
    const b = button(`Add preset: ${p.title}`, "btn small-btn", () => addPreset(p));
    b.title = p.note || "";
    presets.append(b);
  }
  card.replaceChildren(el("h2", null, "Discord rules"), el("p", "small", RULES_HINT), regions, list, bar,
    ...(presets.childElementCount ? [presets] : []));
}

/* ---------------------------------------------------------------- notification channels (one per type) */
const CHANNELS_KEY = "evedm:notifications.discord.channels";
const CHANNELS_HINT = "One Discord channel per notification type. Enter a channel ID (posted by the bot) or a webhook "
  + "URL (posted without the bot). A type with an empty row is not posted. The role, when given, is pinged with every "
  + "message of that type. The header of the message is the short name shown in the first column.";
let channelsF = null;          // the "channels" field from the server
let chan = {};                 // editor model: { type: { channel_id, mention_role_id } }
let loadedChannels = "";       // canonical JSON as loaded

function canonChannels() {
  const out = {};
  for (const type of Object.keys(chan).sort()) {
    const c = chan[type], id = (c.channel_id || "").trim();
    if (id) out[type] = { channel_id: id, mention_role_id: (c.mention_role_id || "").trim() };
  }
  return out;
}
const channelsDirty = () => JSON.stringify(canonChannels()) !== loadedChannels;

function loadChannels() {
  channelsF = data.groups.flatMap((g) => g.fields).find((f) => f.type === "channels") || null;
  chan = {};
  for (const [type, c] of Object.entries(channelsF ? channelsF.value || {} : {})) {
    chan[type] = { channel_id: String(c.channel_id || ""), mention_role_id: String(c.mention_role_id || "") };
  }
  loadedChannels = JSON.stringify(canonChannels());
}

function channelRow(opt) {
  const c = (chan[opt.type] ||= { channel_id: "", mention_role_id: "" });
  const row = el("div", "s-chrow");
  const name = el("div", "s-chname");
  name.append(el("strong", null, opt.short), el("span", "small", opt.type));
  const id = Object.assign(el("input"), { type: "text", value: c.channel_id, placeholder: "channel ID or webhook URL" });
  const role = Object.assign(el("input"), { type: "text", value: c.mention_role_id, placeholder: "role ID" });
  id.setAttribute("aria-label", `${opt.short}: channel ID or webhook URL`);
  role.setAttribute("aria-label", `${opt.short}: role ID`);
  const result = el("span", "small");
  const test = button("Test", "btn small-btn", async () => {
    test.disabled = true;
    try {
      result.textContent = (await send("/api/settings/discord/test", "POST", { channel_id: c.channel_id.trim() })).detail || "sent";
    } catch (err) {
      result.textContent = err.message;
    }
    test.disabled = !c.channel_id.trim();
  });
  const sync = () => {
    c.channel_id = id.value;
    c.mention_role_id = role.value;
    test.disabled = !c.channel_id.trim();
    refresh();
  };
  id.addEventListener("input", sync);
  role.addEventListener("input", sync);
  test.disabled = !c.channel_id.trim();
  row.append(name, id, role, test, result);
  return row;
}

function renderChannels() {
  const card = $("#s-channels");
  card.hidden = !channelsF;
  if (!channelsF) return;
  const list = el("div", "s-chlist"), bar = el("div", "s-drow");
  for (const g of data.channel_options || []) {
    const det = el("details", "s-group");
    det.open = true;
    det.append(el("summary", null, g.label));
    for (const opt of g.types) det.append(channelRow(opt));
    list.append(det);
  }
  if (channelsF.overridden) {
    const reset = button("Reset channels", "btn small-btn", () => {
      if (!resets.delete(CHANNELS_KEY)) resets.add(CHANNELS_KEY);
      refresh();
    });
    reset.id = "s-channels-reset";
    reset.title = "go back to the channels from the configuration files";
    bar.append(reset);
  }
  for (const p of data.channel_presets || []) {
    bar.append(button(`Fill: ${p.title}`, "btn small-btn", () => {
      for (const [type, id] of Object.entries(p.channels)) {
        const c = (chan[type] ||= { channel_id: "", mention_role_id: "" });
        if (!c.channel_id.trim()) c.channel_id = id;  // a row that has a channel keeps it
      }
      renderChannels();
      refresh();
    }));
  }
  card.replaceChildren(el("h2", null, "Notification channels"), el("p", "small", CHANNELS_HINT), list, bar);
}

function render() {
  resets.clear();
  $("#s-intro").textContent = `Changes are saved to ${data.file} and win over the configuration files and `
    + `environment variables.${data.note ? " " + data.note : ""}`;
  renderDiscord();
  loadChannels();
  renderChannels();
  loadRules();
  renderRules();
  renderGroups();
  applyFilter();
  refresh();
}

/* ---------------------------------------------------------------- actions */
function status(kind, text) {
  const box = $("#s-status");
  box.replaceChildren();
  if (text) box.append(el("div", `notice ${kind}`, text));
}
const shortKey = (k) => k.slice(k.indexOf(":") + 1);

async function save() {
  const { set, reset } = pending();
  $("#s-save").disabled = true;
  try {
    const res = await send("/api/settings", "PUT", { set, reset });
    data = res;
    render();
    let msg = `Saved ${res.changed.length} setting(s).`;
    if (res.restart_needed.length) msg += ` Restart needed for: ${res.restart_needed.map(shortKey).join(", ")}`;
    status("ok", msg);
  } catch (err) {
    status("err", err.message);  // keep the user's edits
    refresh();
  }
}

function restartStep(confirming) {
  $("#s-restart").hidden = confirming;
  $("#s-restart-ok").hidden = $("#s-restart-no").hidden = !confirming;
}

async function restart() {
  restartStep(false);
  $("#s-restart").disabled = true;
  try {
    await send("/api/settings/restart", "POST", {});
  } catch (err) {
    status("err", err.message);
    $("#s-restart").disabled = false;
    return;
  }
  status("ok", "Restarting…");
  const started = Date.now();
  const timer = setInterval(async () => {
    try {
      if ((await fetch("/api/health", { cache: "no-store", priority: "low" })).ok && Date.now() - started > 3000) location.reload();
    } catch (_) { /* still down */ }
    if (Date.now() - started > 60000) {
      clearInterval(timer);
      status("err", "The app did not come back within 60 seconds. Check the container and reload this page.");
      $("#s-restart").disabled = false;
    }
  }, 2000);
}

async function init() {
  try {
    data = await api("/api/settings");
  } catch (err) {
    status("err", err.message);
    return;
  }
  render();
}

/* ---------------------------------------------------------------- Characters tab */
let chars = null;        // last /api/settings/characters answer
const APP_LABEL = { main: "evedm", legacy: "legacy", other: "other app", none: "no token" };
const SHOWN = 6;         // permissions shown before "+N"

function charRow(c) {
  const tr = el("tr");
  const who = el("td");
  who.append(el("div", null, c.name), el("div", "sub", String(c.character_id)));
  const main = el("td");
  main.append(el("div", null, c.owner_name));
  if (c.owner_id) main.append(el("div", "sub", String(c.owner_id)));
  const st = el("td");
  const badge = el("span", `badge ${c.status === "OK" ? "online" : c.status === "LIMITED" ? "limited" : "err"}`, c.status);
  st.append(badge);
  const app = el("td", null, APP_LABEL[c.app] || c.app);
  if (c.app === "legacy" && !(chars.legacy_client.client_id && chars.legacy_client.secret_set)) {
    app.append(el("div", "sub", "waiting for the application"));
  }
  const perms = el("td");
  perms.title = c.scopes.join("\n");
  for (const m of c.missing) {
    const s = el("span", "perm missing", `no ${m}`);
    s.title = "required, not granted";
    perms.append(s);
  }
  for (const p of c.permissions.slice(0, SHOWN)) perms.append(el("span", "perm", p));
  if (c.permissions.length > SHOWN) {
    const more = el("span", "perm more", `+${c.permissions.length - SHOWN}`);
    more.title = c.permissions.slice(SHOWN).join(", ");
    perms.append(more);
  }
  if (!c.permissions.length) perms.append(el("span", "sub", "none"));
  const tags = el("td", "hide-sm", c.tags.join(", "));
  tr.append(who, main, st, app, perms, tags);
  tr.dataset.text = [c.name, c.character_id, c.owner_name, c.status, APP_LABEL[c.app], ...c.tags, ...c.permissions,
    ...c.scopes].join(" ").toLowerCase();
  return tr;
}

function renderChars() {
  const body = $("#c-rows");
  body.replaceChildren(...chars.characters.map(charRow));
  const lc = chars.legacy_client;
  $("#lc-id").value = lc.client_id || "";
  $("#lc-secret").value = "";
  $("#lc-secret").placeholder = lc.secret_set ? "saved - type to replace" : "not set";
  $("#lc-state").textContent = lc.client_id && lc.secret_set ? "Set: imported characters refresh with it."
    : "Not complete: imported characters wait until both values are saved.";
  filterChars();
}

function filterChars() {
  const q = $("#c-filter").value.trim().toLowerCase();
  let shown = 0;
  for (const tr of $("#c-rows").children) {
    tr.hidden = q !== "" && !tr.dataset.text.includes(q);
    if (!tr.hidden) shown += 1;
  }
  const all = chars ? chars.characters : [];
  const distinct = new Set(all.map((c) => c.character_id)).size;
  const legacy = all.filter((c) => c.app === "legacy").length;
  $("#c-summary").textContent = `${shown} of ${all.length} links, ${distinct} characters, ${legacy} on the legacy app`;
}

function charStatus(kind, text) {
  const box = $("#c-status");
  box.replaceChildren();
  if (text) box.append(el("div", `notice ${kind}`, text));
}

async function loadChars() {
  try {
    chars = await api("/api/settings/characters");
    renderChars();
  } catch (err) {
    charStatus("err", err.message);
  }
}

async function saveLegacy() {
  const set = { "evedm:eve.legacy_client_id": $("#lc-id").value.trim() };
  const secret = $("#lc-secret").value.trim();
  if (secret) set["evedm:eve.legacy_client_secret"] = secret;
  $("#lc-save").disabled = true;
  try {
    data = await send("/api/settings", "PUT", { set, reset: [] });
    render();  // the Settings tab shows the same two fields
    charStatus("ok", "Legacy EVE application saved.");
    await loadChars();
  } catch (err) {
    charStatus("err", err.message);
  } finally {
    $("#lc-save").disabled = false;
  }
}

let charsLoaded = false;
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  });
  document.querySelectorAll(".tab-panel").forEach((pn) => { pn.hidden = pn.id !== `tab-${name}`; });
  if (name === "characters" && !charsLoaded) { charsLoaded = true; loadChars(); }
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
}

document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("#c-filter").addEventListener("input", filterChars);
$("#c-reload").addEventListener("click", loadChars);
$("#lc-save").addEventListener("click", saveLegacy);
showTab(location.hash === "#characters" ? "characters" : "settings");

$("#s-filter").addEventListener("input", applyFilter);
$("#s-changed").addEventListener("change", applyFilter);
$("#s-save").addEventListener("click", save);
$("#s-discard").addEventListener("click", () => { render(); status("", ""); });
$("#s-restart").addEventListener("click", () => restartStep(true));
$("#s-restart-no").addEventListener("click", () => restartStep(false));
$("#s-restart-ok").addEventListener("click", restart);
init();
