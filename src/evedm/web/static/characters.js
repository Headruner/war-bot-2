/* EVE Distance Meter - characters page: list, enable/disable, tags, remove, import and export. */
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
const STATUS_TEXT = { OK: "OK", LIMITED: "Limited", REAUTH: "Re-auth required", DISABLED: "Disabled" };

async function load() {
  const rows = await (await api("/api/characters")).json();
  const body = $("#char-rows");
  body.innerHTML = "";
  $("#empty").hidden = rows.length > 0;
  for (const c of rows) {
    const tr = el("tr");
    const sel = el("input"); sel.type = "checkbox"; sel.className = "pick"; sel.value = c.character_id;
    sel.disabled = c.demo;
    const tdSel = el("td"); tdSel.append(sel);

    const tdName = el("td");
    const nameLine = el("div", null, c.name);
    if (c.main) nameLine.append(el("span", "badge main", "Main"));
    tdName.append(nameLine);
    tdName.append(el("div", "small", c.demo ? "demo character" : `token ${c.refresh_tail}`));

    const tdStatus = el("td");
    tdStatus.append(el("span", `status-pill status-${c.status}`, STATUS_TEXT[c.status] || c.status));
    if (c.missing_scopes.length) tdStatus.append(el("div", "small", "missing: " + c.missing_scopes.map((s) => s.split(".")[1]).join(", ")));
    if (c.status === "REAUTH") {
      const a = el("a", "small", "Re-authorize"); a.href = "/sso/add";
      const wrap = el("div"); wrap.append(a); tdStatus.append(wrap);
    }

    const tdEnabled = el("td");
    const toggle = el("input"); toggle.type = "checkbox"; toggle.checked = c.enabled;
    toggle.addEventListener("change", async () => {
      await api(`/api/characters/${c.character_id}`, { method: "PATCH", body: JSON.stringify({ enabled: toggle.checked }) });
      const del = $("#delete-account");
if (del) del.addEventListener("click", async () => {
  if (!confirm("Delete your account? All linked characters are removed and their tokens revoked. This cannot be undone.")) return;
  const resp = await api("/api/me", { method: "DELETE", body: JSON.stringify({ confirm: true }) });
  if (resp.ok) location.href = "/login";
});

load();
    });
    tdEnabled.append(toggle);

    const tdTags = el("td");
    const tags = el("input"); tags.type = "text"; tags.value = c.tags.join(", "); tags.placeholder = "e.g. cyno, logi";
    tags.addEventListener("change", () => api(`/api/characters/${c.character_id}`, { method: "PATCH", body: JSON.stringify({ tags: tags.value.split(",") }) }));
    tdTags.append(tags);

    const tdScopes = el("td", "small hide-sm", c.scopes.map((s) => s.split(".")[1]).join(", "));

    const tdActions = el("td");
    const rm = el("button", "btn danger", "Remove"); rm.type = "button";
    if (c.main) { rm.disabled = true; rm.title = "The main signs you in. Delete the account to remove it."; }
    rm.addEventListener("click", async () => {
      if (!confirm(`Remove ${c.name}? Its tokens are deleted and revoked.`)) return;
      await api(`/api/characters/${c.character_id}`, { method: "DELETE" });
      const del = $("#delete-account");
if (del) del.addEventListener("click", async () => {
  if (!confirm("Delete your account? All linked characters are removed and their tokens revoked. This cannot be undone.")) return;
  const resp = await api("/api/me", { method: "DELETE", body: JSON.stringify({ confirm: true }) });
  if (resp.ok) location.href = "/login";
});

load();
    });
    tdActions.append(rm);
    tr.append(tdSel, tdName, tdStatus, tdEnabled, tdTags, tdScopes, tdActions);
    body.append(tr);
  }
}

$("#import-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const resp = await api("/api/characters/import", { method: "POST", body: fd });
  const data = await resp.json();
  const out = $("#import-report");
  out.innerHTML = "";
  out.hidden = false;
  if (data.rejected) {
    out.className = "notice err";
    out.append(el("div", null, `File rejected: ${data.rejected}`));
    (data.errors || []).forEach((e) => out.append(el("div", "small", e)));
  } else {
    out.className = "notice ok";
    const s = data.summary;
    out.append(el("div", null, `Import finished: ${s.Added} added, ${s.Updated} updated, ${s.Limited} limited, ${s.Rejected} rejected.`));
    const table = el("table", "grid report");
    data.entries.forEach((e) => {
      const tr = el("tr");
      tr.append(el("td", null, `#${e.index + 1}`), el("td", null, e.name || String(e.character_id || "—")),
        el("td", `r-${e.result}`, e.result), el("td", "small", e.reason || (e.missing_scopes || []).join(", ")));
      table.append(tr);
    });
    out.append(table);
  }
  ev.target.value = "";
  const del = $("#delete-account");
if (del) del.addEventListener("click", async () => {
  if (!confirm("Delete your account? All linked characters are removed and their tokens revoked. This cannot be undone.")) return;
  const resp = await api("/api/me", { method: "DELETE", body: JSON.stringify({ confirm: true }) });
  if (resp.ok) location.href = "/login";
});

load();
});

$("#export-btn").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll(".pick:checked")].map((x) => Number(x.value));
  const what = ids.length ? `${ids.length} selected characters` : "all characters";
  if (!confirm(`Export ${what}? The file contains refresh tokens: anyone holding it can read those characters' locations.`)) return;
  const resp = await api("/api/characters/export", { method: "POST", body: JSON.stringify({ confirm: true, character_ids: ids }) });
  if (!resp.ok) { alert((await resp.json()).detail || "Export failed"); return; }
  const blob = await resp.blob();
  const name = (resp.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
  const a = el("a"); a.href = URL.createObjectURL(blob); a.download = name ? name[1] : "evedm-characters.json";
  document.body.append(a); a.click(); a.remove();
});

const del = $("#delete-account");
if (del) del.addEventListener("click", async () => {
  if (!confirm("Delete your account? All linked characters are removed and their tokens revoked. This cannot be undone.")) return;
  const resp = await api("/api/me", { method: "DELETE", body: JSON.stringify({ confirm: true }) });
  if (resp.ok) location.href = "/login";
});

load();
