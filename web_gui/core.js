/**
 * Ultron Brain Core — the GUI's only status surface.
 *
 * Canvas-2D reactor orb. The UI carries no status labels: the core's colour,
 * ring velocity, corona amplitude and pulse rate ARE the readout
 * (dormant / waking / listening / thinking / speaking / error). Amplitude comes
 * straight off a Web Audio analyser, so the orb breathes with the live mic and
 * with Ultron's voice.
 */

const STATES = {
  // hue triplets are rgb; energy drives glow + ring speed + corona gain
  dormant:   { rgb: [ 74,  99, 115], energy: 0.16, spin: 0.10, breathe: 0.55 },
  waking:    { rgb: [225, 250, 255], energy: 1.00, spin: 1.40, breathe: 2.20 },
  listening: { rgb: [  0, 243, 255], energy: 0.62, spin: 0.55, breathe: 1.05 },
  thinking:  { rgb: [255, 176,  32], energy: 0.80, spin: 1.15, breathe: 1.60 },
  speaking:  { rgb: [138, 110, 255], energy: 0.92, spin: 0.80, breathe: 1.30 },
  error:     { rgb: [255,  77,  94], energy: 0.70, spin: 0.22, breathe: 2.60 },
};

const BINS = 72;

let canvas = null;
let ctx = null;
let dpr = 1;
let W = 0;
let H = 0;

let stateName = "dormant";
let target = STATES.dormant;
let cur = { rgb: [...STATES.dormant.rgb], energy: 0.16, spin: 0.1, breathe: 0.55 };

let analyser = null;
let freqData = null;
let extLevel = 0;          // level pushed by app.js (e.g. Ultron's outbound voice)
let level = 0;             // smoothed 0..1 amplitude actually rendered
const spectrum = new Float32Array(BINS);

let ringAngle = 0;
let phase = 0;
let docked = false;
const shockwaves = [];     // expanding activation rings

function lerp(a, b, t) { return a + (b - a) * t; }

function resize() {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  W = rect.width;
  H = rect.height;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

/**
 * Current amplitude + ring spectrum. The mic analyser drives it while Vince
 * speaks; pushed levels (Ultron's outbound voice, which the Mac plays and the
 * browser never analyses) take over whenever they are louder.
 */
function sampleAudio() {
  let micLevel = 0;
  const bins = new Float32Array(BINS);

  if (analyser && freqData) {
    analyser.getByteFrequencyData(freqData);
    const n = freqData.length;
    let sum = 0;
    for (let i = 0; i < BINS; i++) {
      // log-ish bin mapping keeps voice energy spread around the ring
      const idx = Math.min(n - 1, Math.floor(Math.pow(i / BINS, 1.7) * n));
      bins[i] = freqData[idx] / 255;
      sum += bins[i];
    }
    micLevel = sum / BINS;
  }

  extLevel *= 0.88;

  if (extLevel > micLevel) {
    // Synthesise a plausible spectrum around the pushed level.
    for (let i = 0; i < BINS; i++) {
      const wob = 0.45 + 0.55 * Math.abs(Math.sin(phase * 2.1 + i * 0.55) * Math.sin(phase * 0.9 + i * 0.17));
      spectrum[i] = lerp(spectrum[i], extLevel * wob, 0.3);
    }
  } else {
    for (let i = 0; i < BINS; i++) spectrum[i] = lerp(spectrum[i], bins[i], 0.35);
  }

  level = lerp(level, Math.min(1, Math.max(micLevel, extLevel) * 2.2), 0.18);
}

function ring(cx, cy, r, tilt, rotation, alpha, width, dash) {
  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(rotation);
  ctx.scale(1, tilt);
  ctx.beginPath();
  ctx.arc(0, 0, r, 0, Math.PI * 2);
  ctx.restore();
  ctx.globalAlpha = alpha;
  ctx.lineWidth = width;
  if (dash) ctx.setLineDash(dash);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
}

function draw() {
  requestAnimationFrame(draw);
  if (!ctx || !W) return;

  // ease every visual property so state changes read as a transition
  cur.rgb = cur.rgb.map((v, i) => lerp(v, target.rgb[i], 0.06));
  cur.energy = lerp(cur.energy, target.energy, 0.05);
  cur.spin = lerp(cur.spin, target.spin, 0.05);
  cur.breathe = lerp(cur.breathe, target.breathe, 0.05);

  sampleAudio();

  phase += 0.016;
  ringAngle += 0.004 + cur.spin * 0.012;

  const cx = W / 2;
  const cy = H / 2;
  const maxR = Math.min(W, H) / 2;      // everything must fade out INSIDE the
  const base = maxR * (docked ? 0.60 : 0.40); // canvas or its square edge shows
  const breath = 1 + 0.035 * Math.sin(phase * cur.breathe) + level * 0.10;
  const R = base * breath;
  const [r, g, b] = cur.rgb.map(Math.round);
  const col = (a) => `rgba(${r},${g},${b},${a})`;
  const rad = (mult) => Math.min(R * mult, maxR * 0.96);

  ctx.clearRect(0, 0, W, H);
  ctx.globalCompositeOperation = "lighter";

  // --- outer atmosphere -------------------------------------------------
  const halo = ctx.createRadialGradient(cx, cy, R * 0.5, cx, cy, maxR);
  halo.addColorStop(0, col(0.30 * cur.energy + level * 0.20));
  halo.addColorStop(0.45, col(0.10 * cur.energy));
  halo.addColorStop(1, col(0));
  ctx.fillStyle = halo;
  ctx.fillRect(0, 0, W, H);

  // --- gyroscope rings --------------------------------------------------
  ctx.strokeStyle = col(1);
  const ringAlpha = 0.10 + cur.energy * 0.42;
  ring(cx, cy, rad(1.75), 0.30, ringAngle, ringAlpha, 1.1, [14, 10]);
  ring(cx, cy, rad(2.1), 0.62, -ringAngle * 0.7 + 1.1, ringAlpha * 0.7, 1, [4, 12]);
  if (!docked) {
    ring(cx, cy, rad(2.35), 0.18, ringAngle * 0.45 + 2.2, ringAlpha * 0.5, 1, [40, 26]);
  }

  // --- audio corona -----------------------------------------------------
  const gain = R * (0.20 + 0.55 * level) * (0.4 + cur.energy);
  ctx.beginPath();
  for (let i = 0; i <= BINS; i++) {
    const k = i % BINS;
    const a = (k / BINS) * Math.PI * 2 - Math.PI / 2;
    const rr = Math.min(R * 1.16 + spectrum[k] * gain, maxR * 0.96);
    const x = cx + Math.cos(a) * rr;
    const y = cy + Math.sin(a) * rr;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.strokeStyle = col(0.35 + level * 0.5);
  ctx.lineWidth = 1.6;
  ctx.stroke();

  // spectral spokes: only legible once there is real signal
  if (level > 0.04) {
    ctx.lineWidth = docked ? 1 : 1.8;
    for (let i = 0; i < BINS; i += 2) {
      const a = (i / BINS) * Math.PI * 2 - Math.PI / 2;
      const inner = R * 1.10;
      const outer = Math.min(inner + spectrum[i] * gain * 1.15, maxR * 0.96);
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
      ctx.lineTo(cx + Math.cos(a) * outer, cy + Math.sin(a) * outer);
      ctx.strokeStyle = col(0.10 + spectrum[i] * 0.55);
      ctx.stroke();
    }
  }

  // --- plasma core ------------------------------------------------------
  const wobX = cx + Math.sin(phase * 1.3) * R * 0.10;
  const wobY = cy + Math.cos(phase * 1.7) * R * 0.10;
  const core = ctx.createRadialGradient(wobX, wobY, 0, cx, cy, R);
  const hot = 0.55 + cur.energy * 0.45;
  core.addColorStop(0, `rgba(255,255,255,${hot})`);
  core.addColorStop(0.35, col(0.85 * (0.3 + cur.energy)));
  core.addColorStop(0.78, col(0.35 * (0.3 + cur.energy)));
  core.addColorStop(1, col(0));
  ctx.fillStyle = core;
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.fill();

  // containment shell
  ctx.beginPath();
  ctx.arc(cx, cy, R * 1.03, 0, Math.PI * 2);
  ctx.strokeStyle = col(0.55);
  ctx.lineWidth = 1;
  ctx.stroke();

  // --- activation shockwaves -------------------------------------------
  for (let i = shockwaves.length - 1; i >= 0; i--) {
    const s = shockwaves[i];
    s.t += 0.022;
    if (s.t >= 1) { shockwaves.splice(i, 1); continue; }
    if (s.t <= 0) continue; // staggered pulse not born yet
    const rr = Math.min(R * (1 + s.t * 3.4), maxR * 0.98);
    ctx.beginPath();
    ctx.arc(cx, cy, rr, 0, Math.PI * 2);
    ctx.strokeStyle = col((1 - s.t) * 0.55);
    ctx.lineWidth = 2 * (1 - s.t) + 0.4;
    ctx.stroke();
  }

  ctx.globalCompositeOperation = "source-over";
}

window.UltronCore = {
  init(el) {
    canvas = el;
    ctx = canvas.getContext("2d");
    resize();
    new ResizeObserver(resize).observe(canvas);
    window.addEventListener("resize", resize);
    requestAnimationFrame(draw);
  },

  /** dormant | waking | listening | thinking | speaking | error */
  setState(name) {
    if (!STATES[name] || name === stateName) return;
    stateName = name;
    target = STATES[name];
  },

  get state() { return stateName; },

  /** Mic (or any) analyser node the corona reads its spectrum from. */
  setAnalyser(node) {
    analyser = node || null;
    freqData = node ? new Uint8Array(node.frequencyBinCount) : null;
  },

  /** Push an amplitude 0..1 for sources with no analyser (remote voice PCM). */
  setLevel(v) {
    extLevel = Math.max(extLevel, Math.min(1, Math.max(0, v)));
  },

  /** Expanding ring — used on wake, interrupt, and widget spawns. */
  pulse(count = 1) {
    for (let i = 0; i < count; i++) shockwaves.push({ t: -i * 0.18 });
  },

  setDocked(flag) { docked = !!flag; },
};
