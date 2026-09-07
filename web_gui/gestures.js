/**
 * Hand-gesture input source for the Spatial Visualization Engine.
 *
 * MediaPipe HandLandmarker (fully local, vendored wasm + model) reads the
 * shared webcam stream and drives the active 3D scene:
 *
 *   Point (index finger)            → cursor + hover; stable point reports
 *                                     the object to the AI ("what is this?")
 *   Pinch (thumb+index) on object   → grab & move it
 *   Pinch on empty space            → orbit the camera
 *   Open palm move                  → orbit the camera
 *   TWO hands pinching              → zoom (spread = in, squeeze = out)
 *
 * The tracked feed shows in the sidebar Camera card with landmark skeleton
 * markings drawn on an overlay canvas.
 */

import { FilesetResolver, HandLandmarker } from "./vendor/tasks_vision.mjs";

const hudEl = document.getElementById("gesture-hud");
const cursorEl = document.getElementById("gesture-cursor");
const btn = document.getElementById("btn-hands");
const overlayEl = document.getElementById("cam-overlay");

let landmarker = null;
let landmarkerDelegate = "GPU";
let video = null;          // hidden internal video consuming the shared stream
let running = false;
let starting = false;      // guards against concurrent auto-start calls
let lastVideoTime = -1;

// Smoothed cursor + gesture state
let smoothing = null;
let grabbing = false;
let orbiting = false;
let lastOrbit = null;
let lastPinchSpan = null;

const SMOOTH = 0.35;

// MediaPipe hand skeleton connections (landmark index pairs)
const HAND_CONNECTIONS = [
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [5, 9], [9, 10], [10, 11], [11, 12],
  [9, 13], [13, 14], [14, 15], [15, 16],
  [13, 17], [17, 18], [18, 19], [19, 20],
  [0, 17],
];

function lerp(a, b, t) { return a + (b - a) * t; }
function dist2d(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }

// ---------- Stable-point reporting ----------

let hoverId = null;
let hoverSince = 0;
let reportedId = null;

function reportPointedObject(hit) {
  const id = hit ? hit.userData.spec.id : null;
  const now = performance.now();
  if (id !== hoverId) {
    hoverId = id;
    hoverSince = now;
    return;
  }
  if (id && id !== reportedId && now - hoverSince > 600) {
    reportedId = id;
    // Only an SVE scene has a workspace to report against — a focused asset
    // card is a single mesh the hub tracks nothing about.
    const sve = window.SVE;
    if (T() !== sve || !sve?.state) return;
    const entry = sve.state.workspace[sve.state.activeSceneId];
    window.sveSend?.({
      type: "sve_user_action",
      scene_id: entry?.spec.id,
      action: "point_at",
      object_id: id,
    });
  }
  if (!id) reportedId = null;
}

// ---------- Gesture classification ----------

function handScale(lm) {
  return dist2d(lm[0], lm[9]) || 0.1;
}

function isPinch(lm) {
  return dist2d(lm[4], lm[8]) < handScale(lm) * 0.45;
}

function fingerExtended(lm, tip, pip) {
  return dist2d(lm[0], lm[tip]) > dist2d(lm[0], lm[pip]) * 1.15;
}

function isOpenPalm(lm) {
  return (
    fingerExtended(lm, 8, 6) &&
    fingerExtended(lm, 12, 10) &&
    fingerExtended(lm, 16, 14) &&
    fingerExtended(lm, 20, 18) &&
    !isPinch(lm)
  );
}

function isPointing(lm) {
  return (
    fingerExtended(lm, 8, 6) &&
    !fingerExtended(lm, 12, 10) &&
    !fingerExtended(lm, 16, 14) &&
    !isPinch(lm)
  );
}

// ---------- Landmark markings on the Camera card ----------

function drawMarkings(hands) {
  if (!overlayEl || overlayEl.style.display === "none") return;
  const cw = overlayEl.clientWidth, ch = overlayEl.clientHeight;
  if (!cw || !ch || !video || !video.videoWidth) return;
  if (overlayEl.width !== cw || overlayEl.height !== ch) {
    overlayEl.width = cw;
    overlayEl.height = ch;
  }
  const ctx = overlayEl.getContext("2d");
  ctx.clearRect(0, 0, cw, ch);
  if (!hands.length) return;

  // The preview video uses object-fit: cover — replicate its crop math
  const vw = video.videoWidth, vh = video.videoHeight;
  const scale = Math.max(cw / vw, ch / vh);
  const offX = (vw * scale - cw) / 2;
  const offY = (vh * scale - ch) / 2;
  const px = (l) => l.x * vw * scale - offX;
  const py = (l) => l.y * vh * scale - offY;

  hands.forEach((lm, hi) => {
    const color = hi === 0 ? "#5b8def" : "#4cc38a";
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.globalAlpha = 0.9;
    HAND_CONNECTIONS.forEach(([a, b]) => {
      ctx.beginPath();
      ctx.moveTo(px(lm[a]), py(lm[a]));
      ctx.lineTo(px(lm[b]), py(lm[b]));
      ctx.stroke();
    });
    lm.forEach((l, i) => {
      ctx.beginPath();
      ctx.arc(px(l), py(l), i === 4 || i === 8 ? 4.5 : 2.5, 0, Math.PI * 2);
      ctx.fillStyle = (i === 4 || i === 8) ? "#e5b567" : color;
      ctx.fill();
    });
    // Pinch indicator: line between thumb and index tips
    if (isPinch(lm)) {
      ctx.strokeStyle = "#e5b567";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(px(lm[4]), py(lm[4]));
      ctx.lineTo(px(lm[8]), py(lm[8]));
      ctx.stroke();
    }
  });
  ctx.globalAlpha = 1;
}

function clearMarkings() {
  if (overlayEl) {
    const ctx = overlayEl.getContext("2d");
    ctx.clearRect(0, 0, overlayEl.width, overlayEl.height);
    overlayEl.style.display = "none";
  }
}

// ---------- Engine ----------

async function ensureLandmarker() {
  if (landmarker) return landmarker;
  const fileset = await FilesetResolver.forVisionTasks("./vendor/wasm");
  try {
    landmarker = await HandLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: "./vendor/hand_landmarker.task", delegate: landmarkerDelegate },
      runningMode: "VIDEO",
      numHands: 2,
    });
  } catch (e) {
    if (landmarkerDelegate === "GPU") {
      // Some WKWebView/GPU combos reject the GPU delegate — retry on CPU
      console.warn("HandLandmarker GPU delegate failed, retrying with CPU:", e);
      landmarkerDelegate = "CPU";
      landmarker = await HandLandmarker.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: "./vendor/hand_landmarker.task", delegate: "CPU" },
        runningMode: "VIDEO",
        numHands: 2,
      });
    } else {
      throw e;
    }
  }
  return landmarker;
}

// The HUD is a single auto-fading glass pill. It carries the "show a hand"
// helper until MediaPipe recognises one, then only ever names the object the
// user is pointing at or dragging — no running status commentary.
let hintDismissed = false;

function setHud(text, active = true) {
  if (!hudEl) return;
  if (active && text) hudEl.textContent = text;
  hudEl.classList.toggle("show", !!(active && text));
}

function showHelperHint() {
  if (!hintDismissed) setHud("Show a hand to the camera");
}

function dismissHelperHint() {
  if (hintDismissed) return;
  hintDismissed = true;
  setHud("", false);
}

function updateCursor(ndcX, ndcY, mode) {
  // The focused card supplies its own element, so the cursor lands over the
  // model being manipulated rather than the parked SVE stage.
  const viewport = T()?.viewportEl?.() || document.getElementById("sve-viewport");
  if (!cursorEl || !viewport) return;
  const rect = viewport.getBoundingClientRect();
  cursorEl.style.display = "block";
  cursorEl.style.left = `${((ndcX + 1) / 2) * rect.width}px`;
  cursorEl.style.top = `${((1 - ndcY) / 2) * rect.height}px`;
  cursorEl.dataset.mode = mode;
}

function hideCursor() {
  if (cursorEl) cursorEl.style.display = "none";
}

// ---------- Gesture target ----------
// Hand tracking drives whichever surface has focus: a generated-asset card when
// one is focused, the SVE scene otherwise. Both expose the same small surface
// (pickAt / select / moveSelectedTo / orbitCamera / dollyCamera /
// commitSelectedMove / register+unregisterInputSource), so nothing below needs
// to know which it is talking to.
function T() {
  const av = window.FridayAssetViewer;
  if (av?.focusedId && av.has(av.focusedId)) return av.gestureTarget;
  return window.SVE;
}

const inputSource = {
  update() {
    if (!running || !video || video.readyState < 2 || !T()) return;
    if (video.currentTime === lastVideoTime) return;
    lastVideoTime = video.currentTime;

    let result;
    try {
      result = landmarker.detectForVideo(video, performance.now());
    } catch {
      return;
    }
    const hands = result?.landmarks || [];
    drawMarkings(hands);
    if (hands.length) dismissHelperHint();

    if (!hands.length) {
      hideCursor();
      showHelperHint();
      endGrab();
      orbiting = false;
      lastOrbit = null;
      lastPinchSpan = null;
      return;
    }

    // ---- Two-hand pinch = zoom ----
    if (hands.length === 2 && isPinch(hands[0]) && isPinch(hands[1])) {
      endGrab();
      orbiting = false;
      const span = dist2d(hands[0][8], hands[1][8]);
      if (lastPinchSpan != null && span > 0.01) {
        const factor = lastPinchSpan / span;
        T().dollyCamera(Math.max(0.9, Math.min(1.1, factor)));
      }
      lastPinchSpan = span;
      setHud("", false);
      hideCursor();
      return;
    }
    lastPinchSpan = null;

    const lm = hands[0];
    // Mirror x so moving your hand right moves the cursor right
    const rawX = (1 - lm[8].x) * 2 - 1;
    const rawY = -(lm[8].y * 2 - 1);
    if (!smoothing) smoothing = { x: rawX, y: rawY };
    smoothing.x = lerp(smoothing.x, rawX, SMOOTH);
    smoothing.y = lerp(smoothing.y, rawY, SMOOTH);
    const nx = smoothing.x, ny = smoothing.y;

    const pinch = isPinch(lm);
    const palm = isOpenPalm(lm);

    if (pinch) {
      if (!grabbing && !orbiting) {
        const hit = T().pickAt(nx, ny);
        if (hit) {
          T().select(hit);
          grabbing = true;
        } else {
          orbiting = true;
          lastOrbit = { x: nx, y: ny };
        }
      }
      if (grabbing) {
        T().moveSelectedTo(nx, ny);
        updateCursor(nx, ny, "grab");
        setHud(T().selected?.userData.spec.label || T().selected?.userData.spec.id || "", true);
      } else if (orbiting && lastOrbit) {
        T().orbitCamera((nx - lastOrbit.x) * 2.2, (ny - lastOrbit.y) * 1.6);
        lastOrbit = { x: nx, y: ny };
        updateCursor(nx, ny, "orbit");
        setHud("", false);
      }
      return;
    }
    endGrab();
    orbiting = false;
    lastOrbit = null;

    if (palm) {
      if (lastOrbit) {
        T().orbitCamera((nx - lastOrbit.x) * 2.2, (ny - lastOrbit.y) * 1.6);
      }
      lastOrbit = { x: nx, y: ny };
      updateCursor(nx, ny, "orbit");
      setHud("", false);
      return;
    }
    lastOrbit = null;

    if (isPointing(lm)) {
      updateCursor(nx, ny, "point");
      const hit = T().pickAt(nx, ny);
      reportPointedObject(hit);
      setHud(hit ? (hit.userData.spec.label || hit.userData.spec.id) : "", !!hit);
      return;
    }

    updateCursor(nx, ny, "idle");
    setHud("", false);
  },
  dispose() {},
};

function endGrab() {
  if (grabbing) {
    T().commitSelectedMove();
    grabbing = false;
  }
}

// ---------- Toggle ----------

function notifyState() {
  window.dispatchEvent(new CustomEvent("friday-gesture-state", { detail: { running } }));
}

async function startHands() {
  if (running || starting) return;
  starting = true;
  try {
    setHud("Loading hand tracker…");
    await ensureLandmarker();
    const stream = await window.FridayCamera.acquire();
    video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.srcObject = stream;
    await video.play();
    lastVideoTime = -1;
    smoothing = null;
    hintDismissed = false;
    running = true;
    T().registerInputSource(inputSource);
    if (overlayEl) overlayEl.style.display = "block";
    btn?.classList.add("active");
    showHelperHint();
  } catch (e) {
    console.error("Hand tracking start failed:", e);
    setHud(`Hand tracking failed: ${e.message}`, true);
    setTimeout(() => { if (!running) setHud("", false); }, 4000);
    if (video) {
      video.srcObject = null;
      video = null;
      window.FridayCamera.release();
    }
    running = false;
  } finally {
    starting = false;
    notifyState();
  }
}

function stopHands() {
  if (starting) {
    // A start is in flight; mark intent by letting sync call us again later.
    return;
  }
  if (!running) return;
  running = false;
  detachInputSource();
  endGrab();
  if (video) {
    video.pause();
    video.srcObject = null;
  }
  video = null;
  window.FridayCamera.release();
  btn?.classList.remove("active");
  hideCursor();
  clearMarkings();
  setHud("", false);
  notifyState();
}

function detachInputSource() {
  window.SVE?.unregisterInputSource?.(inputSource);
  window.FridayAssetViewer?.gestureTarget?.unregisterInputSource?.(inputSource);
}

// Our tick is driven by the render loop of whatever we are pointed at, so when
// focus moves between a card and the scene the registration has to move too —
// otherwise the loop that was ticking us is disposed and tracking silently
// freezes while still holding the camera.
window.addEventListener("friday-asset-focus", () => {
  if (!running) return;
  detachInputSource();
  T()?.registerInputSource?.(inputSource);
});

btn?.addEventListener("click", () => (running ? stopHands() : startHands()));

window.FridayGestures = {
  start: startHands,
  stop: stopHands,
  get running() { return running; },
  get starting() { return starting; },
};
