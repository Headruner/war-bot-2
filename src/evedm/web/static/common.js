/* EVE Distance Meter - helpers shared by every page (one cached file instead of a copy in each script).
   apiRaw(url, opts)  -> the Response (X-EVEDM header, same-origin cookies, JSON content type for a JSON body);
                         a 401 sends the browser to the sign-in page. Used where the caller reads status or blob.
   api(url, opts)     -> the parsed JSON; a non-2xx answer throws Error(detail from the server).
   opts are those of fetch(), e.g. { method, body, signal, priority: "low" } (priority is for background requests). */
"use strict";

async function apiRaw(url, opts = {}) {
  const jsonBody = opts.body && !(opts.body instanceof FormData);
  const resp = await fetch(url, { credentials: "same-origin", ...opts,
    headers: { "X-EVEDM": "1", ...(jsonBody ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) } });
  if (resp.status === 401) { location.href = "/login"; return new Promise(() => {}); }  // signed out: leave quietly
  return resp;
}

async function api(url, opts = {}) {
  const resp = await apiRaw(url, opts);
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return resp.json();
}

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
