/**
 * Generative widget engine.
 *
 * The backend emits UI as data, never as markup:
 *
 *   { "action": "RENDER_WIDGET", "widget_id": "widget_nvda_stock",
 *     "layout": { "col_span": 6 },
 *     "components": [ { "type": "DataChart", "chartType": "line",
 *                       "data": [122, 124, 123, 128.5],
 *                       "labels": ["9:30", "11:00", "1:00", "4:00"] } ] }
 *
 * Every component in the registry renders DATA ONLY. Widgets have no title
 * bars, no headers and no section labels by design — a chart, a metric or a
 * live feed has to explain itself. The only chrome is a hover-only dismiss dot.
 */

const CYAN = "#00f3ff";
const SERIES_COLORS = ["#00f3ff", "#8a6eff", "#ffb020", "#3ddc97", "#ff4d5e", "#4aa3ff"];

let gridEl = null;
const widgets = new Map();      // widget_id -> { el, ttl }
const charts = new Set();       // canvases needing redraw on resize

// ---------- helpers ----------

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function num(v) {
  const n = typeof v === "number" ? v : parseFloat(String(v).replace(/[^0-9eE.+-]/g, ""));
  return Number.isFinite(n) ? n : 0;
}

function fmt(v) {
  if (typeof v !== "number") return String(v ?? "");
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (abs >= 1e4) return (v / 1e3).toFixed(1) + "K";
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(abs < 10 ? 2 : 1);
}

function seriesOf(spec) {
  if (Array.isArray(spec.series) && spec.series.length) {
    return spec.series.map((s, i) => ({
      data: (s.data || []).map(num),
      color: s.color || SERIES_COLORS[i % SERIES_COLORS.length],
      label: s.label || s.name || "",
    }));
  }
  const data = (spec.data || spec.values || []).map(num);
  return [{ data, color: spec.color || CYAN, label: spec.label || "" }];
}

// ---------- chart painting ----------

function paintChart(canvas) {
  const spec = canvas._spec;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const W = rect.width;
  const H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const series = seriesOf(spec);
  const labels = spec.labels || [];
  const bare = spec.chartType === "sparkline";
  const padL = bare ? 1 : 8;
  const padR = bare ? 1 : 30;
  const padT = bare ? 2 : 10;
  const padB = bare ? 2 : (labels.length ? 18 : 6);
  const plotW = Math.max(1, W - padL - padR);
  const plotH = Math.max(1, H - padT - padB);

  let min = Infinity;
  let max = -Infinity;
  series.forEach((s) => s.data.forEach((v) => { if (v < min) min = v; if (v > max) max = v; }));
  if (!Number.isFinite(min)) { min = 0; max = 1; }
  if (spec.chartType === "bar" && min > 0) min = 0;
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08;
  max += span * 0.08;

  const x = (i, n) => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (v) => padT + plotH - ((v - min) / (max - min)) * plotH;

  // baseline grid
  if (!bare) {
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const gy = padT + (plotH / 3) * i;
      ctx.beginPath();
      ctx.moveTo(padL, gy);
      ctx.lineTo(padL + plotW, gy);
      ctx.stroke();
    }
    ctx.fillStyle = "rgba(233,247,255,0.34)";
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.textAlign = "left";
    ctx.fillText(fmt(max - span * 0.08), padL + plotW + 6, padT + 8);
    ctx.fillText(fmt(min + span * 0.08), padL + plotW + 6, padT + plotH);
  }

  series.forEach((s) => {
    const n = s.data.length;
    if (!n) return;

    if (spec.chartType === "bar") {
      const gap = Math.min(10, plotW / (n * 4));
      const bw = Math.max(2, plotW / n - gap);
      const zero = y(Math.max(min, 0));
      s.data.forEach((v, i) => {
        const bx = padL + (plotW / n) * i + gap / 2;
        const by = y(v);
        const grad = ctx.createLinearGradient(0, by, 0, zero);
        grad.addColorStop(0, s.color);
        grad.addColorStop(1, s.color + "22");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.roundRect(bx, Math.min(by, zero), bw, Math.max(1.5, Math.abs(zero - by)), 3);
        ctx.fill();
      });
      return;
    }

    // line / area / sparkline: smoothed polyline through midpoints
    const pts = s.data.map((v, i) => [x(i, n), y(v)]);
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) {
      const [px, py] = pts[i - 1];
      const [cx2, cy2] = pts[i];
      ctx.quadraticCurveTo(px, py, (px + cx2) / 2, (py + cy2) / 2);
    }
    if (pts.length > 1) ctx.lineTo(pts[pts.length - 1][0], pts[pts.length - 1][1]);

    if (spec.chartType !== "line" || spec.fill) {
      const fillPath = new Path2D();
      fillPath.moveTo(pts[0][0], padT + plotH);
      pts.forEach(([px, py]) => fillPath.lineTo(px, py));
      fillPath.lineTo(pts[pts.length - 1][0], padT + plotH);
      fillPath.closePath();
      const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
      grad.addColorStop(0, s.color + "4d");
      grad.addColorStop(1, s.color + "00");
      ctx.fillStyle = grad;
      ctx.fill(fillPath);
    }

    ctx.strokeStyle = s.color;
    ctx.lineWidth = bare ? 1.5 : 2;
    ctx.lineJoin = "round";
    ctx.shadowColor = s.color;
    ctx.shadowBlur = bare ? 4 : 10;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // live head
    const [hx, hy] = pts[pts.length - 1];
    ctx.fillStyle = s.color;
    ctx.beginPath();
    ctx.arc(hx, hy, bare ? 1.8 : 3, 0, Math.PI * 2);
    ctx.fill();
    if (!bare) {
      ctx.strokeStyle = s.color + "66";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(hx, hy, 7, 0, Math.PI * 2);
      ctx.stroke();
    }
  });

  // x labels: thin them out until they fit
  if (!bare && labels.length) {
    ctx.fillStyle = "rgba(233,247,255,0.38)";
    ctx.font = "10px 'JetBrains Mono', monospace";
    const step = Math.max(1, Math.ceil((labels.length * 46) / plotW));
    labels.forEach((lab, i) => {
      if (i % step && i !== labels.length - 1) return;
      const lx = x(i, labels.length);
      ctx.textAlign = i === 0 ? "left" : i === labels.length - 1 ? "right" : "center";
      ctx.fillText(String(lab), Math.min(Math.max(lx, padL), padL + plotW), H - 5);
    });
  }
}

function chartCanvas(spec, cls) {
  const canvas = el("canvas", cls || "w-chart");
  canvas._spec = spec;
  charts.add(canvas);
  // first paint after layout settles, then on every resize
  requestAnimationFrame(() => paintChart(canvas));
  new ResizeObserver(() => paintChart(canvas)).observe(canvas);
  return canvas;
}

// ---------- component registry ----------

const Components = {
  /** Single figure: value, optional delta and a caption that IS the data. */
  MetricCard(spec) {
    const wrap = el("div", "c-metric");
    const row = el("div", "c-metric-row");
    row.appendChild(el("div", "c-metric-value", typeof spec.value === "number" ? fmt(spec.value) : (spec.value ?? "—")));
    if (spec.unit) row.appendChild(el("div", "c-metric-unit", spec.unit));
    if (spec.delta !== undefined && spec.delta !== null) {
      const d = num(spec.delta);
      const dir = spec.trend || (d >= 0 ? "up" : "down");
      const chip = el("div", `c-metric-delta ${dir}`, `${d >= 0 ? "▲" : "▼"} ${typeof spec.delta === "string" ? spec.delta : fmt(Math.abs(d))}`);
      row.appendChild(chip);
    }
    wrap.appendChild(row);
    if (spec.label) wrap.appendChild(el("div", "c-metric-label", spec.label));
    if (Array.isArray(spec.spark) && spec.spark.length) {
      wrap.appendChild(chartCanvas({ chartType: "sparkline", data: spec.spark, color: spec.color || CYAN }, "c-metric-spark"));
    }
    return wrap;
  },

  DataChart(spec) {
    const wrap = el("div", "c-chart");
    wrap.appendChild(chartCanvas(spec));
    const legend = seriesOf(spec).filter((s) => s.label);
    if (legend.length > 1) {
      const bar = el("div", "c-legend");
      legend.forEach((s) => {
        const item = el("div", "c-legend-item");
        const dot = el("span", "c-legend-dot");
        dot.style.background = s.color;
        item.append(dot, el("span", null, s.label));
        bar.appendChild(item);
      });
      wrap.appendChild(bar);
    }
    return wrap;
  },

  ListGroup(spec) {
    const list = el("div", `c-list${spec.dense ? " dense" : ""}`);
    (spec.items || []).forEach((raw) => {
      const item = typeof raw === "string" ? { label: raw } : raw || {};
      const row = el("div", `c-list-row${item.state ? " s-" + item.state : ""}`);
      const dot = el("span", "c-list-dot");
      if (item.color) dot.style.background = item.color;
      row.appendChild(dot);
      const main = el("div", "c-list-main");
      main.appendChild(el("div", "c-list-label", item.label ?? item.text ?? ""));
      if (item.meta) main.appendChild(el("div", "c-list-meta", item.meta));
      row.appendChild(main);
      if (item.value !== undefined && item.value !== null) {
        row.appendChild(el("div", "c-list-value", typeof item.value === "number" ? fmt(item.value) : item.value));
      }
      list.appendChild(row);
    });
    return list;
  },

  /** Live feed surface. Adopts an existing <video>/<img> when handed one. */
  VisionFeed(spec) {
    const wrap = el("div", "c-vision");
    if (spec.element) {
      wrap.appendChild(spec.element);
    } else if (spec.image_base64 || spec.src) {
      const img = el("img");
      img.src = spec.src || "data:image/jpeg;base64," + spec.image_base64;
      wrap.appendChild(img);
    }
    if (spec.mirrored) wrap.classList.add("mirrored");
    return wrap;
  },

  TextBlock(spec) {
    const p = el("div", `c-text${spec.mono ? " mono" : ""}${spec.size ? " " + spec.size : ""}`);
    p.textContent = spec.text || spec.value || "";
    return p;
  },

  KeyValue(spec) {
    const grid = el("div", "c-kv");
    const rows = spec.rows || spec.items || [];
    rows.forEach((r) => {
      grid.appendChild(el("div", "c-kv-k", r.key ?? r.label ?? ""));
      grid.appendChild(el("div", "c-kv-v", typeof r.value === "number" ? fmt(r.value) : (r.value ?? "")));
    });
    return grid;
  },

  ImageTile(spec) {
    const wrap = el("div", "c-image");
    const img = el("img");
    img.src = spec.src || "data:image/jpeg;base64," + (spec.image_base64 || "");
    if (spec.alt) img.alt = spec.alt;
    wrap.appendChild(img);
    return wrap;
  },

  /** Conversation surface — Ultron's words on the left, Vince's on the right. */
  Transcript(spec) {
    const wrap = el("div", "c-transcript");
    (spec.messages || []).forEach((m) => {
      const b = el("div", `c-bubble ${m.role || "ultron"}`);
      b.textContent = m.text || "";
      wrap.appendChild(b);
    });
    requestAnimationFrame(() => { wrap.scrollTop = wrap.scrollHeight; });
    return wrap;
  },

  /** Tool execution stream: dot state carries running/done, no labels. */
  ActivityStream(spec) {
    const wrap = el("div", "c-activity");
    (spec.items || []).forEach((it) => {
      const row = el("div", `c-act-row ${it.state || "running"}`);
      row.appendChild(el("span", "c-act-dot"));
      const body = el("div", "c-act-body");
      body.appendChild(el("div", "c-act-name", it.name || ""));
      if (it.detail) body.appendChild(el("div", "c-act-detail", it.detail));
      row.appendChild(body);
      wrap.appendChild(row);
    });
    requestAnimationFrame(() => { wrap.scrollTop = wrap.scrollHeight; });
    return wrap;
  },
};

// ---------- widget lifecycle ----------

function announce() {
  window.dispatchEvent(new CustomEvent("ultron-widgets-changed", {
    detail: { count: window.UltronWidgets.visibleCount() },
  }));
}

function applyLayout(node, layout) {
  const l = layout || {};
  const cols = Math.max(2, Math.min(12, num(l.col_span || l.colSpan || 6) || 6));
  const rows = Math.max(1, Math.min(4, num(l.row_span || l.rowSpan || 1) || 1));
  node.style.setProperty("--col-span", cols);
  node.style.setProperty("--row-span", rows);
  if (l.priority) node.style.order = -num(l.priority);
}

function buildBody(node, components) {
  const body = node.querySelector(".widget-body");
  body.replaceChildren();
  (components || []).forEach((spec) => {
    const build = Components[spec && spec.type];
    if (!build) {
      // unknown type: show the payload rather than silently dropping it
      body.appendChild(Components.TextBlock({ text: JSON.stringify(spec), mono: true, size: "sm" }));
      return;
    }
    try {
      body.appendChild(build(spec));
    } catch (e) {
      console.warn("Widget component failed:", spec, e);
    }
  });
}

function createShell(id) {
  const node = el("div", "widget widget-enter");
  node.dataset.widgetId = id;
  node.appendChild(el("div", "widget-body"));
  const x = el("button", "widget-x");
  x.title = "Dismiss";
  x.addEventListener("click", () => window.UltronWidgets.remove(id));
  node.appendChild(x);
  node.addEventListener("animationend", () => node.classList.remove("widget-enter"), { once: true });
  return node;
}

window.UltronWidgets = {
  init(grid) {
    gridEl = grid;
    window.addEventListener("resize", () => charts.forEach(paintChart));
  },

  registry: Components,

  /** Render (or re-render in place) a widget from a backend schema. */
  render(schema) {
    if (!gridEl || !schema) return null;
    const id = schema.widget_id || schema.id || "widget_" + Math.random().toString(36).slice(2, 8);
    let entry = widgets.get(id);
    if (!entry) {
      const node = createShell(id);
      entry = { el: node, ttl: null };
      widgets.set(id, entry);
      gridEl.appendChild(node);
    }
    applyLayout(entry.el, schema.layout);
    buildBody(entry.el, schema.components);

    if (entry.ttl) clearTimeout(entry.ttl);
    const ttl = num(schema.ttl_ms || schema.ttlMs);
    if (ttl > 0) entry.ttl = setTimeout(() => this.remove(id), ttl);

    announce();
    return entry.el;
  },

  has(id) { return widgets.has(id); },

  remove(id) {
    const entry = widgets.get(id);
    if (!entry) return;
    if (entry.ttl) clearTimeout(entry.ttl);
    widgets.delete(id);
    entry.el.querySelectorAll("canvas").forEach((c) => charts.delete(c));
    entry.el.classList.add("widget-exit");
    setTimeout(() => entry.el.remove(), 260);
    announce();
  },

  /** Drops generated widgets; system feeds (camera/screen/spatial) survive. */
  clear() {
    [...widgets.keys()].forEach((id) => this.remove(id));
  },

  /** Show a pre-declared system widget (its DOM lives in index.html). */
  showSystem(id, layout) {
    const node = gridEl && gridEl.querySelector(`[data-system-widget="${id}"]`);
    if (!node || !node.hidden) return node;
    applyLayout(node, layout);
    node.hidden = false;
    node.classList.add("widget-enter");
    node.addEventListener("animationend", () => node.classList.remove("widget-enter"), { once: true });
    announce();
    return node;
  },

  hideSystem(id) {
    const node = gridEl && gridEl.querySelector(`[data-system-widget="${id}"]`);
    if (!node || node.hidden) return;
    node.hidden = true;
    announce();
  },

  systemVisible(id) {
    const node = gridEl && gridEl.querySelector(`[data-system-widget="${id}"]`);
    return !!node && !node.hidden;
  },

  visibleCount() {
    if (!gridEl) return 0;
    return [...gridEl.children].filter((c) => !c.hidden && !c.classList.contains("widget-exit")).length;
  },

  /** Generated (schema-rendered) widgets only — system feeds not counted. */
  generatedCount() { return widgets.size; },

  repaintCharts() { charts.forEach(paintChart); },
};
