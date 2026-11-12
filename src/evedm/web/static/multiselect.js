/* EVE Distance Meter - smart-search multi-select: type two letters, pick suggestions from /api/structures/suggest,
   the picks stay as chips. MultiSelect(container, {kind, placeholder, onChange}) -> {value(), set(items)}.
   Suggestion text comes from the server (names are user content): it is only ever put in with textContent. */
"use strict";

function MultiSelect(container, { kind, placeholder = "", onChange = () => {} }) {
  const MAX = 20, DEBOUNCE_MS = 200;
  let chips = [], items = [], active = -1, timer = null, aborter = null;
  const id = `ms-${kind}-${Math.random().toString(36).slice(2, 7)}`;
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const box = mk("div", "ms-box");
  const input = mk("input", "ms-input");
  const list = mk("ul", "ms-list");
  input.type = "text"; input.placeholder = placeholder; input.spellcheck = false; input.autocomplete = "off";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", id);
  input.setAttribute("aria-autocomplete", "list");
  list.id = id; list.setAttribute("role", "listbox"); list.hidden = true;
  box.append(input);
  container.classList.add("ms");
  container.append(box, list);

  function highlight(parent, text, needle) {
    const at = needle ? text.toLowerCase().indexOf(needle.toLowerCase()) : -1;
    if (at < 0) { parent.append(document.createTextNode(text)); return; }
    parent.append(document.createTextNode(text.slice(0, at)), mk("mark", null, text.slice(at, at + needle.length)),
      document.createTextNode(text.slice(at + needle.length)));
  }
  function render() {
    chips.forEach((c) => c.node.remove());
    chips.forEach((c) => box.insertBefore(c.node, input));
    input.disabled = chips.length >= MAX;
  }
  function addChip(item) {
    if (chips.length >= MAX || chips.some((c) => c.kind === item.kind && c.id === item.id)) return;
    const node = mk("span", `ms-chip ms-${item.kind}`);
    const x = mk("button", "ms-x", "×");
    x.type = "button"; x.title = `Remove ${item.name}`; x.setAttribute("aria-label", `Remove ${item.name}`);
    x.addEventListener("click", () => { chips = chips.filter((c) => c.node !== node); render(); onChange(); });
    node.append(mk("span", "ms-kind", item.kind === "system" ? "system" : item.kind), document.createTextNode(` ${item.name} `), x);
    chips.push({ kind: item.kind, id: item.id, name: item.name, node });
  }
  function close() { list.hidden = true; input.setAttribute("aria-expanded", "false"); items = []; active = -1; }
  function pick(item) { addChip(item); input.value = ""; close(); render(); onChange(); input.focus(); }
  function mark(k) {
    active = k;
    [...list.children].forEach((li, n) => {
      li.classList.toggle("active", n === k);
      li.setAttribute("aria-selected", String(n === k));
      if (n === k) { input.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); }
    });
  }
  function show(found, needle) {
    items = found;
    list.innerHTML = "";
    found.forEach((it, n) => {
      const li = mk("li", "ms-opt");
      li.id = `${id}-${n}`; li.setAttribute("role", "option"); li.setAttribute("aria-selected", "false");
      const name = mk("span", "ms-name");
      highlight(name, it.name, needle);
      li.append(name, mk("span", "ms-detail small", it.detail || ""));
      li.addEventListener("mousedown", (ev) => { ev.preventDefault(); pick(it); });
      list.append(li);
    });
    if (!found.length) list.append(mk("li", "ms-empty small", "nothing found"));
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    mark(found.length ? 0 : -1);
  }
  async function suggest() {
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    if (aborter) aborter.abort();
    aborter = new AbortController();
    try {
      const resp = await fetch(`/api/structures/suggest?${new URLSearchParams({ kind, q })}`,
        { credentials: "same-origin", headers: { "X-EVEDM": "1" }, signal: aborter.signal });
      if (resp.status === 401) { location.href = "/login"; return; }
      if (!resp.ok) { close(); return; }
      const d = await resp.json();
      if (input.value.trim() === q) show(d.items.filter((it) => !chips.some((c) => c.kind === it.kind && c.id === it.id)), q);
    } catch (err) {
      if (err.name !== "AbortError") close();  // a failed suggestion is only a closed list
    }
  }
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(suggest, DEBOUNCE_MS); });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      if (!items.length) return;
      ev.preventDefault();
      mark((active + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length);
    } else if (ev.key === "Enter") {
      if (items[active]) { ev.preventDefault(); pick(items[active]); }
    } else if (ev.key === "Escape") {
      close();
    } else if (ev.key === "Backspace" && !input.value && chips.length) {
      chips.pop().node.remove(); render(); onChange();
    }
  });
  input.addEventListener("blur", () => { clearTimeout(timer); if (aborter) aborter.abort(); close(); });
  box.addEventListener("click", () => input.focus());

  return {
    value: () => chips.map(({ kind: k, id: i, name }) => ({ kind: k, id: i, name })),
    set(list_) { chips.forEach((c) => c.node.remove()); chips = []; (list_ || []).forEach(addChip); render(); },
  };
}
