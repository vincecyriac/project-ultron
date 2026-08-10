/**
 * Ultron — frontend orchestrator.
 *
 * Owns the wake state machine, the WebSocket bridge to the Python hub, mic
 * capture, live feeds, remote voice playback, and the routing of everything the
 * hub emits into the generative widget grid.
 *
 * Two rules shape this file:
 *   1. Ultron boots DORMANT. No audio leaves the browser and the hub holds no
 *      Gemini Live socket until a wake trigger fires (double clap, "Ultron",
 *      the dock mic, or typing a message).
 *   2. Nothing in the UI is labelled. State is expressed by the Brain Core's
 *      colour and motion (core.js); content is expressed as widgets
 *      (widgets.js). This file never writes status text.
 */

// ---------- state ----------

const State = {
  awake: false,
  phase: "dormant",        // dormant | waking | listening | thinking | speaking | error
  micMuted: false,
  camera: false,
  screen: false,
};

const IDLE_SLEEP_MS = 180000;   // no voice, text or tool work for 3 min -> dormant
const TRANSCRIPT_TTL_MS = 60000;
const ACTIVITY_TTL_MS = 30000;
const MAX_TRANSCRIPT = 14;
const MAX_ACTIVITY = 10;

let ws = null;
let audioCtx = null;
let micStream = null;
let scriptNode = null;
let audioAnalyser = null;
let idleTimer = null;

// Remote = served through Tailscale (HTTPS, non-localhost). Remote clients play
// Ultron's voice in the browser; local ones rely on the Mac's speakers.
const IS_REMOTE = !["127.0.0.1", "localhost", ""].includes(window.location.hostname);

// ---------- DOM ----------

const stageEl = document.getElementById("stage");
const coreMountEl = document.getElementById("core-mount");
const gridEl = document.getElementById("widget-grid");

const btnMic = document.getElementById("btn-mic");
const btnCam = document.getElementById("btn-cam");
const btnScreen = document.getElementById("btn-screen");

const commandEl = document.getElementById("command");
const chatInputEl = document.getElementById("chat-input");
const modelSelectEl = document.getElementById("model-select");

const camVideoEl = document.getElementById("webcam-video");
const camImgEl = document.getElementById("webcam-img");
const screenImgEl = document.getElementById("screen-img");

const transcript = [];
const activity = [];
let transcriptTimer = null;
let activityTimer = null;
const pendingActivities = {};

// ---------- phase / core ----------

function setPhase(phase) {
  State.phase = phase;
  UltronCore.setState(phase);
  document.body.dataset.phase = phase;
}

function bumpIdle() {
  if (idleTimer) clearTimeout(idleTimer);
  if (!State.awake) return;
  idleTimer = setTimeout(() => goDormant("idle"), IDLE_SLEEP_MS);
}

// ---------- wake state machine ----------

function sendWakeState(awake, reason) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "set_wake_state", awake, reason }));
  }
}

function wake(reason) {
  if (State.awake) return;
  State.awake = true;
  State.micMuted = false;
  unlockAudio();
  UltronWake.setListening(false);
  UltronWake.chimeWake();
  UltronCore.pulse(3);
  setPhase("waking");
  syncDock();
  sendWakeState(true, reason || "manual");
  bumpIdle();
  // If the hub never confirms (offline), fall back to listening visuals anyway.
  setTimeout(() => { if (State.awake && State.phase === "waking") setPhase("listening"); }, 2500);
}

function goDormant(reason) {
  if (!State.awake) return;
  State.awake = false;
  sendWakeState(false, reason || "manual");
  if (State.camera) requestCamera(false);
  if (State.screen) requestScreen(false);
  UltronWidgets.clear();
  UltronWidgets.hideSystem("spatial");
  flushPlayback();
  closeCommand();
  UltronWake.chimeSleep();
  setPhase("dormant");
  UltronWake.setListening(true);
  syncDock();
  if (idleTimer) clearTimeout(idleTimer);
}

function toggleMute() {
  State.micMuted = !State.micMuted;
  syncDock();
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
    ws.send(JSON.stringify({ type: "client_hello", remote: IS_REMOTE, awake: State.awake }));
    sendWakeState(State.awake, "reconnect");
    if (!micStream) initMicrophone();
    setPhase(State.awake ? "listening" : "dormant");
  };

  ws.onmessage = (event) => {
    try {
      handleServerMessage(JSON.parse(event.data));
    } catch (e) {
      console.error("WS parse error:", e);
    }
  };

  ws.onclose = () => {
    setPhase("error");
    setTimeout(initWebSocket, 3000);
  };
}

function handleServerMessage(msg) {
  // Generative UI arrives as an action verb rather than a transport type.
  if (msg.action === "RENDER_WIDGET") {
    UltronWidgets.render(msg);
    UltronCore.pulse();
    bumpIdle();
    return;
  }
  if (msg.action === "CLEAR_WIDGET") {
    if (msg.widget_id) UltronWidgets.remove(msg.widget_id);
    else UltronWidgets.clear();
    return;
  }

  switch (msg.type) {
    case "wake_state":
      if (msg.awake && !State.awake) {
        // Woken by the hub (voice command, another device): adopt it silently.
        State.awake = true;
        UltronWake.setListening(false);
        setPhase("listening");
        syncDock();
        bumpIdle();
      } else if (msg.awake) {
        setPhase("listening");
      } else if (!msg.awake && State.awake) {
        goDormant(msg.reason || "hub");
      } else {
        setPhase("dormant");
      }
      break;

    case "status":
      applyStatus(msg.status);
      break;

    case "audio_out":
      if (!State.awake) break;
      setPhase("speaking");
      if (msg.pcm_base64) {
        UltronCore.setLevel(pcmLevel(msg.pcm_base64));
        if (IS_REMOTE) playPcmChunk(msg.pcm_base64);
      }
      bumpIdle();
      break;

    case "interrupted":
      flushPlayback();
      if (State.awake) setPhase("listening");
      UltronCore.pulse();
      break;

    case "exec_approval_request":
      showExecApproval(msg);
      break;

    case "exec_approval_closed":
      closeExecApproval(msg.id);
      break;

    case "chat_log":
      pushMessage(msg.sender, msg.text, msg.style);
      break;

    case "sve_workspace":
    case "sve_scene_create":
    case "sve_scene_update":
    case "sve_scene_delete": {
      window.SVE && window.SVE.handleEvent(msg);
      syncSpatialWidget();
      syncGesturesToWorkspace();
      break;
    }

    case "camera_frame":
      // Backend JPEG frames feed Gemini; preview uses the local 30fps
      // getUserMedia stream instead. Fall back to JPEGs only if that failed.
      if (msg.image_base64 && !UltronCamera.active()) {
        camImgEl.src = "data:image/jpeg;base64," + msg.image_base64;
        camImgEl.hidden = false;
        camVideoEl.hidden = true;
      }
      break;

    case "screen_frame":
      if (msg.image_base64) screenImgEl.src = "data:image/jpeg;base64," + msg.image_base64;
      break;

    case "sense_update":
      applySenseState(msg.camera_active, msg.screen_active);
      break;

    case "tool_activity":
      pushActivity(msg);
      break;

    case "model_list":
      populateModelList(msg.models, msg.active_id);
      break;

    case "model_changed":
      if (modelSelectEl.value !== msg.id) modelSelectEl.value = msg.id;
      break;
  }
}

/** Hub status strings drive colour only — no text is ever rendered. */
function applyStatus(status) {
  const s = (status || "").toLowerCase();
  if (!State.awake) {
    if (s.includes("error") || s.includes("failed")) setPhase("error");
    return;
  }
  if (s.includes("speaking")) setPhase("speaking");
  else if (s.includes("executing") || s.includes("thinking") || s.includes("connecting")) setPhase("thinking");
  else if (s.includes("error") || s.includes("failed") || s.includes("disconnected")) setPhase("error");
  else if (s.includes("listening") || s.includes("interrupted")) setPhase("listening");
  else if (s.includes("dormant")) setPhase("dormant");
}

// ---------- stage layout: hero <-> docked ----------

function syncStage() {
  const populated = UltronWidgets.visibleCount() > 0;
  stageEl.classList.toggle("hero", !populated);
  stageEl.classList.toggle("docked", populated);
  UltronCore.setDocked(populated);
  // grid columns change with the stage; charts need a repaint once settled
  setTimeout(() => UltronWidgets.repaintCharts(), 720);
}

window.addEventListener("ultron-widgets-changed", syncStage);

// ---------- transcript / activity widgets ----------

function renderTranscript() {
  UltronWidgets.render({
    widget_id: "sys_transcript",
    layout: { col_span: 5, row_span: 2 },
    components: [{ type: "Transcript", messages: transcript }],
  });
  if (transcriptTimer) clearTimeout(transcriptTimer);
  transcriptTimer = setTimeout(() => UltronWidgets.remove("sys_transcript"), TRANSCRIPT_TTL_MS);
}

function pushMessage(sender, text, style) {
  if (!text) return;
  if (style === "system") {
    // Hub log lines are machine chatter: they belong in the activity stream.
    pushActivity({ phase: "log", name: text.slice(0, 160) });
    return;
  }
  transcript.push({ role: sender === "You" ? "user" : "ultron", text });
  while (transcript.length > MAX_TRANSCRIPT) transcript.shift();
  renderTranscript();
  bumpIdle();
}

function renderActivity() {
  UltronWidgets.render({
    widget_id: "sys_activity",
    layout: { col_span: 4, row_span: 1 },
    components: [{ type: "ActivityStream", items: activity }],
  });
  if (activityTimer) clearTimeout(activityTimer);
  activityTimer = setTimeout(() => UltronWidgets.remove("sys_activity"), ACTIVITY_TTL_MS);
}

function pushActivity(msg) {
  if (msg.phase === "start") {
    const item = { name: msg.name, detail: msg.args_preview || "", state: "running" };
    activity.push(item);
    pendingActivities[msg.name] = item;
    setPhase(State.awake ? "thinking" : State.phase);
  } else if (msg.phase === "done") {
    const item = pendingActivities[msg.name];
    if (item) {
      item.state = "done";
      item.detail = msg.result_preview || item.detail;
      delete pendingActivities[msg.name];
    }
    // Last tool finished: colour falls back to listening until voice resumes.
    if (State.awake && State.phase === "thinking" && !Object.keys(pendingActivities).length) {
      setPhase("listening");
    }
  } else {
    activity.push({ name: msg.name, state: "log" });
  }
  while (activity.length > MAX_ACTIVITY) activity.shift();
  renderActivity();
  bumpIdle();
}

// ---------- spatial widget ----------

function syncSpatialWidget() {
  if (window.SVE && window.SVE.hasActiveScene()) {
    UltronWidgets.showSystem("spatial", { col_span: 7, row_span: 2 });
    // The renderer initialised while this widget was display:none (0x0), so
    // its first resize() was a no-op. Nudge it once the grid track settles.
    setTimeout(() => window.SVE.forceResize(), 80);
    setTimeout(() => window.SVE.forceResize(), 480);
  } else {
    UltronWidgets.hideSystem("spatial");
  }
}

// ---------- shared webcam stream ----------
// Refcounted: live preview + gesture tracker share one getUserMedia stream
// (30fps) instead of the backend's 0.8s JPEG feed.

let remoteCamFacing = "environment"; // phone default: rear camera (surroundings)

const UltronCamera = {
  stream: null,
  refs: 0,
  async acquire() {
    this.refs++;
    if (!this.stream) {
      this.stream = await navigator.mediaDevices.getUserMedia({ video: camConstraints() });
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
    this.stream = await navigator.mediaDevices.getUserMedia({ video: camConstraints() });
    return this.stream;
  },
  active() {
    return !!this.stream;
  },
};
window.UltronCamera = UltronCamera;

function camConstraints() {
  const video = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };
  if (IS_REMOTE) video.facingMode = remoteCamFacing;
  return video;
}

let previewOn = false;

async function startLocalPreview() {
  if (previewOn) return;
  try {
    camVideoEl.srcObject = await UltronCamera.acquire();
    previewOn = true;
    camVideoEl.hidden = false;
    camImgEl.hidden = true;
  } catch (e) {
    console.warn("Local camera preview failed, falling back to backend frames:", e);
  }
}

function stopLocalPreview() {
  if (!previewOn) return;
  previewOn = false;
  camVideoEl.srcObject = null;
  camVideoEl.hidden = true;
  UltronCamera.release();
}

function syncCameraWidget() {
  const live = previewOn || State.camera || !!window.UltronGestures?.running;
  if (live) UltronWidgets.showSystem("camera", { col_span: 4, row_span: 1 });
  else UltronWidgets.hideSystem("camera");
}

window.addEventListener("ultron-gesture-state", (e) => {
  if (e.detail.running) startLocalPreview();
  else if (!State.camera) stopLocalPreview();
  syncCameraWidget();
});

function applySenseState(camActive, screenActive) {
  State.camera = !!camActive;
  State.screen = !!screenActive;

  if (State.camera) {
    startLocalPreview();
    if (IS_REMOTE) startRemoteCamPush();
  } else {
    stopRemoteCamPush();
    if (!window.UltronGestures?.running) {
      stopLocalPreview();
      camImgEl.hidden = true;
    }
  }
  syncCameraWidget();

  if (State.screen) UltronWidgets.showSystem("screen", { col_span: 5, row_span: 1 });
  else UltronWidgets.hideSystem("screen");

  syncDock();
}

// ---------- phone camera push (remote sessions) ----------
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
  if (!ws || ws.readyState !== WebSocket.OPEN || !State.awake) return;
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
if (IS_REMOTE) {
  camFlipBtn.hidden = false;
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

// ---------- microphone ----------

async function initMicrophone() {
  if (micStream) return;
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    // Browsers spawn AudioContexts suspended until a user gesture; try to
    // resume right away (harmless no-op if it's blocked — the broadened
    // unlockAudio() listeners below catch the very first interaction instead,
    // so wake detection starts as soon as physically possible).
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});

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
    audioAnalyser.fftSize = 128;
    audioAnalyser.smoothingTimeConstant = 0.6;
    sourceNode.connect(audioAnalyser);
    UltronCore.setAnalyser(audioAnalyser);

    // Passive wake listening shares this graph; nothing is transmitted.
    UltronWake.attach({ audioCtx, sourceNode });
    UltronWake.handler = (reason) => wake(reason);
    UltronWake.setListening(!State.awake);

    scriptNode = audioCtx.createScriptProcessor(2048, 1, 1);
    sourceNode.connect(scriptNode);
    scriptNode.connect(audioCtx.destination);

    scriptNode.onaudioprocess = (e) => {
      // Dormant or muted: the mic feeds the local wake detector only.
      if (!State.awake || State.micMuted) return;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;

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
    console.warn("Microphone unavailable:", err);
    setPhase("error");
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

// ---------- remote voice playback (phone / Tailscale clients) ----------
// The hub broadcasts Ultron's voice as 24kHz PCM16; remote devices have no path
// to the Mac's speakers, so schedule the chunks through Web Audio here.

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

function decodePcm(b64) {
  const raw = window.atob(b64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

/** RMS of a voice chunk, so the core reacts even when the Mac does the playing. */
function pcmLevel(b64) {
  try {
    const pcm = decodePcm(b64);
    let sum = 0;
    let n = 0;
    for (let i = 0; i < pcm.length; i += 8) {
      const v = pcm[i] / 32768;
      sum += v * v;
      n++;
    }
    return n ? Math.min(1, Math.sqrt(sum / n) * 3.2) : 0;
  } catch (_) {
    return 0;
  }
}

function playPcmChunk(b64) {
  try {
    const ctx = ensurePlaybackCtx();
    const pcm = decodePcm(b64);
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

// Browsers (and the desktop app's WebKit shell) keep AudioContexts suspended
// until a user gesture — while suspended, the analyser never sees live
// samples, so passive clap/wake-word detection is silently dead. Catch the
// very first interaction, of ANY kind, as early as possible to resume it.
function unlockAudio() {
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  if (playbackCtx && playbackCtx.state === "suspended") playbackCtx.resume();
  if (!micStream) initMicrophone();
}
["pointerdown", "touchstart", "keydown", "wheel"].forEach((evt) =>
  document.addEventListener(evt, unlockAudio, { passive: true })
);
// Also retry on a slow timer: some WebKit builds allow resume() once the tab
// has been in focus for a moment, without needing a gesture at all.
const audioUnlockRetry = setInterval(() => {
  if (audioCtx && audioCtx.state === "running") { clearInterval(audioUnlockRetry); return; }
  if (audioCtx) audioCtx.resume().catch(() => {});
}, 1000);

// ---------- remote exec approval ----------

const approvalOverlayEl = document.getElementById("exec-approval");
const approvalCmdEl = document.getElementById("exec-approval-cmd");
const approvalToolEl = document.getElementById("exec-approval-tool");
let currentApprovalId = null;

function showExecApproval(msg) {
  currentApprovalId = msg.id;
  approvalToolEl.textContent = msg.tool === "execute_applescript_task" ? "AppleScript" : "Shell command";
  approvalCmdEl.textContent = msg.preview || "(empty)";
  approvalOverlayEl.hidden = false;
  UltronWake.chimeAlert();
  if (navigator.vibrate) navigator.vibrate(120);
}

function closeExecApproval(id) {
  if (id && id !== currentApprovalId) return;
  currentApprovalId = null;
  approvalOverlayEl.hidden = true;
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
  closeExecApproval(currentApprovalId);
}

document.getElementById("exec-approve").addEventListener("click", () => respondExecApproval(true));
document.getElementById("exec-deny").addEventListener("click", () => respondExecApproval(false));

// ---------- dock ----------

function syncDock() {
  btnMic.classList.toggle("on", State.awake && !State.micMuted);
  btnMic.classList.toggle("muted", State.awake && State.micMuted);
  btnCam.classList.toggle("on", State.camera);
  btnScreen.classList.toggle("on", State.screen);
}

function requestCamera(active) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "toggle_camera", active }));
  }
}

function requestScreen(active) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "toggle_screen", active }));
  }
}

// Tap: wake, or mute/unmute once awake. Hold: back to dormant.
let micHoldTimer = null;
let micHeld = false;

btnMic.addEventListener("pointerdown", () => {
  micHeld = false;
  micHoldTimer = setTimeout(() => {
    micHeld = true;
    btnMic.classList.remove("holding");
    goDormant("hold");
  }, 600);
  if (State.awake) btnMic.classList.add("holding");
});

function endMicHold() {
  if (micHoldTimer) clearTimeout(micHoldTimer);
  btnMic.classList.remove("holding");
}

btnMic.addEventListener("pointerup", () => {
  endMicHold();
  if (micHeld) return;
  if (!State.awake) wake("dock");
  else toggleMute();
});
btnMic.addEventListener("pointerleave", endMicHold);
btnMic.addEventListener("pointercancel", endMicHold);

btnCam.addEventListener("click", () => {
  if (!State.awake) wake("camera");
  requestCamera(!State.camera);
});

btnScreen.addEventListener("click", () => {
  if (!State.awake) wake("screen");
  requestScreen(!State.screen);
});

document.getElementById("btn-cam-close").addEventListener("click", () => requestCamera(false));
document.getElementById("btn-screen-close").addEventListener("click", () => requestScreen(false));
document.getElementById("btn-spatial-close").addEventListener("click", () => UltronWidgets.hideSystem("spatial"));
document.getElementById("btn-sve-reset").addEventListener("click", () => window.SVE && window.SVE.resetCamera());

// ---------- ephemeral command line ----------

function openCommand(seed) {
  commandEl.hidden = false;
  if (seed) chatInputEl.value = seed;
  chatInputEl.focus();
}

function closeCommand() {
  chatInputEl.value = "";
  commandEl.hidden = true;
  chatInputEl.blur();
}

function sendUserText() {
  const text = chatInputEl.value.trim();
  if (!text) {
    closeCommand();
    return;
  }
  if (!State.awake) wake("text");
  pushMessage("You", text, "user");
  chatInputEl.value = "";
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "user_text", text }));
  }
}

chatInputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendUserText();
  if (e.key === "Escape") closeCommand();
});
chatInputEl.addEventListener("blur", () => {
  if (!chatInputEl.value.trim()) closeCommand();
});

function populateModelList(models, activeId) {
  modelSelectEl.replaceChildren();
  (models || []).forEach((m) => {
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

// ---------- global input ----------

coreMountEl.addEventListener("click", () => {
  if (!State.awake) wake("core");
  else if (State.phase === "speaking") interrupt();
  else openCommand();
});

function interrupt() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "interrupt" }));
  flushPlayback();
  if (State.awake) setPhase("listening");
}

window.addEventListener("keydown", (e) => {
  const typing = document.activeElement &&
    ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);

  if (e.key === "Escape") {
    if (!approvalOverlayEl.hidden) return;          // approval is deliberate: no escape hatch
    if (!commandEl.hidden) closeCommand();
    else if (State.phase === "speaking") interrupt();
    else if (UltronWidgets.generatedCount() > 0 || UltronWidgets.systemVisible("spatial")) {
      UltronWidgets.clear();
      UltronWidgets.hideSystem("spatial");
    }
    else if (State.awake) goDormant("escape");      // sleeps + stops live feeds
    return;
  }

  if (typing) return;

  if (e.key === "/") {
    e.preventDefault();
    openCommand();
  } else if (e.key === " ") {
    e.preventDefault();
    if (!State.awake) wake("space");
    else toggleMute();
  } else if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
    openCommand(e.key);
    e.preventDefault();
  }
});

// ---------- SVE bridge ----------

// sve.js (module) calls this to report user interactions back to the engine.
window.sveSend = (obj) => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
};

// Hands-on by default whenever the spatial widget is showing a live scene; off
// (camera released) when it closes or the workspace empties.
function syncGesturesToWorkspace() {
  const g = window.UltronGestures;
  if (!g || !window.SVE) return;
  const visible = UltronWidgets.systemVisible("spatial");
  if (visible && window.SVE.hasActiveScene()) {
    if (!g.running && !g.starting) g.start();
  } else if (g.running) {
    g.stop();
  }
}
window.syncGesturesToWorkspace = syncGesturesToWorkspace;

// Watchdog: recovers from transient start failures (permission just granted,
// GPU delegate fallback, camera briefly busy) without user interaction.
setInterval(syncGesturesToWorkspace, 5000);

// ---------- boot ----------

window.addEventListener("DOMContentLoaded", () => {
  UltronCore.init(document.getElementById("core-canvas"));
  UltronWidgets.init(gridEl);
  setPhase("dormant");
  syncDock();
  syncStage();
  initWebSocket();
  initMicrophone();
});
