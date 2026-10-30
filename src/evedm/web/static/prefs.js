/* EVE Distance Meter - per-browser settings kept in one cookie (evedm_prefs): small JSON, one year, SameSite=Lax.
   Nothing is stored on the server; another browser or a cleared cookie starts from the defaults. */
"use strict";

const Prefs = (() => {
  const NAME = "evedm_prefs";
  const MAX_AGE = 365 * 86400;
  function readAll() {
    try {
      const raw = document.cookie.split("; ").find((c) => c.startsWith(NAME + "="));
      const value = raw ? JSON.parse(decodeURIComponent(raw.slice(NAME.length + 1))) : {};
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch (_) {
      return {};  // unreadable cookie: defaults
    }
  }
  function writeAll(all) {
    const value = encodeURIComponent(JSON.stringify(all));
    if (value.length > 3500) return;  // stay well inside the 4 KB cookie limit
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${NAME}=${value}; Path=/; Max-Age=${MAX_AGE}; SameSite=Lax${secure}`;
  }
  return {
    /* settings group (e.g. "map") merged over its defaults */
    get(group, defaults = {}) {
      const stored = readAll()[group];
      return { ...defaults, ...(stored && typeof stored === "object" ? stored : {}) };
    },
    set(group, values) {
      const all = readAll();
      all[group] = { ...(all[group] || {}), ...values };
      writeAll(all);
    },
  };
})();
