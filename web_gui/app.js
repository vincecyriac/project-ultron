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

// ---------- Orb state machine ----------
// Priority: offline > speaking > thinking > listening > idle.

let lastAudioOutAt = 0;
let speakingLevel = 0;      // envelope from the model's outgoing PCM
let micLevel = 0;           // envelope from the local analyser
let activeTools = 0;
let connectionState = "connecting";   // connecting | online | offline
let backendBusy = false;    // backend reported an executing/connecting status

const SPEAK_HOLD_MS = 450;
const MIC_GATE = 0.055;

function resolveOrbState() {
  if (connectionState === "offline") return "offline";
  if (performance.now() - lastAudioOutAt < SPEAK_HOLD_MS) return "speaking";
  if (activeTools > 0 || backendBusy) return "thinking";
  if (!isMicMuted && micLevel > MIC_GATE) return "listening";
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

  speakingLevel *= 0.90;

  const state = resolveOrbState();
  orb.setState(state);

  if (state === "speaking") orb.setLevel(Math.min(1, speakingLevel * 2.2));
  else if (state === "listening") orb.setLevel(Math.min(1, micLevel * 1.8));
  else if (state === "thinking") orb.setLevel(0.12 + Math.abs(Math.sin(performance.now() / 520)) * 0.20);
  else orb.setLevel(micLevel * 0.6);
}

/** RMS of a base64 PCM16 chunk, sampled sparsely — cheap enough per chunk. */
function pcmRms(b64) {
  try {
    const raw = window.atob(b64);
    const n = (raw.length / 2) | 0;
    if (!n) return 0;
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
    return Math.sqrt(sum / count);
  } catch (_) {
    return 0;
  }
}

// ---------- Telemetry ----------

function tickClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  teleClockEl.textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function applyTelemetry(msg) {
  if (typeof msg.cpu === "number") {
    teleCpuEl.textContent = `CPU ${Math.round(msg.cpu)}%`;
    teleCpuEl.classList.toggle("warm", msg.cpu >= 80);
  }
  if (typeof msg.mem_used_gb === "number" && typeof msg.mem_total_gb === "number") {
    teleRamEl.textContent = `RAM ${msg.mem_used_gb.toFixed(1)}/${Math.round(msg.mem_total_gb)} GB`;
    teleRamEl.classList.toggle("warm", msg.mem_used_gb / msg.mem_total_gb >= 0.9);
  }
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
    activeTools = 0;
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
        speakingLevel = Math.max(speakingLevel, pcmRms(msg.pcm_base64));
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

    case "tool_activity":
      if (msg.phase === "start") activeTools++;
      else if (msg.phase === "done") activeTools = Math.max(0, activeTools - 1);
      break;

    case "system_telemetry":
      applyTelemetry(msg);
      break;

    case "interrupted":
      lastAudioOutAt = 0;
      speakingLevel = 0;
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
  if (s.includes("interrupted")) {
    lastAudioOutAt = 0;
    speakingLevel = 0;
  }
}

// ---------- Spatial layout ----------

let spatialActive = false;
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

function setSpatialMode(active) {
  if (active === spatialActive) return;
  spatialActive = active;
  document.body.classList.toggle("spatial-active", active);
  pumpRenderers();
}

function syncSpatialMode() {
  setSpatialMode(!!window.SVE?.hasActiveScene());
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
  lastAudioOutAt = 0;
  speakingLevel = 0;
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

document.getElementById("btn-reset-view").addEventListener("click", () => window.SVE?.resetCamera());

// Hands-on by default whenever a live scene is on stage; off (camera released)
// when the workspace empties. Also drives the spatial layout toggle, since
// sve.js calls this hook whenever the scene set changes.
function syncGesturesToWorkspace() {
  syncSpatialMode();

  const g = window.UltronGestures;
  if (!g || !window.SVE) return;
  if (spatialActive && window.SVE.hasActiveScene()) {
    if (!g.running && !g.starting) g.start();
  } else if (g.running) {
    g.stop();
  }
}
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
