/* EVE Distance Meter - Roster page: import, resolve, manage and export the static character pool (UC-12).
   Filters are remembered in this browser (prefs cookie, ROS-21). */
"use strict";

async function api(url, opts = {}) {
  const resp = await fetch(url, { credentials: "same-origin", ...opts,
    headers: { "X-EVEDM": "1", ...(opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
      ...(opts.headers || {}) } });
  if (resp.status === 401) { location.href = "/login"; return new Promise(() => {}); }  // signed out: leave quietly
  return resp;
}
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const key = (k) => encodeURIComponent(k);

const filters = Prefs.get("roster", { state: "", tag: "", enabledOnly: false });
$("#filter-state").value = filters.state;
$("#filter-tag").value = filters.tag;
$("#filter-enabled").checked = Boolean(filters.enabledOnly);

async function load() {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.tag) params.set("tag", filters.tag);
  const resp = await api(`/api/roster?${params}`);
  const data = await resp.json();
  const rows = filters.enabledOnly ? data.rows.filter((r) => r.enabled) : data.rows;
  const body = $("#roster-rows");
  body.innerHTML = "";
  $("#roster-empty").hidden = rows.length > 0;
  const s = data.stats;
  $("#roster-stats").textContent = s.entries
    ? `${s.entries} entries · ${s.enabled} ranked · ${s.unresolved} unresolved` +
      (s.uploaded_at ? ` · last upload ${new Date(s.uploaded_at * 1000).toLocaleString()}` : "")
    : "empty";
  rows.forEach((r) => body.append(renderRow(r)));
}

function renderRow(r) {
  const tr = el("tr");

  const name = el("td");
  const line = el("div", null, r.name);
  if (r.shadowed) line.append(el("span", "badge shadowed", "Also linked"));
  name.append(line);

  const system = el("td");
  const input = el("input");
  input.type = "text";
  input.value = r.system ? r.system.name : r.system_name;
  input.size = 16;
  input.addEventListener("change", async () => {
    const resp = await api(`/api/roster/${key(r.key)}`, { method: "PATCH", body: JSON.stringify({ system_name: input.value }) });
    if (!resp.ok) { alert((await resp.json()).detail || "Could not change the system"); }
    load();
  });
  system.append(input);
  if (r.system) {
    system.append(el("div", "small", `${r.system.region} · security ${r.system.security.toFixed(1)}`));
  } else {
    const hint = el("div", "small");
    hint.append(el("span", "badge unresolved", r.reason || "unresolved"));
    (r.suggestions || []).forEach((candidate) => {
      const wrap = el("span", "suggest-fix");
      const b = el("button", null, candidate);
      b.type = "button";
      b.addEventListener("click", async () => {
        await api(`/api/roster/${key(r.key)}`, { method: "PATCH", body: JSON.stringify({ system_name: candidate }) });
        load();
      });
      wrap.append(b);
      hint.append(wrap);
    });
    system.append(hint);
  }

  const ranked = el("td");
  const toggle = el("input");
  toggle.type = "checkbox";
  toggle.checked = r.enabled;
  toggle.addEventListener("change", async () => {
    await api(`/api/roster/${key(r.key)}`, { method: "PATCH", body: JSON.stringify({ enabled: toggle.checked }) });
    load();
  });
  ranked.append(toggle);

  const tags = el("td");
  const tagInput = el("input");
  tagInput.type = "text";
  tagInput.value = r.tags.join(", ");
  tagInput.size = 12;
  tagInput.placeholder = "e.g. hauler";
  tagInput.addEventListener("change", async () => {
    await api(`/api/roster/${key(r.key)}`, { method: "PATCH", body: JSON.stringify({ tags: tagInput.value.split(",") }) });
    load();
  });
  tags.append(tagInput);

  const note = el("td", "small hide-sm", r.note || "");

  const actions = el("td");
  const rm = el("button", "btn danger", "Remove");
  rm.type = "button";
  rm.addEventListener("click", async () => {
    if (!confirm(`Remove ${r.name} from the roster?`)) return;
    await api(`/api/roster/${key(r.key)}`, { method: "DELETE" });
    load();
  });
  actions.append(rm);

  tr.append(name, system, ranked, tags, note, actions);
  return tr;
}

/* ---------------------------------------------------------------- import (ROS-01..ROS-08) */
$("#roster-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const resp = await api("/api/roster/import", { method: "POST", body: fd });
  const data = await resp.json();
  const out = $("#roster-report");
  out.innerHTML = "";
  out.hidden = false;
  if (data.rejected) {
    out.className = "notice err";
    out.append(el("div", null, `File rejected: ${data.rejected}`));
    (data.errors || []).slice(0, 20).forEach((e) => out.append(el("div", "small", e)));
  } else {
    const s = data.summary;
    out.className = s.Rejected || s.Unresolved ? "notice" : "notice ok";
    out.append(el("div", null, `Import finished: ${s.Added} added, ${s.Updated} updated, ` +
      `${s.Unresolved} unresolved, ${s.Rejected} rejected — ${data.total} entries in the roster.`));
    const table = el("table", "grid report");
    data.entries.filter((e) => e.result === "Rejected" || e.reason).slice(0, 50).forEach((e) => {
      const tr = el("tr");
      tr.append(el("td", null, `#${e.index + 1}`), el("td", null, e.name), el("td", null, e.system),
        el("td", `r-${e.result}`, e.result),
        el("td", "small", e.reason + (e.suggestions && e.suggestions.length ? ` — did you mean ${e.suggestions.join(", ")}?` : "")));
      table.append(tr);
    });
    if (table.children.length) out.append(el("div", "small", "Entries that need attention:"), table);
  }
  ev.target.value = "";
  load();
});

$("#roster-resolve").addEventListener("click", async () => {
  const resp = await api("/api/roster/resolve", { method: "POST", body: JSON.stringify({}) });
  const data = await resp.json();
  const out = $("#roster-report");
  out.className = "notice ok";
  out.hidden = false;
  out.textContent = `${data.resolved} entries resolved; ${data.stats.unresolved} still unresolved.`;
  load();
});

$("#roster-clear").addEventListener("click", async () => {
  if (!confirm("Remove every entry from the roster? The uploaded file is not kept, so this cannot be undone.")) return;
  await api("/api/roster?confirm=1", { method: "DELETE" });
  load();
});

$("#roster-export").addEventListener("click", async () => {
  const resp = await api("/api/roster/export");
  if (!resp.ok) { alert((await resp.json()).detail || "Export failed"); return; }
  const blob = await resp.blob();
  const name = (resp.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
  const a = el("a");
  a.href = URL.createObjectURL(blob);
  a.download = name ? name[1] : "evedm-roster.json";
  document.body.append(a);
  a.click();
  a.remove();
});

["state", "tag", "enabledOnly"].forEach((name) => {
  const input = $(`#filter-${name === "enabledOnly" ? "enabled" : name}`);
  input.addEventListener("change", () => {
    filters[name] = name === "enabledOnly" ? input.checked : input.value.trim();
    Prefs.set("roster", { [name]: filters[name] });
    load();
  });
});

load();
