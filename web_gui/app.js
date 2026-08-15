import * as THREE from 'three';

/**
 * Ultron — frontend engine.
 * Refactored for widget-based spatial layout.
 * WebSocket bridge to the Python backend, media streaming, and 3D orb.
 */

let ws = null;
let audioCtx = null;
let micStream = null;
let scriptNode = null;
let isMicMuted = true; // Default to muted until wake
let isCamActive = false;
let isScreenActive = false;

let audioAnalyser = null;
let audioDataArray = null;
let modelSpeaking = false;

// New Dock DOM
const btnMic = document.getElementById("btn-mic");
const btnCam = document.getElementById("btn-cam");
const btnScreen = document.getElementById("btn-screen");
const btnFullscreen = document.getElementById("btn-fullscreen");

const camImgEl = document.getElementById("webcam-img");
const screenImgEl = document.getElementById("screen-img");

// Remote = served through Tailscale (HTTPS, non-localhost).
const IS_REMOTE = !["127.0.0.1", "localhost", ""].includes(window.location.hostname);

// ---------- WebSocket ----------

function initWebSocket() {
  const wsUrl = window.location.protocol === "https:"
    ? `wss://${window.location.host}/ws`
    : `ws://${window.location.hostname || "127.0.0.1"}:8765`;
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log("[WS] Connected");
    ws.send(JSON.stringify({ type: "client_hello", remote: IS_REMOTE }));
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      
      // Forward widget payloads to GUIEngine
      if (msg.action && window.GUIEngine && window.GUIEngine.handleEvent(msg)) {
        return;
      }
      
      handleServerMessage(msg);
    } catch (e) {
      console.error("WS parse error:", e);
    }
  };

  ws.onclose = () => {
    console.log("[WS] Disconnected");
    setTimeout(initWebSocket, 3000);
  };
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case "audio_out":
      modelSpeaking = true;
      if (IS_REMOTE && msg.pcm_base64) playPcmChunk(msg.pcm_base64);
      break;

    case "exec_approval_request":
      showExecApproval(msg);
      break;

    case "exec_approval_closed":
      closeExecApproval(msg.id);
      break;

    case "sve_workspace":
    case "sve_scene_create":
    case "sve_scene_update":
    case "sve_scene_delete": {
      if (window.SVE) window.SVE.handleEvent(msg);
      syncGesturesToWorkspace();
      break;
    }

    case "camera_frame":
      if (msg.image_base64 && !UltronCamera.active()) {
        camImgEl.src = "data:image/jpeg;base64," + msg.image_base64;
      }
      break;

    case "screen_frame":
      if (msg.image_base64) {
        screenImgEl.src = "data:image/jpeg;base64," + msg.image_base64;
      }
      break;

    case "sense_update":
      updateSenseBadges(msg.camera_active, msg.screen_active);
      break;

    case "interrupted":
      modelSpeaking = false;
      flushPlayback();
      break;
  }
}

// Shared webcam stream
let remoteCamFacing = "environment"; 

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
let previewOn = false;

window.addEventListener("ultron-gesture-state", (e) => {
  if (e.detail.running) {
    startLocalPreview();
  } else if (!isCamActive) {
    stopLocalPreview();
  }
});

async function startLocalPreview() {
  if (previewOn) return;
  try {
    camVideoEl.srcObject = await UltronCamera.acquire();
    previewOn = true;
  } catch (e) {
    console.warn("Local camera preview failed", e);
  }
}

function stopLocalPreview() {
  if (!previewOn) return;
  previewOn = false;
  camVideoEl.srcObject = null;
  UltronCamera.release();
}

function updateSenseBadges(camActive, screenActive) {
  isCamActive = camActive;
  isScreenActive = screenActive;

  btnCam.classList.toggle("active", camActive);
  if (camActive) {
    startLocalPreview();
    if (IS_REMOTE) startRemoteCamPush();
  } else {
    stopRemoteCamPush();
    if (!window.UltronGestures?.running) {
      stopLocalPreview();
    }
  }

  btnScreen.classList.toggle("active", screenActive);
}

// Remote Cam Push
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

// ---------- Microphone ----------

async function initMicrophone() {
  if (micStream) return;
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
    console.error("Microphone access error: " + err.message);
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

// ---------- Remote audio playback ----------
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
  closeExecApproval(currentApprovalId);
}

document.getElementById("exec-approve").addEventListener("click", () => respondExecApproval(true));
document.getElementById("exec-deny").addEventListener("click", () => respondExecApproval(false));

// ---------- Dock Controls ----------

btnMic.addEventListener("click", () => {
  isMicMuted = !isMicMuted;
  btnMic.classList.toggle("active", !isMicMuted);
  if (!isMicMuted) initMicrophone(); // Make sure mic is init
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

btnFullscreen.addEventListener("click", () => {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch(err => console.log(err));
  } else {
    document.exitFullscreen();
  }
});

// ---------- SVE bridge ----------
window.sveSend = (obj) => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
};
function syncGesturesToWorkspace() {
  const g = window.UltronGestures;
  if (!g || !window.SVE) return;
  if (window.SVE.hasActiveScene()) {
    if (!g.running && !g.starting) g.start();
  } else if (g.running) {
    g.stop();
  }
}
setInterval(syncGesturesToWorkspace, 5000);

// ---------- 3D Orb Visualizer ----------

function renderOrbVisualizer() {
  const container = document.getElementById("hero-stage");
  const canvas = document.getElementById("visualizer-canvas");
  
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
  camera.position.z = 10;
  
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  
  // Responsive sizing
  const resize = () => {
    const width = container.clientWidth;
    const height = container.clientHeight;
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  };
  window.addEventListener('resize', resize);
  resize();

  // Create the Brain Core Orb (Wireframe Icosahedron)
  const geometry = new THREE.IcosahedronGeometry(3, 2);
  const material = new THREE.MeshBasicMaterial({ 
    color: 0x00f3ff, 
    wireframe: true, 
    transparent: true,
    opacity: 0.6
  });
  const orb = new THREE.Mesh(geometry, material);
  scene.add(orb);

  // Animation Loop
  let phase = 0;
  function animate() {
    requestAnimationFrame(animate);
    
    // Default slow rotation
    let rotSpeed = 0.002;
    let scale = 1.0;

    // Active state modifiers
    if (modelSpeaking) {
      rotSpeed = 0.02;
      scale = 1.1 + Math.sin(phase * 0.5) * 0.1;
    } else if (!isMicMuted && audioAnalyser && audioDataArray) {
      audioAnalyser.getByteFrequencyData(audioDataArray);
      let avg = 0;
      for (let i = 0; i < audioDataArray.length; i++) avg += audioDataArray[i];
      avg = avg / audioDataArray.length;
      
      if (avg > 10) {
        rotSpeed = 0.01 + (avg / 255) * 0.05;
        scale = 1.0 + (avg / 255) * 0.3;
      }
    }

    orb.rotation.x += rotSpeed;
    orb.rotation.y += rotSpeed;
    
    // Smooth scaling
    orb.scale.lerp(new THREE.Vector3(scale, scale, scale), 0.1);

    phase += 0.1;
    renderer.render(scene, camera);
  }
  animate();
}

// ---------- Engine Initialization ----------

window.addEventListener("DOMContentLoaded", () => {
  // Setup engines
  if (window.GUIEngine) window.GUIEngine.init();
  
  renderOrbVisualizer();
  initWebSocket();

  // First interaction listener to initialize wake engine
  const onFirstInteraction = () => {
    document.removeEventListener('pointerdown', onFirstInteraction);
    document.removeEventListener('keydown', onFirstInteraction);
    
    // Fix AudioContext autoplay issue
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    if (playbackCtx && playbackCtx.state === "suspended") playbackCtx.resume();
    
    if (window.WakeEngine) {
      window.WakeEngine.init(() => {
        // On Wake:
        console.log("WakeEngine triggered wake!");
        isMicMuted = false;
        btnMic.classList.add("active");
        initMicrophone();
      });
    }
  };

  document.addEventListener('pointerdown', onFirstInteraction);
  document.addEventListener('keydown', onFirstInteraction);
});
