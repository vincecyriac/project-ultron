/**
 * Ultron — frontend engine.
 *
 * WebSocket bridge to the Python backend: 16 kHz PCM mic capture, 24 kHz voice
 * playback with barge-in flush, sensor toggles, live system telemetry, and the
 * holographic orb's state/energy feed.
 *
 * The UI has no chrome: system state is expressed entirely through the orb's
 * colour and energy (see orb.js), and the SVE 3D workspace slides in beside a
 * docked orb whenever a spatial scene goes live.
 */

let ws = null;
let audioCtx = null;
let micStream = null;
let scriptNode = null;
let isMicMuted = false;
let isCamActive = false;
let isScreenActive = false;

let audioAnalyser = null;
let audioDataArray = null;

// Remote = served through Tailscale (HTTPS, non-localhost). Remote clients
// play Ultron's voice in the browser; local ones rely on the Mac's speakers.
const IS_REMOTE = !["127.0.0.1", "localhost", ""].includes(window.location.hostname);

// ---------- DOM ----------

const teleClockEl = document.getElementById("tele-clock");
const teleCpuEl = document.getElementById("tele-cpu");
const teleRamEl = document.getElementById("tele-ram");

const orbStageEl = document.getElementById("orb-stage");
const captionEl = document.getElementById("caption");

const chatInputEl = document.getElementById("chat-input");
const sendBtnEl = document.getElementById("send-btn");
const commandPillEl = document.querySelector(".command-pill");

const btnMic = document.getElementById("btn-mic");
const btnCam = document.getElementById("btn-cam");
const btnScreen = document.getElementById("btn-screen");

const camVideoEl = document.getElementById("webcam-video");
const camImgEl = document.getElementById("webcam-img");
const camPlaceholder = document.getElementById("cam-placeholder");
const camFeedCard = document.getElementById("cam-feed-card");
const screenImgEl = document.getElementById("screen-img");
const screenPlaceholder = document.getElementById("screen-placeholder");
const screenFeedCard = document.getElementById("screen-feed-card");
const pipStackEl = document.querySelector(".pip-stack");
const agentRailEl = document.getElementById("agent-rail");
const widgetDeckEl = document.getElementById("widget-deck");
const sveStageEl = document.getElementById("sve-stage");
const sveParkingEl = document.getElementById("sve-parking");

// ---------- Orb state machine ----------
// Priority: offline > speaking > thinking > listening > idle.

let lastAudioOutAt = 0;
let speakingLevel = 0;      // envelope from the model's outgoing PCM
let micLevel = 0;           // envelope from the local analyser
let activeTools = 0;
let connectionState = "connecting";   // connecting | online | offline
let backendBusy = false;      // backend reported an executing/connecting status
let backendSpeaking = false;  // hub says a model turn is producing audio

const SPEAK_HOLD_MS = 450;
const MIC_GATE = 0.055;
// Ultron's voice comes out of the Mac's speakers via PyAudio, which the
// browser's echoCancellation cannot remove — it only cancels what the browser
// itself plays. So the mic keeps hearing him for a moment after he stops, and
// that must not be mistaken for the user talking.
const ECHO_GUARD_MS = 1500;

function resolveOrbState() {
  if (connectionState === "offline") return "offline";

  const sinceAudio = performance.now() - lastAudioOutAt;

  // The hub knows when a model turn is producing audio and tells us; trust that
  // over guessing from chunk arrival. A natural pause longer than SPEAK_HOLD_MS
  // used to drop the orb out of "speaking" mid-answer, and with his own voice in
  // the mic it landed on "listening" — a 90 degree hue jump, green to blue,
  // while he was still talking. The timer stays as a fallback for the window
  // before the first status arrives.
  if (backendSpeaking || isVoicePlaying() || sinceAudio < SPEAK_HOLD_MS) return "speaking";

  if (activeTools > 0 || backendBusy) return "thinking";
  const sinceVoiceEnded = performance.now() - playbackCursorMs;
  if (!isMicMuted && micLevel > MIC_GATE
      && sinceAudio > ECHO_GUARD_MS && sinceVoiceEnded > ECHO_GUARD_MS) return "listening";
  return "idle";
}

function pumpOrb() {
  requestAnimationFrame(pumpOrb);
  const orb = window.UltronOrb;
  if (!orb) return;

  // Mic envelope (RMS over the analyser bins), only meaningful while unmuted.
  if (audioAnalyser && audioDataArray && !isMicMuted) {
    audioAnalyser.getByteFrequencyData(audioDataArray);
    let sum = 0;
    for (let i = 0; i < audioDataArray.length; i++) {
      const v = audioDataArray[i] / 255;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / audioDataArray.length);
    micLevel += (rms - micLevel) * 0.25;
  } else {
    micLevel *= 0.85;
  }

  // Drop chunks that have finished playing; the head of the queue is the audio
  // audible right now, so the orb animates in step with what you actually hear.
  const nowMs = performance.now();
  while (audioSchedule.length && audioSchedule[0].untilMs <= nowMs) audioSchedule.shift();
  const heardRms = audioSchedule.length ? audioSchedule[0].rms : 0;
  speakingLevel += (heardRms - speakingLevel) * (heardRms > speakingLevel ? 0.45 : 0.12);

  const state = resolveOrbState();
  orb.setState(state);

  // Feed the live envelope back in: it animates the orb without touching hue.
  if (state === "speaking") orb.setLevel(Math.min(1, speakingLevel * 2.2));
  else if (state === "listening") orb.setLevel(Math.min(1, micLevel * 1.8));
  else if (state === "thinking") orb.setLevel(0.12 + Math.abs(Math.sin(performance.now() / 520)) * 0.20);
  else orb.setLevel(micLevel * 0.6);
}

// The hub streams Ultron's voice as fast as the model generates it — measured
// at ~35s of speech delivered in under 10s. Animating on arrival therefore ran
// the orb dry while he was still audibly talking. Instead every chunk is placed
// on a playback timeline and the orb reads whichever chunk is *being heard now*.
const AUDIO_SR = 24000;
let playbackCursorMs = 0;      // when the queued audio runs out
const audioSchedule = [];      // { untilMs, rms } in playback order

/** RMS + duration of a base64 PCM16 chunk, sampled sparsely. */
function pcmStats(b64) {
  try {
    const raw = window.atob(b64);
    const n = (raw.length / 2) | 0;
    if (!n) return { rms: 0, durationMs: 0 };
    const step = Math.max(1, Math.floor(n / 128));
    let sum = 0;
    let count = 0;
    for (let i = 0; i < n; i += step) {
      let s = raw.charCodeAt(i * 2) | (raw.charCodeAt(i * 2 + 1) << 8);
      if (s > 32767) s -= 65536;
      const v = s / 32768;
      sum += v * v;
      count++;
    }
    return { rms: Math.sqrt(sum / count), durationMs: (n / AUDIO_SR) * 1000 };
  } catch (_) {
    return { rms: 0, durationMs: 0 };
  }
}

function scheduleAudioChunk(b64) {
  const { rms, durationMs } = pcmStats(b64);
  if (!durationMs) return;
  const now = performance.now();
  if (playbackCursorMs < now) playbackCursorMs = now;   // fresh utterance
  playbackCursorMs += durationMs;
  audioSchedule.push({ untilMs: playbackCursorMs, rms });
}

function clearAudioSchedule() {
  audioSchedule.length = 0;
  playbackCursorMs = 0;
}

/** True while there is still queued voice being heard. */
function isVoicePlaying() {
  return performance.now() < playbackCursorMs;
}

// ---------- Telemetry ----------

function tickClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  teleClockEl.textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

let latestTelemetry = null;

function applyTelemetry(msg) {
  latestTelemetry = msg;
  if (typeof msg.cpu === "number") {
    teleCpuEl.textContent = `CPU ${Math.round(msg.cpu)}%`;
    teleCpuEl.classList.toggle("warm", msg.cpu >= 80);
  }
  if (typeof msg.mem_used_gb === "number" && typeof msg.mem_total_gb === "number") {
    teleRamEl.textContent = `RAM ${msg.mem_used_gb.toFixed(1)}/${Math.round(msg.mem_total_gb)} GB`;
    teleRamEl.classList.toggle("warm", msg.mem_used_gb / msg.mem_total_gb >= 0.9);
  }
}

// ---------- Background agent chips ----------
// The hub broadcasts tool_activity with name "agent:<tier>" when a sub-agent
// starts and finishes; each one gets a chip so long-running work is visible.

const AGENT_LABELS = { os: "OS agent", spatial: "Spatial agent" };
const agentChips = new Map();

function agentStart(tier, goal) {
  let chip = agentChips.get(tier);
  if (!chip) {
    chip = document.createElement("div");
    chip.className = "agent-chip";
    chip.innerHTML = '<span class="agent-spinner"></span>'
      + '<span class="agent-name"></span><span class="agent-goal"></span>';
    agentRailEl.appendChild(chip);
    agentChips.set(tier, chip);
  }
  clearTimeout(chip._removeTimer);
  chip.classList.remove("done");
  chip.querySelector(".agent-name").textContent = AGENT_LABELS[tier] || tier;
  chip.querySelector(".agent-goal").textContent = goal ? `· ${goal}` : "";
}

// A momentary WS drop (session rotation, reload) shouldn't wipe the status of
// agents that are still running on the backend; only a sustained outage does.
let agentClearTimer = null;

function scheduleAgentChipClear() {
  clearTimeout(agentClearTimer);
  agentClearTimer = setTimeout(() => {
    agentChips.forEach((chip) => chip.remove());
    agentChips.clear();
  }, 8000);
}

function cancelAgentChipClear() {
  clearTimeout(agentClearTimer);
  agentClearTimer = null;
}

function agentDone(tier, result) {
  const chip = agentChips.get(tier);
  if (!chip) return;
  chip.querySelector(".agent-goal").textContent = result ? `· ${result.slice(0, 60)}` : "· done";
  chip.classList.add("done");
  chip._removeTimer = setTimeout(() => {
    chip.remove();
    agentChips.delete(tier);
  }, 600);
}

// ---------- Caption ----------

let captionTimer = null;

function showCaption(text, kind = "ultron") {
  if (!text) return;
  clearTimeout(captionTimer);
  captionEl.textContent = text;
  captionEl.className = `caption show ${kind}`;
  const dwell = Math.min(14000, 3200 + text.length * 55);
  captionTimer = setTimeout(() => captionEl.classList.remove("show"), dwell);
}

// ---------- WebSocket ----------

function initWebSocket() {
  // Over HTTPS (Tailscale Serve) the WS is mounted on the same origin at /ws;
  // plain local access connects straight to the gateway port.
  const wsUrl = window.location.protocol === "https:"
    ? `wss://${window.location.host}/ws`
    : `ws://${window.location.hostname || "127.0.0.1"}:8765`;
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    cancelAgentChipClear();
    connectionState = "online";
    backendBusy = false;
    ws.send(JSON.stringify({ type: "client_hello", remote: IS_REMOTE }));
    initMicrophone();
  };

  ws.onmessage = (event) => {
    try {
      handleServerMessage(JSON.parse(event.data));
    } catch (e) {
      console.error("WS parse error:", e);
    }
  };

  ws.onclose = () => {
    connectionState = "offline";
    backendBusy = false;
    backendSpeaking = false;
    activeTools = 0;
    clearAudioSchedule();
    scheduleAgentChipClear();
    setTimeout(initWebSocket, 3000);
  };
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case "status":
      updateStatus(msg.status);
      break;

    case "audio_out":
      lastAudioOutAt = performance.now();
      backendBusy = false;
      if (msg.pcm_base64) {
        scheduleAudioChunk(msg.pcm_base64);
        if (IS_REMOTE) playPcmChunk(msg.pcm_base64);
      }
      break;

    case "exec_approval_request":
      showExecApproval(msg);
      break;

    case "exec_approval_closed":
      closeExecApproval(msg.id);
      break;

    case "chat_log":
      // System log lines stay out of the UI — the orb carries system state.
      if (msg.style === "system") console.info("[Ultron]", msg.text);
      else showCaption(msg.text, msg.style === "user" ? "user" : "ultron");
      break;

    case "sve_workspace":
    case "sve_scene_create":
    case "sve_scene_update":
    case "sve_scene_delete":
      window.SVE?.handleEvent(msg);
      syncGesturesToWorkspace();
      break;

    case "camera_frame":
      // Backend JPEG frames feed Gemini; preview uses the local 30fps
      // getUserMedia stream instead. Fallback to JPEGs only if that failed.
      if (msg.image_base64 && !UltronCamera.active()) {
        camImgEl.src = "data:image/jpeg;base64," + msg.image_base64;
        camImgEl.style.display = "block";
        camPlaceholder.style.display = "none";
      }
      break;

    case "screen_frame":
      if (msg.image_base64) {
        screenImgEl.src = "data:image/jpeg;base64," + msg.image_base64;
        screenImgEl.style.display = "block";
        screenPlaceholder.style.display = "none";
      }
      break;

    case "sense_update":
      updateSenseState(msg.camera_active, msg.screen_active);
      break;

    case "tool_activity": {
      const agentTier = msg.name && msg.name.startsWith("agent:") ? msg.name.slice(6) : null;
      if (msg.phase === "start") {
        activeTools++;
        if (agentTier) agentStart(agentTier, msg.args_preview);
      } else if (msg.phase === "done") {
        activeTools = Math.max(0, activeTools - 1);
        if (agentTier) agentDone(agentTier, msg.result_preview);
      }
      break;
    }

    case "system_telemetry":
      applyTelemetry(msg);
      break;

    case "widget_action":
      switch (msg.action) {
        case "sync":
          clearAllWidgetsLocal();
          (msg.widgets || []).forEach(mountWidget);
          break;
        case "create":  mountWidget(msg.widget); break;
        case "update":  patchWidget(msg.widget_id, msg.components); break;
        case "dismiss": dismissWidgetLocal(msg.widget_id); break;
        case "clear_all": clearAllWidgetsLocal(); break;
      }
      break;

    case "interrupted":
      backendSpeaking = false;
      lastAudioOutAt = 0;
      speakingLevel = 0;
      clearAudioSchedule();
      flushPlayback();
      break;
  }
}

function updateStatus(status) {
  const s = (status || "").toLowerCase();
  if (s.includes("error") || s.includes("failed") || s.includes("disconnected") || s.includes("shutting")) {
    connectionState = "offline";
    backendBusy = false;
  } else {
    connectionState = "online";
    backendBusy = s.includes("executing") || s.includes("connecting") || s.includes("resuming");
  }
  // "Speaking" is emitted per audio part; "Listening" lands on turn_complete;
  // "Listening (Interrupted)" on barge-in — all three settle the turn state.
  if (s.includes("speaking")) backendSpeaking = true;
  else if (s.includes("listening") || s.includes("interrupted")) backendSpeaking = false;

  if (s.includes("interrupted")) {
    lastAudioOutAt = 0;
    speakingLevel = 0;
    clearAudioSchedule();
  }
}

// ---------- Widget deck ----------
// The hub drives this by voice: create / update / dismiss / clear_all. The
// layout is a pure function of how many widgets are mounted (see setDeckCount),
// so the orb docks and the workspace opens without anything else being told.

const widgets = new Map();   // id -> { el, spec }

// Cards no longer have a fixed type, so the header icon is inferred from the
// first component — the one that sets the card's character.
const COMPONENT_ICONS = {
  hero_stat:      '<path d="M3 17l6-6 4 4 7-7"/><path d="M14 8h6v6"/>',
  chart_svg:      '<path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/>',
  metric_grid:    '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  feed_list:      '<path d="M4 5h16M4 10h16M4 15h10"/>',
  media_view:     '<rect x="3" y="4" width="18" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-6 6"/>',
  progress_gauge: '<rect x="3" y="3" width="18" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  "3d_spatial":   '<path d="M12 2l9 5v10l-9 5-9-5V7z"/><path d="M12 12l9-5M12 12v10M12 12L3 7"/>',
};

function widgetIcon(spec) {
  if (spec.type === "3d_spatial") return COMPONENT_ICONS["3d_spatial"];
  const first = (spec.components || [])[0];
  return COMPONENT_ICONS[first && first.type] || COMPONENT_ICONS.feed_list;
}

function setDeckCount() {
  const n = widgets.size;
  widgetDeckEl.dataset.count = String(n);
  document.body.classList.toggle("widgets-active", n > 0);
  pumpRenderers();
}

function mountWidget(spec) {
  let entry = widgets.get(spec.id);
  if (entry) {                       // same id -> patch in place, never re-add
    entry.spec = spec;
    renderWidgetBody(entry);
    return;
  }

  const el = document.createElement("div");
  el.className = "widget";
  el.dataset.type = spec.type;
  el.dataset.id = spec.id;
  el.innerHTML =
    '<div class="widget-head">'
    + `<span class="widget-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${widgetIcon(spec)}</svg></span>`
    + `<span class="widget-title">${esc(spec.title || spec.type)}</span>`
    + '<button class="widget-close" title="Dismiss">&times;</button>'
    + '</div><div class="widget-body"></div>';

  el.querySelector(".widget-close").addEventListener("click", () => {
    dismissWidgetLocal(spec.id);
    sendWidgetAction("dismiss_widget", { widget_id: spec.id });
  });

  widgetDeckEl.appendChild(el);
  entry = { el, spec };
  widgets.set(spec.id, entry);
  renderWidgetBody(entry);
  setDeckCount();
}

function patchWidget(id, components) {
  const entry = widgets.get(id);
  if (!entry) return;
  entry.spec.components = components;
  renderWidgetBody(entry);
}

function dismissWidgetLocal(id) {
  const entry = widgets.get(id);
  if (!entry) return;
  if (entry.spec.type === "3d_spatial") {
    parkSveStage();
    // The scenes have to go too, or the workspace watchdog sees a live scene
    // with no card and mounts it straight back.
    discardAllScenes();
  }
  entry.el.classList.add("leaving");
  widgets.delete(id);
  setTimeout(() => entry.el.remove(), 260);
  setDeckCount();
}

function clearAllWidgetsLocal() {
  [...widgets.keys()].forEach(dismissWidgetLocal);
}

function sendWidgetAction(tool, args) {
  // The hub owns the authoritative deck, so tell it what the user did here.
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "widget_user_action", tool, args }));
  }
}

// ---------- Component pipeline ----------
// A widget is a title plus an ordered array of declarative primitives. Each
// renderer takes one primitive and returns HTML; the card is their concatenation,
// so one card can carry a hero figure, a chart and a metric matrix at once.

const COMPONENTS = {
  hero_stat: renderHeroStat,
  chart_svg: renderChartSvg,
  metric_grid: renderMetricGrid,
  feed_list: renderFeedList,
  media_view: renderMediaView,
  progress_gauge: renderProgressGauge,
};

function renderWidgetBody(entry) {
  const body = entry.el.querySelector(".widget-body");
  if (entry.spec.type === "3d_spatial") { adoptSveStage(body); return; }

  const list = Array.isArray(entry.spec.components) ? entry.spec.components : [];
  const html = list.map((c) => {
    const fn = COMPONENTS[c && c.type];
    if (!fn) return "";
    try { return fn(c); } catch (e) { console.warn("component render failed", c && c.type, e); return ""; }
  }).join("");
  body.innerHTML = html || '<div class="cmp-empty">No components supplied.</div>';
}

function esc(v) {
  return String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function num(v) {
  const n = typeof v === "number" ? v : parseFloat(String(v).replace(/[^0-9.\-]/g, ""));
  return isFinite(n) ? n : NaN;
}

/** up / down / flat from an explicit direction, else from the change value. */
function trendOf(c) {
  const d = String(c.direction || "").toLowerCase();
  if (d === "up" || d === "down") return d;
  const pct = num(c.change_percent);
  if (isFinite(pct)) return Math.abs(pct) < 0.005 ? "flat" : pct > 0 ? "up" : "down";
  return "flat";
}

// ---------- hero_stat ----------

function renderHeroStat(c) {
  const dir = trendOf(c);
  const arrow = dir === "up" ? "\u25B2" : dir === "down" ? "\u25BC" : "\u25AC";
  const pct = num(c.change_percent);
  const bits = [];
  if (c.change_value != null && c.change_value !== "") bits.push(esc(c.change_value));
  if (isFinite(pct)) bits.push(`${pct > 0 ? "+" : ""}${pct.toFixed(2)}%`);

  return '<section class="cmp cmp-hero">'
    + '<div class="hero-row">'
    + `<span class="hero-value">${esc(c.value)}</span>`
    + (c.subtitle ? `<span class="hero-sub">${esc(c.subtitle)}</span>` : "")
    + (bits.length ? `<span class="hero-delta ${dir}">${arrow} ${bits.join("  ")}</span>` : "")
    + '</div>'
    + ((c.tag || c.timestamp)
        ? '<div class="hero-meta">'
          + (c.tag ? `<span class="hero-tag">${esc(c.tag)}</span>` : "")
          + (c.timestamp ? `<span class="hero-time">${esc(c.timestamp)}</span>` : "")
          + '</div>'
        : "")
    + '</section>';
}

// ---------- chart_svg ----------
// viewBox coordinates with preserveAspectRatio="none" so one path stretches to
// whatever width the card has, while stroke width stays constant.

function renderChartSvg(c) {
  const pts = (c.points || []).map(num).filter(isFinite);
  if (pts.length < 2) return "";

  const dir = (() => {
    const d = String(c.direction || "").toLowerCase();
    if (d === "up" || d === "down") return d;
    return pts[pts.length - 1] >= pts[0] ? "up" : "down";
  })();
  const stroke = dir === "down" ? "var(--ember)" : "var(--cyan)";

  const W = 100, H = 34;
  const base = num(c.baseline);
  const lo = Math.min(...pts, isFinite(base) ? base : Infinity);
  const hi = Math.max(...pts, isFinite(base) ? base : -Infinity);
  const span = hi - lo || 1;
  const pad = span * 0.12;
  const min = lo - pad, max = hi + pad;
  const y = (v) => H - ((v - min) / (max - min)) * H;
  const x = (i) => (i / (pts.length - 1)) * W;

  const line = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(2)} ${y(v).toFixed(2)}`).join(" ");
  const area = `${line} L${W} ${H} L0 ${H} Z`;
  const gid = `cg${Math.random().toString(36).slice(2, 8)}`;
  const baseY = isFinite(base) ? y(base).toFixed(2) : null;

  const svg = `<svg class="cmp-chart-svg ${dir}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
    + `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0%" stop-color="${stroke}" stop-opacity="0.28"/>`
    + `<stop offset="100%" stop-color="${stroke}" stop-opacity="0"/></linearGradient></defs>`
    + `<path d="${area}" fill="url(#${gid})"/>`
    + (baseY !== null
        ? `<line class="cmp-baseline" x1="0" y1="${baseY}" x2="${W}" y2="${baseY}"/>` : "")
    + `<path class="cmp-line" d="${line}" fill="none" stroke="${stroke}"/>`
    + '</svg>';

  const labels = Array.isArray(c.labels) ? c.labels : [];
  return '<section class="cmp cmp-chart">'
    + '<div class="chart-frame">'
    + `<span class="axis-y hi">${esc(fmtTick(hi))}</span>`
    + `<span class="axis-y lo">${esc(fmtTick(lo))}</span>`
    + (baseY !== null
        ? `<span class="baseline-tag" style="top:${(baseY / H) * 100}%">`
          + `${esc(c.baseline_label || "Previous close")} ${esc(fmtTick(base))}</span>` : "")
    + svg + '</div>'
    + (labels.length
        ? `<div class="axis-x">${labels.map((l) => `<span>${esc(l)}</span>`).join("")}</div>` : "")
    + '</section>';
}

function fmtTick(v) {
  if (!isFinite(v)) return "";
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 10) return v.toFixed(2);
  return v.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

// ---------- metric_grid ----------

function renderMetricGrid(c) {
  const items = Array.isArray(c.items) ? c.items : [];
  if (!items.length) return "";
  const cols = Math.max(2, Math.min(4, Number(c.columns) || 3));
  return `<section class="cmp cmp-metrics" style="--cols:${cols}">`
    + items.map((m) =>
        '<div class="metric">'
        + `<span class="metric-key">${esc(m.label)}</span>`
        + `<span class="metric-val">${esc(m.value ?? "—")}</span></div>`).join("")
    + '</section>';
}

// ---------- feed_list ----------

function renderFeedList(c) {
  const items = Array.isArray(c.items) ? c.items : [];
  if (!items.length) return "";
  return '<section class="cmp cmp-feed">'
    + items.map((it, i) =>
        '<article class="feed-item">'
        + `<span class="feed-idx">${String(i + 1).padStart(2, "0")}</span>`
        + '<div class="feed-main">'
        + '<div class="feed-top">'
        + (it.category ? `<span class="feed-badge">${esc(it.category)}</span>` : "")
        + `<span class="feed-head">${esc(it.headline ?? it.title ?? "")}</span></div>`
        + ((it.brief || it.timestamp)
            ? '<div class="feed-sub">'
              + (it.brief ? `<span>${esc(it.brief)}</span>` : "")
              + (it.timestamp ? `<span class="feed-time">${esc(it.timestamp)}</span>` : "")
              + '</div>'
            : "")
        + '</div></article>').join("")
    + '</section>';
}

// ---------- media_view ----------

function renderMediaView(c) {
  let inner = "";
  if (c.svg && String(c.svg).trim().startsWith("<svg")) {
    inner = String(c.svg);                      // trusted: composed by the hub
  } else if (c.url) {
    inner = `<img src="${esc(c.url)}" alt="${esc(c.caption || "")}" loading="lazy"`
          + ` onerror="this.closest('.media-frame').classList.add('failed')">`;
  } else {
    return "";
  }
  return '<section class="cmp cmp-media">'
    + `<div class="media-frame"><span class="hud tl"></span><span class="hud tr"></span>`
    + `<span class="hud bl"></span><span class="hud br"></span>${inner}`
    + '<span class="media-fallback">image unavailable</span></div>'
    + (c.caption ? `<div class="media-caption">${esc(c.caption)}</div>` : "")
    + '</section>';
}

// ---------- progress_gauge ----------

function renderProgressGauge(c) {
  const items = Array.isArray(c.items) ? c.items : [];
  if (!items.length) return "";
  const radial = String(c.style || "linear").toLowerCase() === "radial";
  return `<section class="cmp cmp-gauge ${radial ? "radial" : "linear"}">`
    + items.map((g) => {
        const max = num(g.max) || 100;
        const v = Math.max(0, Math.min(max, num(g.value) || 0));
        const pct = (v / max) * 100;
        const label = esc(g.label);
        const text = esc(g.display ?? `${Math.round(pct)}${g.suffix ?? "%"}`);
        const tone = pct >= 85 ? "hot" : pct >= 60 ? "warm" : "";
        if (!radial) {
          return '<div class="gauge-row">'
            + `<span class="gauge-label">${label}</span>`
            + `<span class="gauge-track"><span class="gauge-fill ${tone}" style="width:${pct.toFixed(1)}%"></span></span>`
            + `<span class="gauge-val">${text}</span></div>`;
        }
        const R = 26, C = 2 * Math.PI * R;
        return '<div class="gauge-dial">'
          + `<svg viewBox="0 0 64 64"><circle class="dial-bg" cx="32" cy="32" r="${R}"/>`
          + `<circle class="dial-fg ${tone}" cx="32" cy="32" r="${R}"`
          + ` stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${(C * (1 - pct / 100)).toFixed(1)}"/></svg>`
          + `<span class="dial-val">${text}</span><span class="gauge-label">${label}</span></div>`;
      }).join("")
    + '</section>';
}

// ---------- 3D stage adoption ----------
// The WebGL context, its ResizeObserver and every gesture overlay live on
// #sve-stage. Moving that one node into a card keeps all of it intact.

function adoptSveStage(body) {
  if (!sveStageEl || sveStageEl.parentElement === body) return;
  body.appendChild(sveStageEl);
  ensureSveControls();
  pumpRenderers();
}

function discardAllScenes() {
  const ws3d = window.SVE?.state?.workspace;
  if (!ws3d) return;
  Object.keys(ws3d).forEach((sceneId) => {
    window.sveSend?.({ type: "sve_user_action", scene_id: sceneId, action: "delete_scene" });
    window.SVE.handleEvent({ type: "sve_scene_delete", scene_id: sceneId });
  });
}

function parkSveStage() {
  if (sveStageEl && sveParkingEl && sveStageEl.parentElement !== sveParkingEl) {
    sveParkingEl.appendChild(sveStageEl);
  }
}

// The Hands / Reset pills belong to the card, so they are built with it.
function ensureSveControls() {
  if (sveStageEl.querySelector(".sve-controls")) return;
  const bar = document.createElement("div");
  bar.className = "sve-controls";
  bar.innerHTML =
    '<button class="pill" id="btn-hands" title="Hand gesture control">'
    + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M18 11V6a1.5 1.5 0 0 0-3 0"/><path d="M15 10.5V4a1.5 1.5 0 0 0-3 0v6.5"/>'
    + '<path d="M12 10.5V5a1.5 1.5 0 0 0-3 0v7"/>'
    + '<path d="M9 12V8.5a1.5 1.5 0 0 0-3 0V14a7 7 0 0 0 7 7h1a6 6 0 0 0 6-6v-3a1.5 1.5 0 0 0-3 0"/>'
    + '</svg><span>Hands</span></button>'
    + '<button class="pill" id="btn-reset-view" title="Reset camera">'
    + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
    + '<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>'
    + '</svg><span>Reset view</span></button>'
    + '<button class="pill close" id="btn-close-spatial" title="Dismiss">&times;</button>';
  sveStageEl.appendChild(bar);

  bar.querySelector("#btn-hands").addEventListener("click", () => {
    const g = window.UltronGestures;
    if (!g) return;
    g.running ? g.stop() : g.start();
  });
  bar.querySelector("#btn-reset-view").addEventListener("click", () => window.SVE?.resetCamera());
  bar.querySelector("#btn-close-spatial").addEventListener("click", () => {
    dismissWidgetLocal(SVE_WIDGET_ID);
    sendWidgetAction("dismiss_widget", { widget_id: SVE_WIDGET_ID });
  });
}

// gestures.js can no longer bind the pill itself (it is built on demand), so
// mirror its state here.
window.addEventListener("ultron-gesture-state", (e) => {
  document.getElementById("btn-hands")?.classList.toggle("active", !!e.detail.running);
});

// ---------- Spatial layout ----------

let resizePump = null;

/** Keep both WebGL renderers correct while the 600ms layout transition runs. */
function pumpRenderers(durationMs = 720) {
  cancelAnimationFrame(resizePump);
  const until = performance.now() + durationMs;
  const step = () => {
    window.UltronOrb?.resize();
    window.SVE?.resize?.();
    if (performance.now() < until) resizePump = requestAnimationFrame(step);
  };
  step();
}

// Feed stack height feeds the rail layout so the command pill never rides
// over the PIPs, whatever combination of feeds is live.
if (pipStackEl) {
  new ResizeObserver(() => {
    const h = pipStackEl.getBoundingClientRect().height;
    document.documentElement.style.setProperty("--pip-stack-h", `${Math.round(h)}px`);
  }).observe(pipStackEl);
}

window.addEventListener("resize", () => pumpRenderers(120));

// ---------- Shared webcam stream ----------
// Live preview and the gesture tracker share one refcounted getUserMedia
// stream (30fps) instead of the backend's 0.8s JPEG feed.

let remoteCamFacing = "environment"; // phone default: rear camera (show surroundings)

const UltronCamera = {
  stream: null,
  refs: 0,
  async acquire() {
    this.refs++;
    if (!this.stream) {
      const video = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };
      if (IS_REMOTE) video.facingMode = remoteCamFacing;
      this.stream = await navigator.mediaDevices.getUserMedia({ video });
    }
    return this.stream;
  },
  release() {
    this.refs = Math.max(0, this.refs - 1);
    if (this.refs === 0 && this.stream) {
      this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }
  },
  // Re-open with current constraints (camera flip) keeping refcount intact.
  async restart() {
    if (!this.stream) return null;
    this.stream.getTracks().forEach((t) => t.stop());
    this.stream = null;
    const video = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };
    if (IS_REMOTE) video.facingMode = remoteCamFacing;
    this.stream = await navigator.mediaDevices.getUserMedia({ video });
    return this.stream;
  },
  active() {
    return !!this.stream;
  },
};
window.UltronCamera = UltronCamera;

let previewOn = false;

// PIP tiles render only while their source is live.
function updateFeedCards() {
  const gestures = window.UltronGestures?.running;
  camFeedCard.style.display = (previewOn || isCamActive || gestures) ? "" : "none";
  screenFeedCard.style.display = isScreenActive ? "" : "none";
}

window.addEventListener("ultron-gesture-state", (e) => {
  if (e.detail.running) {
    startLocalPreview();
  } else if (!isCamActive) {
    stopLocalPreview();
  }
  updateFeedCards();
});

async function startLocalPreview() {
  if (previewOn) return;
  try {
    camVideoEl.srcObject = await UltronCamera.acquire();
    previewOn = true;
    camVideoEl.style.display = "block";
    camImgEl.style.display = "none";
    camPlaceholder.style.display = "none";
  } catch (e) {
    console.warn("Local camera preview failed, falling back to backend frames:", e);
  }
}

function stopLocalPreview() {
  if (!previewOn) return;
  previewOn = false;
  camVideoEl.srcObject = null;
  camVideoEl.style.display = "none";
  UltronCamera.release();
}

function updateSenseState(camActive, screenActive) {
  isCamActive = camActive;
  isScreenActive = screenActive;

  setDockState(btnCam, camActive);
  if (camActive) {
    startLocalPreview();
    if (IS_REMOTE) startRemoteCamPush();
  } else {
    stopRemoteCamPush();
    if (!window.UltronGestures?.running) {
      stopLocalPreview();
      camImgEl.style.display = "none";
      camPlaceholder.style.display = "block";
    }
  }

  setDockState(btnScreen, screenActive);
  if (!screenActive) {
    screenImgEl.style.display = "none";
    screenPlaceholder.style.display = "block";
  }

  updateFeedCards();
}

// ---------- Phone camera push (remote sessions) ----------
// While a remote session streams the camera, the phone captures ~1fps JPEG
// frames and ships them to the hub, which feeds them to the model instead of
// the Mac webcam.

let remoteCamTimer = null;
const remoteCamCanvas = document.createElement("canvas");

async function startRemoteCamPush() {
  if (!IS_REMOTE || remoteCamTimer) return;
  await startLocalPreview();
  remoteCamTimer = setInterval(pushRemoteCamFrame, 1000);
}

function stopRemoteCamPush() {
  if (remoteCamTimer) {
    clearInterval(remoteCamTimer);
    remoteCamTimer = null;
  }
}

function pushRemoteCamFrame() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const vw = camVideoEl.videoWidth;
  const vh = camVideoEl.videoHeight;
  if (!vw || !vh) return;
  const scale = Math.min(1, 1024 / vw);
  remoteCamCanvas.width = Math.round(vw * scale);
  remoteCamCanvas.height = Math.round(vh * scale);
  remoteCamCanvas.getContext("2d").drawImage(camVideoEl, 0, 0, remoteCamCanvas.width, remoteCamCanvas.height);
  const dataUrl = remoteCamCanvas.toDataURL("image/jpeg", 0.7);
  ws.send(JSON.stringify({ type: "remote_camera_frame", image_base64: dataUrl.split(",")[1] }));
}

const camFlipBtn = document.getElementById("btn-cam-flip");
if (IS_REMOTE && camFlipBtn) {
  camFlipBtn.style.display = "";
  camFlipBtn.addEventListener("click", async () => {
    remoteCamFacing = remoteCamFacing === "environment" ? "user" : "environment";
    try {
      const stream = await UltronCamera.restart();
      if (stream) camVideoEl.srcObject = stream;
    } catch (e) {
      console.warn("Camera flip failed:", e);
    }
  });
}

// ---------- Microphone (16 kHz PCM16 → hub → Gemini Live) ----------

async function initMicrophone() {
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });

    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
        sampleRate: 16000
      }
    });

    const sourceNode = audioCtx.createMediaStreamSource(micStream);

    audioAnalyser = audioCtx.createAnalyser();
    audioAnalyser.fftSize = 64;
    audioDataArray = new Uint8Array(audioAnalyser.frequencyBinCount);
    sourceNode.connect(audioAnalyser);

    scriptNode = audioCtx.createScriptProcessor(2048, 1, 1);
    sourceNode.connect(scriptNode);
    scriptNode.connect(audioCtx.destination);

    scriptNode.onaudioprocess = (e) => {
      if (isMicMuted || !ws || ws.readyState !== WebSocket.OPEN) return;

      const inputBuffer = e.inputBuffer.getChannelData(0);
      const pcmBuffer = new Int16Array(inputBuffer.length);
      for (let i = 0; i < inputBuffer.length; i++) {
        const s = Math.max(-1, Math.min(1, inputBuffer[i]));
        pcmBuffer[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }

      ws.send(JSON.stringify({
        type: "audio_in",
        pcm_base64: arrayBufferToBase64(pcmBuffer.buffer)
      }));
    };
  } catch (err) {
    showCaption("Microphone access error: " + err.message, "user");
  }
}

function arrayBufferToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return window.btoa(binary);
}

// ---------- Remote audio playback (phone / Tailscale clients) ----------
// The hub broadcasts Ultron's voice as 24kHz PCM16; remote devices have no
// path to the Mac's speakers, so schedule the chunks through Web Audio here.

let playbackCtx = null;
let playbackTime = 0;
const activeSources = new Set();

function ensurePlaybackCtx() {
  if (!playbackCtx) {
    playbackCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (playbackCtx.state === "suspended") playbackCtx.resume();
  return playbackCtx;
}

function playPcmChunk(b64) {
  try {
    const ctx = ensurePlaybackCtx();
    const raw = window.atob(b64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const pcm = new Int16Array(bytes.buffer);
    if (!pcm.length) return;

    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;

    const buf = ctx.createBuffer(1, f32.length, 24000);
    buf.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);

    const now = ctx.currentTime;
    if (playbackTime < now + 0.05) playbackTime = now + 0.05;
    src.start(playbackTime);
    playbackTime += buf.duration;
    activeSources.add(src);
    src.onended = () => activeSources.delete(src);
  } catch (e) {
    console.warn("PCM playback error:", e);
  }
}

function flushPlayback() {
  activeSources.forEach((s) => { try { s.stop(); } catch (_) {} });
  activeSources.clear();
  playbackTime = 0;
}

// Mobile browsers keep AudioContexts suspended until a user gesture; the
// first tap unlocks both mic capture and voice playback.
document.addEventListener("pointerdown", () => {
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  if (playbackCtx && playbackCtx.state === "suspended") playbackCtx.resume();
  if (IS_REMOTE && !micStream) initMicrophone();
}, { passive: true });

// ---------- Remote exec approval ----------

const approvalOverlayEl = document.getElementById("exec-approval");
const approvalCmdEl = document.getElementById("exec-approval-cmd");
const approvalToolEl = document.getElementById("exec-approval-tool");
let currentApprovalId = null;

function showExecApproval(msg) {
  currentApprovalId = msg.id;
  approvalToolEl.textContent = msg.tool === "execute_applescript_task" ? "AppleScript" : "Shell command";
  approvalCmdEl.textContent = msg.preview || "(empty)";
  approvalOverlayEl.style.display = "flex";
  if (navigator.vibrate) navigator.vibrate(120);
}

function closeExecApproval(id) {
  if (id && id !== currentApprovalId) return;
  currentApprovalId = null;
  approvalOverlayEl.style.display = "none";
}

function respondExecApproval(approved) {
  if (!currentApprovalId) return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: "exec_approval_response",
      id: currentApprovalId,
      approved: approved
    }));
  }
  showCaption((approved ? "Approved" : "Denied") + " remote command.", "user");
  closeExecApproval(currentApprovalId);
}

document.getElementById("exec-approve").addEventListener("click", () => respondExecApproval(true));
document.getElementById("exec-deny").addEventListener("click", () => respondExecApproval(false));

// ---------- Sensor dock ----------

function setDockState(btn, on) {
  btn.classList.toggle("active", on);
  btn.setAttribute("aria-pressed", String(on));
}

btnMic.addEventListener("click", () => {
  isMicMuted = !isMicMuted;
  setDockState(btnMic, !isMicMuted);
});

btnCam.addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "toggle_camera", active: !isCamActive }));
  }
});

btnScreen.addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "toggle_screen", active: !isScreenActive }));
  }
});

// Tapping the orb is the barge-in gesture — it replaces the old Stop button.
orbStageEl.addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "interrupt" }));
  }
  backendSpeaking = false;
  lastAudioOutAt = 0;
  speakingLevel = 0;
  clearAudioSchedule();
  flushPlayback();
});

// ---------- Command line ----------

function sendUserText() {
  const text = chatInputEl.value.trim();
  if (!text) return;

  showCaption(text, "user");
  chatInputEl.value = "";
  commandPillEl.classList.remove("filled");

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "user_text", text: text }));
  }
}

sendBtnEl.addEventListener("click", sendUserText);
chatInputEl.addEventListener("input", () => {
  commandPillEl.classList.toggle("filled", chatInputEl.value.length > 0);
});
chatInputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendUserText();
  if (e.key === "Escape") { chatInputEl.value = ""; commandPillEl.classList.remove("filled"); chatInputEl.blur(); }
});

// Type anywhere to summon the command line — no visible affordance needed.
window.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.activeElement === chatInputEl) return;
  if (document.activeElement && document.activeElement.tagName === "INPUT") return;
  if (e.key.length === 1 && e.key !== " ") {
    chatInputEl.focus();
  }
});

// ---------- SVE bridge ----------

// sve.js (module) calls this to report user interactions back to the engine.
window.sveSend = (obj) => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
};

// Hands-on by default whenever a live scene is on stage; off (camera released)
// when the workspace empties. Also drives the spatial layout toggle, since
// sve.js calls this hook whenever the scene set changes.
function syncGesturesToWorkspace() {
  // A live scene always gets a 3d_spatial card; the hub is told so its deck
  // stays in step with what is actually on screen.
  const hasScene = !!window.SVE?.hasActiveScene();
  const mounted = widgets.has(SVE_WIDGET_ID);
  if (hasScene && !mounted) {
    mountWidget({ id: SVE_WIDGET_ID, type: "3d_spatial", title: "Spatial", payload: {} });
    sendWidgetAction("create_widget", {
      widget_id: SVE_WIDGET_ID, widget_type: "3d_spatial", title: "Spatial", components: "[]",
    });
  } else if (!hasScene && mounted) {
    dismissWidgetLocal(SVE_WIDGET_ID);
    sendWidgetAction("dismiss_widget", { widget_id: SVE_WIDGET_ID });
  }

  const g = window.UltronGestures;
  if (!g || !window.SVE) return;
  if (hasScene) {
    if (!g.running && !g.starting) g.start();
  } else if (g.running) {
    g.stop();
  }
}
const SVE_WIDGET_ID = "spatial";
window.syncGesturesToWorkspace = syncGesturesToWorkspace;

// Watchdog: recovers from transient start failures (permission just granted,
// GPU delegate fallback, camera briefly busy) without user interaction.
setInterval(syncGesturesToWorkspace, 5000);

// ---------- Boot ----------

window.addEventListener("DOMContentLoaded", () => {
  tickClock();
  setInterval(tickClock, 1000);
  requestAnimationFrame(pumpOrb);
  initWebSocket();
});
