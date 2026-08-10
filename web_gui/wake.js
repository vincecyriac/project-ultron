/**
 * Wake engine — passive listening while Ultron is dormant.
 *
 * Ultron boots asleep: the hub holds no Gemini Live socket and no audio leaves
 * the browser. This module listens locally for two triggers and nothing else:
 *
 *   1. Double clap  — Web Audio transient detection (spectral flux + high-band
 *                     ratio), fully local, no network.
 *   2. "Ultron"     — SpeechRecognition wake word when the browser exposes it
 *                     (Chrome/Edge/Safari); silently skipped elsewhere.
 *
 * It also synthesises the activation/deactivation sounds, so there are no audio
 * assets to ship.
 */

const CLAP = {
  minRms: 0.075,        // absolute floor: ignore room noise
  floorRatio: 4.5,      // onset must beat the rolling noise floor by this much
  hfRatio: 0.30,        // claps are broadband — most energy above ~2kHz
  refractoryMs: 110,    // one clap cannot re-trigger itself
  minGapMs: 90,         // double-clap window
  maxGapMs: 700,
  windowMs: 950,        // onsets older than this are forgotten
};

const WAKE_WORD = /\bul+\s?t+r+o+n+\b|\baltron\b|\bultra\s?on\b|\ball\s?tron\b/i;

let audioCtx = null;
let analyser = null;
let timeBuf = null;
let freqBuf = null;
let timer = null;
let listening = false;

let noiseFloor = 0.01;
let lastOnset = 0;
let onsets = [];

let recog = null;
let recogWanted = false;
let recogBackoff = 500;

let onWake = null;

// ---------- clap detection ----------

function analyse() {
  if (!listening || !analyser) return;

  analyser.getByteTimeDomainData(timeBuf);
  let sum = 0;
  for (let i = 0; i < timeBuf.length; i++) {
    const v = (timeBuf[i] - 128) / 128;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / timeBuf.length);

  analyser.getByteFrequencyData(freqBuf);
  // bin width = sampleRate / fftSize; split the spectrum at ~2kHz
  const binHz = audioCtx.sampleRate / (analyser.fftSize || 1024);
  const split = Math.min(freqBuf.length - 1, Math.floor(2000 / binHz));
  let low = 0;
  let high = 0;
  for (let i = 0; i < freqBuf.length; i++) {
    if (i <= split) low += freqBuf[i]; else high += freqBuf[i];
  }
  const hf = high / (low + high + 1e-6);

  const now = performance.now();
  const isOnset =
    rms > CLAP.minRms &&
    rms > noiseFloor * CLAP.floorRatio &&
    hf > CLAP.hfRatio &&
    now - lastOnset > CLAP.refractoryMs;

  if (isOnset) {
    lastOnset = now;
    onsets = onsets.filter((t) => now - t < CLAP.windowMs);
    const prev = onsets[onsets.length - 1];
    onsets.push(now);
    if (prev) {
      const gap = now - prev;
      if (gap >= CLAP.minGapMs && gap <= CLAP.maxGapMs) {
        onsets = [];
        trigger("double_clap");
      }
    }
  } else {
    // track the room only while it is quiet, so a clap never raises the floor
    noiseFloor = noiseFloor * 0.99 + rms * 0.01;
  }
}

// ---------- wake word ----------

function startRecognition() {
  const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Ctor || recog) return;
  try {
    recog = new Ctor();
  } catch (_) {
    recog = null;
    return;
  }
  recog.continuous = true;
  recog.interimResults = true;
  recog.lang = "en-US";

  recog.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const text = e.results[i][0].transcript || "";
      if (WAKE_WORD.test(text)) {
        trigger("wake_word");
        return;
      }
    }
  };
  recog.onerror = (e) => {
    // "not-allowed"/"service-not-allowed" mean the browser refuses: stop retrying
    if (e.error === "not-allowed" || e.error === "service-not-allowed") recogWanted = false;
  };
  recog.onend = () => {
    recog = null;
    if (!recogWanted) return;
    setTimeout(startRecognition, recogBackoff);
    recogBackoff = Math.min(8000, recogBackoff * 1.6);
  };

  try {
    recog.start();
    recogBackoff = 500;
  } catch (_) {
    recog = null;
  }
}

function stopRecognition() {
  recogWanted = false;
  if (recog) {
    try { recog.abort(); } catch (_) {}
    recog = null;
  }
}

function trigger(reason) {
  if (!listening) return;
  listening = false;          // one shot; app re-arms when it goes dormant again
  stopRecognition();
  onsets = [];
  if (onWake) onWake(reason);
}

// ---------- synthesised UI sounds ----------
// Inharmonic partials over a fast attack read as struck metal; the noise burst
// gives the strike its "air".

function metallic({ base, ratios, decay, gain, sweep }) {
  const ctx = audioCtx;
  if (!ctx) return;
  if (ctx.state === "suspended") ctx.resume();
  const t0 = ctx.currentTime + 0.01;

  const bus = ctx.createGain();
  bus.gain.value = gain;
  const shaper = ctx.createBiquadFilter();
  shaper.type = "highpass";
  shaper.frequency.value = 220;
  bus.connect(shaper).connect(ctx.destination);

  ratios.forEach((ratio, i) => {
    const osc = ctx.createOscillator();
    const env = ctx.createGain();
    osc.type = i === 0 ? "triangle" : "sine";
    osc.frequency.setValueAtTime(base * ratio, t0);
    if (sweep) osc.frequency.exponentialRampToValueAtTime(base * ratio * sweep, t0 + decay * 0.9);
    const peak = 1 / (i + 1.6);
    env.gain.setValueAtTime(0.0001, t0);
    env.gain.exponentialRampToValueAtTime(peak, t0 + 0.006);
    env.gain.exponentialRampToValueAtTime(0.0001, t0 + decay * (1 - i * 0.13));
    osc.connect(env).connect(bus);
    osc.start(t0);
    osc.stop(t0 + decay + 0.1);
  });

  // strike transient
  const len = Math.floor(ctx.sampleRate * 0.09);
  const buf = ctx.createBuffer(1, len, ctx.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3);
  const noise = ctx.createBufferSource();
  noise.buffer = buf;
  const bp = ctx.createBiquadFilter();
  bp.type = "bandpass";
  bp.frequency.value = 3200;
  bp.Q.value = 0.9;
  const ng = ctx.createGain();
  ng.gain.value = gain * 0.5;
  noise.connect(bp).connect(ng).connect(ctx.destination);
  noise.start(t0);
}

window.UltronWake = {
  /** Share app.js's mic graph: {audioCtx, sourceNode}. */
  attach({ audioCtx: ctx, sourceNode }) {
    audioCtx = ctx;
    analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0;
    timeBuf = new Uint8Array(analyser.fftSize);
    freqBuf = new Uint8Array(analyser.frequencyBinCount);
    sourceNode.connect(analyser);
    if (!timer) timer = setInterval(analyse, 20);
  },

  /** Arm/disarm passive listening. Armed only while Ultron is dormant. */
  setListening(flag) {
    listening = !!flag && !!analyser;
    if (listening) {
      noiseFloor = 0.01;
      onsets = [];
      recogWanted = true;
      startRecognition();
    } else {
      stopRecognition();
    }
  },

  get armed() { return listening; },

  /** Called with the trigger reason: "double_clap" | "wake_word" | ... */
  set handler(fn) { onWake = fn; },

  /** Soft metallic activation sweep. */
  chimeWake() {
    metallic({ base: 660, ratios: [1, 2.76, 5.4, 8.93], decay: 1.5, gain: 0.16, sweep: 1.02 });
  },

  /** Descending counterpart when Ultron goes dormant. */
  chimeSleep() {
    metallic({ base: 392, ratios: [1, 2.4, 4.1], decay: 0.9, gain: 0.11, sweep: 0.62 });
  },

  /** Available so the app can share one synth for other UI feedback. */
  chimeAlert() {
    metallic({ base: 880, ratios: [1, 3.1], decay: 0.5, gain: 0.13, sweep: 1 });
  },
};
