/**
 * Ultron — frontend engine.
 * WebSocket bridge to the Python backend, echo-cancelled mic streaming,
 * chat, live feeds, tool activity, and a minimal waveform visualizer.
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
let modelSpeaking = false;

// DOM
const statusTextEl = document.getElementById("status-text");
const systemStatusEl = document.getElementById("system-status");
const audioStateTagEl = document.getElementById("audio-state-tag");
const brandMarkEl = document.getElementById("brand-mark");

const btnMic = document.getElementById("btn-mic");
const btnCam = document.getElementById("btn-cam");
const btnScreen = document.getElementById("btn-screen");
const btnInterrupt = document.getElementById("btn-interrupt");

const senseMicEl = document.getElementById("sense-mic");
const senseCamEl = document.getElementById("sense-cam");
const senseScreenEl = document.getElementById("sense-screen");

const chatMessagesEl = document.getElementById("chat-messages");
const chatInputEl = document.getElementById("chat-input");
const sendBtnEl = document.getElementById("send-btn");

const visBadgeEl = document.getElementById("vis-badge");

const camImgEl = document.getElementById("webcam-img");
const camPlaceholder = document.getElementById("cam-placeholder");
const screenImgEl = document.getElementById("screen-img");
const screenPlaceholder = document.getElementById("screen-placeholder");

const modelSelectEl = document.getElementById("model-select");

function populateModelList(models, activeId) {
  modelSelectEl.innerHTML = "";
  models.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.label;
    modelSelectEl.appendChild(opt);
  });
  modelSelectEl.value = activeId;
}

modelSelectEl.addEventListener("change", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "select_model", id: modelSelectEl.value }));
  }
});

const activityFeedEl = document.getElementById("activity-feed");
const activityEmptyEl = document.getElementById("activity-empty");
const actBadgeEl = document.getElementById("act-badge");
const pendingActivities = {};

// Remote = served through Tailscale (HTTPS, non-localhost). Remote clients
// play Ultron's voice in the browser; local ones rely on the Mac's speakers.
const IS_REMOTE = !["127.0.0.1", "localhost", ""].includes(window.location.hostname);

// ---------- WebSocket ----------

function initWebSocket() {
  // Over HTTPS (Tailscale Serve) the WS is mounted on the same origin at /ws;
  // plain local access connects straight to the gateway port.
  const wsUrl = window.location.protocol === "https:"
    ? `wss://${window.location.host}/ws`
    : `ws://${window.location.hostname || "127.0.0.1"}:8765`;
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    updateStatus("Listening");
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
    updateStatus("Disconnected");
    setTimeout(initWebSocket, 3000);
  };
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case "status":
      updateStatus(msg.status);
      break;

    case "audio_out":
      modelSpeaking = true;
      audioStateTagEl.textContent = "Speaking";
      brandMarkEl.classList.add("speaking");
      if (IS_REMOTE && msg.pcm_base64) playPcmChunk(msg.pcm_base64);
      break;

    case "exec_approval_request":
      showExecApproval(msg);
      break;

    case "exec_approval_closed":
      closeExecApproval(msg.id);
      break;

    case "chat_log":
      addChatMessage(msg.sender, msg.text, msg.style);
      break;

    case "sve_workspace":
    case "sve_scene_create":
    case "sve_scene_update":
    case "sve_scene_delete": {
      const wantsAttention = window.SVE && window.SVE.handleEvent(msg);
      if (wantsAttention && !document.getElementById("tab-vis").classList.contains("active")) {
        visBadgeEl.classList.add("active");
      }
      if (msg.type === "sve_scene_create") switchTab("vis");
      syncGesturesToWorkspace();
      break;
    }

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
      updateSenseBadges(msg.camera_active, msg.screen_active);
      break;

    case "tool_activity":
      addToolActivity(msg);
      break;

    case "model_list":
      populateModelList(msg.models, msg.active_id);
      break;

    case "model_changed":
      if (modelSelectEl.value !== msg.id) modelSelectEl.value = msg.id;
      addChatMessage("System", `Model switched to ${msg.label}` +
        (msg.model_type === "local" ? " — text chat mode (voice needs Gemini Live)." : "."), "system");
      break;

    case "interrupted":
      modelSpeaking = false;
      audioStateTagEl.textContent = "Interrupted";
      brandMarkEl.classList.remove("speaking");
      flushPlayback();
      break;
  }
}

// ---------- Status ----------

function updateStatus(status) {
  statusTextEl.textContent = status;
  const s = status.toLowerCase();

  systemStatusEl.classList.remove("listening", "speaking", "executing", "error");

  if (s.includes("speaking")) {
    modelSpeaking = true;
    audioStateTagEl.textContent = "Speaking";
    brandMarkEl.classList.add("speaking");
    systemStatusEl.classList.add("speaking");
  } else if (s.includes("listening")) {
    modelSpeaking = false;
    audioStateTagEl.textContent = "Listening";
    brandMarkEl.classList.remove("speaking");
    systemStatusEl.classList.add("listening");
  } else if (s.includes("executing")) {
    audioStateTagEl.textContent = "Working";
    systemStatusEl.classList.add("executing");
  } else if (s.includes("error") || s.includes("failed") || s.includes("disconnected")) {
    systemStatusEl.classList.add("error");
  }
}

// Shared, refcounted webcam stream: live preview + gesture tracker use one
// getUserMedia stream (30fps) instead of the backend's 0.8s JPEG feed.
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

const camVideoEl = document.getElementById("webcam-video");
const camFeedCard = document.getElementById("cam-feed-card");
const screenFeedCard = document.getElementById("screen-feed-card");
const camFeedLabel = document.getElementById("cam-feed-label");
let previewOn = false;

// Feed cards render only while their source is live.
function updateFeedCards() {
  const gestures = window.UltronGestures?.running;
  const camLive = previewOn || isCamActive || gestures;
  camFeedCard.style.display = camLive ? "" : "none";
  camFeedLabel.textContent = gestures ? "Camera · hand tracking" : "Camera";
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

function updateSenseBadges(camActive, screenActive) {
  isCamActive = camActive;
  isScreenActive = screenActive;

  senseCamEl.classList.toggle("active", camActive);
  btnCam.classList.toggle("active", camActive);
  btnCam.querySelector(".btn-lbl").textContent = camActive ? "Camera on" : "Camera";
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
  updateFeedCards();

  senseScreenEl.classList.toggle("active", screenActive);
  btnScreen.classList.toggle("active", screenActive);
  btnScreen.querySelector(".btn-lbl").textContent = screenActive ? "Screen on" : "Screen";
  if (!screenActive) {
    screenImgEl.style.display = "none";
    screenPlaceholder.style.display = "block";
  }
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

// ---------- Microphone ----------

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
    addChatMessage("System", "Microphone access error: " + err.message, "system");
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
  addChatMessage("System", (approved ? "Approved" : "Denied") + " remote command.", "system");
  closeExecApproval(currentApprovalId);
}

document.getElementById("exec-approve").addEventListener("click", () => respondExecApproval(true));
document.getElementById("exec-deny").addEventListener("click", () => respondExecApproval(false));

// ---------- Controls ----------

btnMic.addEventListener("click", () => {
  isMicMuted = !isMicMuted;
  btnMic.classList.toggle("active", !isMicMuted);
  btnMic.querySelector(".btn-lbl").textContent = isMicMuted ? "Mic off" : "Mic on";
  senseMicEl.classList.toggle("active", !isMicMuted);
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

btnInterrupt.addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "interrupt" }));
    modelSpeaking = false;
    audioStateTagEl.textContent = "Interrupted";
    brandMarkEl.classList.remove("speaking");
  }
});

// ---------- Chat ----------

function addChatMessage(sender, text, style = "ultron") {
  const msgDiv = document.createElement("div");
  msgDiv.className = `msg-bubble ${style || (sender === "You" ? "user" : "ultron")}`;

  if (style === "system") {
    msgDiv.textContent = text;
  } else {
    msgDiv.textContent = text;
  }

  chatMessagesEl.appendChild(msgDiv);
  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
}

function escapeHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function sendUserText() {
  const text = chatInputEl.value.trim();
  if (!text) return;

  addChatMessage("You", text, "user");
  chatInputEl.value = "";

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "user_text", text: text }));
  }
}

sendBtnEl.addEventListener("click", sendUserText);
chatInputEl.addEventListener("keypress", (e) => {
  if (e.key === "Enter") sendUserText();
});

// ---------- Tabs ----------

function switchTab(tabName) {
  ["chat", "vis", "act"].forEach((t) => {
    document.getElementById(`tab-${t}-btn`).classList.toggle("active", tabName === t);
    document.getElementById(`tab-${t}`).classList.toggle("active", tabName === t);
  });

  if (tabName === "vis") visBadgeEl.classList.remove("active");
  if (tabName === "act") actBadgeEl.classList.remove("active");
  syncGesturesToWorkspace();
}

// Hands-on by default whenever the Spatial tab is showing a live scene;
// off (camera released) when leaving it or when the workspace empties.
function syncGesturesToWorkspace() {
  const g = window.UltronGestures;
  if (!g || !window.SVE) return;
  const spatialVisible = document.getElementById("tab-vis").classList.contains("active");
  if (spatialVisible && window.SVE.hasActiveScene()) {
    if (!g.running && !g.starting) g.start();
  } else if (g.running) {
    g.stop();
  }
}

// Watchdog: recovers from transient start failures (permission just granted,
// GPU delegate fallback, camera briefly busy) without user interaction.
setInterval(syncGesturesToWorkspace, 5000);

// ---------- SVE bridge ----------

// sve.js (module) calls this to report user interactions back to the engine.
window.sveSend = (obj) => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
};

// ---------- Tool Activity ----------

function addToolActivity(msg) {
  if (activityEmptyEl) activityEmptyEl.style.display = "none";

  if (msg.phase === "start") {
    const card = document.createElement("div");
    card.className = "activity-card";
    const time = new Date().toLocaleTimeString();
    card.innerHTML = `
      <div class="activity-head">
        <span class="activity-name">${escapeHtml(msg.name)}</span>
        <span class="activity-time">${time}</span>
        <span class="activity-state">running</span>
      </div>
      <div class="activity-args">${escapeHtml(msg.args_preview || "")}</div>
      <div class="activity-result"></div>`;
    activityFeedEl.appendChild(card);
    pendingActivities[msg.name] = card;
  } else if (msg.phase === "done") {
    const card = pendingActivities[msg.name];
    if (card) {
      card.classList.add("done");
      card.querySelector(".activity-state").textContent = "done";
      card.querySelector(".activity-result").textContent = msg.result_preview || "";
      delete pendingActivities[msg.name];
    }
  }
  activityFeedEl.scrollTop = activityFeedEl.scrollHeight;

  if (!document.getElementById("tab-act").classList.contains("active")) {
    actBadgeEl.classList.add("active");
  }
}

// ---------- Waveform visualizer ----------

function renderCanvasVisualizer() {
  const canvas = document.getElementById("visualizer-canvas");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  canvas.width = canvas.offsetWidth * dpr;
  canvas.height = canvas.offsetHeight * dpr;
  ctx.scale(dpr, dpr);

  const W = canvas.offsetWidth;
  const H = canvas.offsetHeight;
  const BARS = 36;
  const gap = 3;
  const barW = (W - gap * (BARS - 1)) / BARS;
  let phase = 0;

  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue("--accent").trim() || "#5b8def";
  const green = css.getPropertyValue("--green").trim() || "#4cc38a";
  const faint = css.getPropertyValue("--border").trim() || "#26292e";

  function draw() {
    requestAnimationFrame(draw);
    ctx.clearRect(0, 0, W, H);
    phase += 0.06;

    let levels = new Array(BARS).fill(0);
    if (modelSpeaking) {
      for (let i = 0; i < BARS; i++) {
        levels[i] = 0.35 + 0.55 * Math.abs(Math.sin(phase * 1.6 + i * 0.45)) * Math.abs(Math.sin(phase * 0.7 + i));
      }
    } else if (audioAnalyser && audioDataArray) {
      audioAnalyser.getByteFrequencyData(audioDataArray);
      for (let i = 0; i < BARS; i++) {
        levels[i] = (audioDataArray[i % audioDataArray.length] / 255) * 0.9;
      }
    }

    const color = modelSpeaking ? green : accent;
    for (let i = 0; i < BARS; i++) {
      const h = Math.max(3, levels[i] * (H - 10));
      const x = i * (barW + gap);
      const y = (H - h) / 2;
      ctx.fillStyle = levels[i] > 0.02 ? color : faint;
      ctx.beginPath();
      ctx.roundRect(x, y, barW, h, barW / 2);
      ctx.fill();
    }
  }

  draw();
}

window.addEventListener("DOMContentLoaded", () => {
  renderCanvasVisualizer();
  initWebSocket();
});
