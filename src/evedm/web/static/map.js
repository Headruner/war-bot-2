/* EVE Distance Meter - 2D star map (canvas). Map coordinates are light years, north up. */
"use strict";

const SEC_COLORS = {
  "1.0": "#2c74df", "0.9": "#3a9aeb", "0.8": "#4acff3", "0.7": "#62daa6", "0.6": "#71e452",
  "0.5": "#eeff83", "0.4": "#de6a0c", "0.3": "#ce440f", "0.2": "#bb1116", "0.1": "#731f1f",
};
const NULL_COLOR = "#b1325f";
function secColor(s) {
  if (s <= 0) return NULL_COLOR;
  return SEC_COLORS[Math.min(1, Math.max(0.1, s)).toFixed(1)];
}
function secText(s) { return (Math.round(s * 10) / 10).toFixed(1); }

class StarMap {
  constructor(canvas, tooltip) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.tooltip = tooltip;
    this.view = { cx: 0, cy: 0, scale: 4 };
    this.route = null;          // array of system indexes (origin ... target)
    this.routeSet = new Set();
    this.origin = null;         // {index, name}
    this.target = null;         // index
    this.markers = [];          // [{index, name}]
    this.pointers = new Map();
    this.pending = false;
    this.loaded = false;
    this._bind();
  }

  async load(url) {
    const resp = await fetch(url, { credentials: "same-origin" });
    const d = await resp.json();
    const n = d.systems.length;
    this.n = n;
    this.ids = d.ids;
    this.idIndex = new Map(d.ids.map((id, i) => [id, i]));
    this.names = new Array(n);
    this.x = new Float32Array(n);
    this.y = new Float32Array(n);
    this.sec = new Float32Array(n);
    this.region = new Uint16Array(n);
    const buckets = new Map();
    let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity;
    d.systems.forEach((s, i) => {
      this.names[i] = s[0]; this.x[i] = s[1]; this.y[i] = s[2]; this.sec[i] = s[3]; this.region[i] = s[4];
      const c = secColor(s[3]);
      if (!buckets.has(c)) buckets.set(c, []);
      buckets.get(c).push(i);
      minx = Math.min(minx, s[1]); maxx = Math.max(maxx, s[1]); miny = Math.min(miny, s[2]); maxy = Math.max(maxy, s[2]);
    });
    this.buckets = [...buckets.entries()];
    this.edges = Int32Array.from(d.edges);
    this.regions = d.regions;
    this.bounds = [minx, miny, maxx, maxy];
    this._buildGrid();
    this.loaded = true;
    this.resize();
    this.fitAll();
  }

  _buildGrid() {
    this.cell = 1.5;
    this.grid = new Map();
    for (let i = 0; i < this.n; i++) {
      const key = Math.floor(this.x[i] / this.cell) + ":" + Math.floor(this.y[i] / this.cell);
      if (!this.grid.has(key)) this.grid.set(key, []);
      this.grid.get(key).push(i);
    }
  }

  indexOf(systemId) { return this.idIndex ? this.idIndex.get(systemId) : undefined; }

  /* ---------------------------------------------------------------- state */
  setScene({ targetId, routeIds, originName, markers }) {
    this.target = targetId != null ? this.indexOf(targetId) ?? null : null;
    this.route = routeIds ? routeIds.map((id) => this.indexOf(id)).filter((i) => i !== undefined) : null;
    this.routeSet = new Set(this.route || []);
    this.origin = this.route && this.route.length ? { index: this.route[0], name: originName } : null;
    this.markers = (markers || []).map((m) => ({ index: this.indexOf(m.systemId), name: m.name }))
      .filter((m) => m.index !== undefined);
    this.fitScene();
  }

  fitScene() {
    const pts = [];
    if (this.route && this.route.length) pts.push(...this.route);
    else if (this.target != null) pts.push(this.target);
    if (!pts.length) return this.fitAll();
    let minx = Infinity, maxx = -Infinity, miny = Infinity, maxy = -Infinity;
    for (const i of pts) {
      minx = Math.min(minx, this.x[i]); maxx = Math.max(maxx, this.x[i]);
      miny = Math.min(miny, this.y[i]); maxy = Math.max(maxy, this.y[i]);
    }
    const span = Math.max(maxx - minx, maxy - miny);
    const pad = Math.max(2.5, span * 0.18);
    const cx = (minx + maxx) / 2, cy = (miny + maxy) / 2;
    const half = Math.max(span / 2 + pad, 9); // keep at least ~18 ly in view so the route has context
    this.fitBounds(cx - half, cy - half, cx + half, cy + half);
  }

  fitAll() { if (this.loaded) this.fitBounds(...this.bounds); }

  fitBounds(minx, miny, maxx, maxy) {
    const w = Math.max(1, this.W - 40), h = Math.max(1, this.H - 110);
    const scale = Math.min(w / Math.max(0.5, maxx - minx), h / Math.max(0.5, maxy - miny));
    this.view = { cx: (minx + maxx) / 2, cy: (miny + maxy) / 2 - 20 / scale, scale: this._clamp(scale) };
    this.draw();
  }

  _clamp(s) { return Math.min(260, Math.max(0.6, s)); }
  zoomBy(f, sx = this.W / 2, sy = this.H / 2) {
    const [wx, wy] = this.toWorld(sx, sy);
    this.view.scale = this._clamp(this.view.scale * f);
    this.view.cx = wx - (sx - this.W / 2) / this.view.scale;
    this.view.cy = wy + (sy - this.H / 2) / this.view.scale;
    this.draw();
  }

  toScreen(i) {
    return [this.W / 2 + (this.x[i] - this.view.cx) * this.view.scale,
      this.H / 2 - (this.y[i] - this.view.cy) * this.view.scale];
  }
  toWorld(sx, sy) {
    return [this.view.cx + (sx - this.W / 2) / this.view.scale, this.view.cy - (sy - this.H / 2) / this.view.scale];
  }

  resize() {
    const r = this.canvas.getBoundingClientRect();
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.W = Math.max(1, r.width); this.H = Math.max(1, r.height);
    this.canvas.width = Math.round(this.W * this.dpr);
    this.canvas.height = Math.round(this.H * this.dpr);
    this.draw();
  }

  draw() {
    if (this.pending || !this.loaded) return;
    this.pending = true;
    requestAnimationFrame(() => { this.pending = false; this._draw(); });
  }

  /* ---------------------------------------------------------------- render */
  _draw() {
    const ctx = this.ctx, s = this.view.scale;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = "#05080d";
    ctx.fillRect(0, 0, this.W, this.H);
    const margin = 30;
    const vis = (sx, sy) => sx > -margin && sx < this.W + margin && sy > -margin && sy < this.H + margin;

    // gate links
    ctx.lineWidth = 1;
    ctx.strokeStyle = s > 12 ? "rgba(120,140,175,0.42)" : "rgba(110,130,165,0.25)";
    ctx.beginPath();
    const e = this.edges;
    for (let k = 0; k < e.length; k += 2) {
      const a = e[k], b = e[k + 1];
      const dx = this.x[a] - this.x[b], dy = this.y[a] - this.y[b];
      if (dx * dx + dy * dy > 625 && !(this.routeSet.has(a) && this.routeSet.has(b))) continue; // >25 ly: special gates
      const [ax, ay] = this.toScreen(a), [bx, by] = this.toScreen(b);
      if (!vis(ax, ay) && !vis(bx, by)) continue;
      ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
    }
    ctx.stroke();

    // systems, one fill per security colour
    const r = Math.min(4.2, Math.max(1.1, 0.9 + s * 0.09));
    for (const [color, list] of this.buckets) {
      ctx.fillStyle = color;
      ctx.beginPath();
      for (const i of list) {
        const [x, y] = this.toScreen(i);
        if (!vis(x, y)) continue;
        ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, Math.PI * 2);
      }
      ctx.fill();
    }

    // label plan: priority labels (target, origin, route) reserve space first, the rest is culled
    this._occ = new Set();
    const plan = [];
    const fontBold = "700 13px Segoe UI, Inter, system-ui, sans-serif";
    const fontRoute = "600 12px Segoe UI, Inter, system-ui, sans-serif";
    const tryPlace = (text, font, x, y, color, spots, force) => {
      ctx.font = font;
      const w = ctx.measureText(text).width, h = 14;
      const box = ([dx, dy, align]) => {
        const left = align === "center" ? x + dx - w / 2 : align === "right" ? x + dx - w : x + dx;
        return [left - 2, y + dy - h / 2 - 1, w + 4, h + 2];
      };
      for (const spot of spots) {
        if (this._reserve(...box(spot))) {
          plan.push({ text, font, x: x + spot[0], y: y + spot[1], color, align: spot[2] });
          return true;
        }
      }
      if (!force) return false;
      const [dx, dy, align] = spots[0];
      this._reserve(...box(spots[0]), true); // forced labels still claim their space
      plan.push({ text, font, x: x + dx, y: y + dy, color, align });
      return true;
    };
    if (this.target != null) {
      const [x, y] = this.toScreen(this.target);
      this._reserve(x - 14, y - 14, 28, 28);
      tryPlace(`Target: ${this.names[this.target]}`, fontBold, x, y, "#ff8b9b",
        [[0, 30, "center"], [0, -30, "center"], [18, 0, "left"], [-18, 0, "right"]], true);
    }
    if (this.origin) {
      const [x, y] = this.toScreen(this.origin.index);
      this._reserve(x - 12, y - 12, 24, 24);
      tryPlace(this.origin.name, fontBold, x, y, "#5ce1e6",
        [[0, -22, "center"], [0, 24, "center"], [16, -10, "left"], [-16, -10, "right"]], true);
    }
    if (this.route && this.route.length > 1) {
      const n = this.route.length;
      for (const i of this.route) { const [x, y] = this.toScreen(i); this._reserve(x - 5, y - 5, 10, 10); }
      this.route.forEach((i, k) => {
        if (k === n - 1 && i === this.target) return; // the target has its own label
        const [x, y] = this.toScreen(i);
        const label = `${this.names[i]} ${secText(this.sec[i])}`;
        tryPlace(label, fontRoute, x, y, "#f2f5f9",
          [[9, -1, "left"], [-9, -1, "right"], [0, -13, "center"], [0, 13, "center"]], k === 0 || k === n - 1);
      });
    }
    for (const m of this.markers) { const [x, y] = this.toScreen(m.index); this._reserve(x - 6, y - 7, 12, 14); }

    // background labels: regions when zoomed out, systems when zoomed in (collision-culled)
    ctx.textBaseline = "middle";
    if (s < 7) {
      ctx.font = "600 12px Segoe UI, Inter, system-ui, sans-serif";
      ctx.fillStyle = "rgba(205,215,235,0.42)";
      ctx.textAlign = "center";
      for (const [name, cx, cy] of this.regions) {
        const sx = this.W / 2 + (cx - this.view.cx) * s, sy = this.H / 2 - (cy - this.view.cy) * s;
        if (!vis(sx, sy)) continue;
        const w = ctx.measureText(name).width;
        if (this._reserve(sx - w / 2, sy - 8, w, 16)) ctx.fillText(name, sx, sy);
      }
    } else if (s >= 20) {
      ctx.font = "11px Segoe UI, Inter, system-ui, sans-serif";
      ctx.fillStyle = "rgba(205,215,235,0.5)";
      ctx.textAlign = "left";
      let count = 0;
      for (let i = 0; i < this.n && count < 400; i++) {
        if (this.routeSet.has(i)) continue;
        const [x, y] = this.toScreen(i);
        if (x < 0 || x > this.W || y < 0 || y > this.H) continue;
        const w = ctx.measureText(this.names[i]).width;
        if (!this._reserve(x + r + 2, y - 7, w + 2, 14)) continue;
        ctx.fillText(this.names[i], x + r + 3, y);
        count++;
      }
    }

    // other characters
    for (const m of this.markers) {
      if (this.origin && m.index === this.origin.index) continue;
      const [x, y] = this.toScreen(m.index);
      ctx.fillStyle = "#e6ebf2";
      ctx.beginPath();
      ctx.moveTo(x, y - 6); ctx.lineTo(x + 5, y); ctx.lineTo(x, y + 6); ctx.lineTo(x - 5, y); ctx.closePath();
      ctx.fill();
      ctx.strokeStyle = "#05080d"; ctx.lineWidth = 1.5; ctx.stroke();
    }

    // selected route
    if (this.route && this.route.length > 1) {
      const pts = this.route.map((i) => this.toScreen(i));
      ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.strokeStyle = "rgba(255,209,102,0.22)"; ctx.lineWidth = 9;
      ctx.beginPath(); pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke();
      ctx.strokeStyle = "#ffd166"; ctx.lineWidth = 2.6;
      ctx.beginPath(); pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke();
      this.route.forEach((i, k) => {
        const [x, y] = pts[k];
        ctx.fillStyle = secColor(this.sec[i]);
        ctx.strokeStyle = "#05080d"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      });
    }

    // origin + target markers
    if (this.origin) {
      const [x, y] = this.toScreen(this.origin.index);
      ctx.strokeStyle = "#5ce1e6"; ctx.lineWidth = 2.5;
      ctx.beginPath(); ctx.arc(x, y, 10, 0, Math.PI * 2); ctx.stroke();
    }
    if (this.target != null) {
      const [x, y] = this.toScreen(this.target);
      ctx.strokeStyle = "#ff5d73"; ctx.lineWidth = 2.5;
      ctx.beginPath(); ctx.arc(x, y, 12, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(x - 19, y); ctx.lineTo(x - 7, y); ctx.moveTo(x + 7, y); ctx.lineTo(x + 19, y);
      ctx.moveTo(x, y - 19); ctx.lineTo(x, y - 7); ctx.moveTo(x, y + 7); ctx.lineTo(x, y + 19);
      ctx.stroke();
    }

    // priority labels on top
    for (const l of plan) {
      ctx.font = l.font;
      ctx.textAlign = l.align;
      this._halo(l.text, l.x, l.y, l.color);
    }
    ctx.textAlign = "left";
  }

  _reserve(x, y, w, h, force = false) {
    const c = 8, cells = [];
    for (let gx = Math.floor(x / c); gx <= Math.floor((x + w) / c); gx++) {
      for (let gy = Math.floor(y / c); gy <= Math.floor((y + h) / c); gy++) {
        const key = gx * 100003 + gy;
        if (this._occ.has(key) && !force) return false;
        cells.push(key);
      }
    }
    for (const k of cells) this._occ.add(k);
    return true;
  }

  _halo(text, x, y, color) {
    const ctx = this.ctx;
    ctx.lineWidth = 4; ctx.strokeStyle = "rgba(5,8,13,0.92)";
    ctx.strokeText(text, x, y);
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }

  /* ---------------------------------------------------------------- interaction */
  nearest(sx, sy, maxPx = 10) {
    const [wx, wy] = this.toWorld(sx, sy);
    const cx = Math.floor(wx / this.cell), cy = Math.floor(wy / this.cell);
    let best = null, bestD = maxPx * maxPx;
    for (let gx = cx - 1; gx <= cx + 1; gx++) {
      for (let gy = cy - 1; gy <= cy + 1; gy++) {
        for (const i of this.grid.get(gx + ":" + gy) || []) {
          const [x, y] = this.toScreen(i);
          const d = (x - sx) ** 2 + (y - sy) ** 2;
          if (d < bestD) { bestD = d; best = i; }
        }
      }
    }
    return best;
  }

  _bind() {
    const c = this.canvas;
    new ResizeObserver(() => this.resize()).observe(c);
    c.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const rect = c.getBoundingClientRect();
      this.zoomBy(Math.exp(-ev.deltaY * 0.0015), ev.clientX - rect.left, ev.clientY - rect.top);
    }, { passive: false });
    c.addEventListener("dblclick", (ev) => {
      const rect = c.getBoundingClientRect();
      this.zoomBy(2, ev.clientX - rect.left, ev.clientY - rect.top);
    });
    c.addEventListener("pointerdown", (ev) => {
      c.setPointerCapture(ev.pointerId);
      this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      c.classList.add("dragging");
      this.tooltip.hidden = true;
    });
    const end = (ev) => {
      this.pointers.delete(ev.pointerId);
      this.pinch = null;
      if (!this.pointers.size) c.classList.remove("dragging");
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", end);
    c.addEventListener("pointerleave", () => { if (!this.pointers.size) this.tooltip.hidden = true; });
    c.addEventListener("pointermove", (ev) => {
      const rect = c.getBoundingClientRect();
      if (!this.pointers.has(ev.pointerId)) return this._hover(ev.clientX - rect.left, ev.clientY - rect.top);
      const prev = this.pointers.get(ev.pointerId);
      this.pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (this.pointers.size === 2) {
        const [p1, p2] = [...this.pointers.values()];
        const dist = Math.hypot(p1.x - p2.x, p1.y - p2.y);
        const mid = [(p1.x + p2.x) / 2 - rect.left, (p1.y + p2.y) / 2 - rect.top];
        if (this.pinch) this.zoomBy(dist / this.pinch, mid[0], mid[1]);
        this.pinch = dist;
        return;
      }
      this.view.cx -= (ev.clientX - prev.x) / this.view.scale;
      this.view.cy += (ev.clientY - prev.y) / this.view.scale;
      this.draw();
    });
  }

  _hover(sx, sy) {
    const i = this.nearest(sx, sy);
    if (i == null) { this.tooltip.hidden = true; return; }
    const here = this.markers.filter((m) => m.index === i).map((m) => m.name);
    const region = this.regions[this.region[i]] ? this.regions[this.region[i]][0] : "";
    this.tooltip.innerHTML = "";
    const title = document.createElement("div");
    title.innerHTML = `<b></b> <span class="sec"></span> <span class="muted"></span>`;
    title.querySelector("b").textContent = this.names[i];
    const chip = title.querySelector(".sec");
    chip.textContent = secText(this.sec[i]);
    chip.style.background = secColor(this.sec[i]);
    title.querySelector(".muted").textContent = region;
    this.tooltip.appendChild(title);
    if (here.length) {
      const who = document.createElement("div");
      who.className = "muted";
      who.textContent = here.join(", ");
      this.tooltip.appendChild(who);
    }
    const [x, y] = this.toScreen(i);
    this.tooltip.style.left = `${x}px`;
    this.tooltip.style.top = `${y}px`;
    this.tooltip.hidden = false;
  }
}

window.StarMap = StarMap;
window.secColor = secColor;
window.secText = secText;
