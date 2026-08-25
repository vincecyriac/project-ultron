/**
 * Spatial Visualization Engine (SVE) — Three.js rendering backend.
 *
 * Consumes renderer-neutral scene-graph specs + incremental ops from the
 * Python SceneManager (sentry_scene.py) over WebSocket, and renders live,
 * persistent, interactive 3D scenes inside Ultron's GUI.
 *
 * Interaction sources are pluggable (SVE.registerInputSource): mouse/touch
 * via OrbitControls + raycasting today; MediaPipe hands / XR controllers
 * plug into the same hooks later (see SVE.md).
 */

import * as THREE from "three";
import { OrbitControls } from "./vendor/OrbitControls.js";
import { RoomEnvironment } from "./vendor/RoomEnvironment.js";

const container = document.getElementById("sve-viewport");
const tabsEl = document.getElementById("sve-scene-tabs");
const emptyEl = document.getElementById("sve-empty");
const infoEl = document.getElementById("sve-info");

// ---------- Engine state ----------

const workspace = {};       // sceneId -> { spec, three, objects: id->Object3D, mixers, selected }
let activeSceneId = null;
let renderer = null;
let controls = null;
let camera = null;
const clock = new THREE.Clock();
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const inputSources = [];    // pluggable gesture/controller sources

let pmremEnv = null;

function initRenderer() {
  if (renderer) return;
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.1;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.appendChild(renderer.domElement);

  // Image-based lighting: makes standard materials read as real surfaces
  const pmrem = new THREE.PMREMGenerator(renderer);
  pmremEnv = pmrem.fromScene(new RoomEnvironment(renderer), 0.04).texture;
  Object.values(workspace).forEach((entry) => { entry.three.environment = pmremEnv; });

  camera = new THREE.PerspectiveCamera(55, 1, 0.1, 4000);
  camera.position.set(8, 6, 12);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;

  new ResizeObserver(resize).observe(container);
  resize();

  renderer.domElement.addEventListener("pointerdown", onPointerDown);
  window.addEventListener("keydown", onKeyDown);

  renderer.setAnimationLoop(tick);
}

function resize() {
  if (!renderer || !container.clientWidth) return;
  renderer.setSize(container.clientWidth, container.clientHeight);
  camera.aspect = container.clientWidth / container.clientHeight;
  camera.updateProjectionMatrix();
}

// ---------- Object construction ----------

function makeGeometry(spec) {
  const s = spec.size || {};
  switch (spec.type) {
    case "sphere": return new THREE.SphereGeometry(s.radius ?? 1, 48, 32);
    case "box": return new THREE.BoxGeometry(s.width ?? 1, s.height ?? 1, s.depth ?? 1);
    case "cylinder": return new THREE.CylinderGeometry(s.radiusTop ?? s.radius ?? 0.5, s.radiusBottom ?? s.radius ?? 0.5, s.height ?? 1, 40);
    case "cone": return new THREE.ConeGeometry(s.radius ?? 0.5, s.height ?? 1, 40);
    case "torus": return new THREE.TorusGeometry(s.radius ?? 1, s.tube ?? 0.25, 24, 64);
    case "ring": return new THREE.RingGeometry(s.innerRadius ?? 0.6, s.outerRadius ?? 1, 64);
    case "plane": return new THREE.PlaneGeometry(s.width ?? 2, s.height ?? 2);
    case "capsule": return new THREE.CapsuleGeometry(s.radius ?? 0.4, s.length ?? 1, 8, 24);
    default: return new THREE.SphereGeometry(0.5, 24, 16);
  }
}

function makeMaterial(spec) {
  const mat = new THREE.MeshStandardMaterial({
    color: new THREE.Color(spec.color || "#8899ff"),
    metalness: spec.metalness ?? 0.15,
    roughness: spec.roughness ?? 0.65,
    wireframe: !!spec.wireframe,
    transparent: (spec.opacity ?? 1) < 1,
    opacity: spec.opacity ?? 1,
    side: (spec.type === "plane" || spec.type === "ring") ? THREE.DoubleSide : THREE.FrontSide,
  });
  if (spec.emissive) {
    mat.emissive = new THREE.Color(spec.emissive);
    mat.emissiveIntensity = 0.9;
  }
  return mat;
}

// Labels are drawn at a constant *screen* size (see updateLabels) rather than a
// fixed world size — a fixed size made a long name physically larger than the
// model it named, which buried the scene under overlapping text.
const LABEL_PX = 15;          // on-screen cap height
const LABEL_MAX_VISIBLE = 12; // declutter budget per frame
const LABEL_PAD_PX = 4;       // gap required between two labels

function makeLabelSprite(text, color = "#ffffff") {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const fontPx = 26;
  const font = `500 ${fontPx}px Inter, sans-serif`;
  ctx.font = font;
  const w = Math.ceil(ctx.measureText(text).width) + 26;
  const h = 46;

  canvas.width = Math.ceil(w * dpr);
  canvas.height = Math.ceil(h * dpr);
  ctx.scale(dpr, dpr);
  ctx.font = font;
  ctx.fillStyle = "rgba(7,10,15,0.78)";
  ctx.beginPath();
  ctx.roundRect(0, 0, w, h, 10);
  ctx.fill();
  ctx.strokeStyle = "rgba(255,255,255,0.10)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(0.5, 0.5, w - 1, h - 1, 10);
  ctx.stroke();
  ctx.fillStyle = color;
  ctx.textBaseline = "middle";
  ctx.fillText(text, 13, h / 2);

  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.minFilter = THREE.LinearFilter;
  tex.generateMipmaps = false;

  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: tex,
    // Occluded by geometry like a real annotation, but never writes depth.
    depthTest: true,
    depthWrite: false,
    transparent: true,
  }));
  sprite.userData.isLabel = true;
  sprite.userData.aspect = w / h;   // used to size it in world units each frame
  sprite.renderOrder = 10;
  return sprite;
}

// ---------- Per-frame label sizing + decluttering ----------

const _lblPos = new THREE.Vector3();
const _lblScale = new THREE.Vector3();
const _lblProj = new THREE.Vector3();

function updateLabels(entry) {
  if (!camera || !renderer) return;

  const size = renderer.getSize(new THREE.Vector2());
  if (!size.y) return;
  // World units per screen pixel at a given depth, for this camera.
  const unitsPerPixel = (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2)) / size.y;

  const found = [];
  entry.three.traverse((o) => {
    if (!o.userData.isLabel) return;
    const owner = o.parent;
    if (!owner || !owner.visible) { o.visible = false; return; }
    o.getWorldPosition(_lblPos);
    found.push({ sprite: o, owner, dist: _lblPos.distanceTo(camera.position), world: _lblPos.clone() });
  });
  if (!found.length) return;

  // The selected object always keeps its label; everything else competes.
  const selected = entry.selectedObj;
  found.sort((a, b) => {
    const as = selected && (a.owner === selected) ? -1 : 0;
    const bs = selected && (b.owner === selected) ? -1 : 0;
    return (as - bs) || (a.dist - b.dist);
  });

  const placed = [];
  for (const item of found) {
    const { sprite, dist, world } = item;

    // Constant on-screen size regardless of depth...
    const worldH = LABEL_PX * unitsPerPixel * dist;
    const worldW = worldH * sprite.userData.aspect;
    // ...and immune to whatever scale the parent object carries.
    sprite.parent.getWorldScale(_lblScale);
    sprite.scale.set(
      worldW / (_lblScale.x || 1),
      worldH / (_lblScale.y || 1),
      1
    );

    _lblProj.copy(world).project(camera);
    if (_lblProj.z > 1) { sprite.visible = false; continue; }   // behind the camera

    const cx = (_lblProj.x * 0.5 + 0.5) * size.x;
    const cy = (-_lblProj.y * 0.5 + 0.5) * size.y;
    const halfW = (LABEL_PX * sprite.userData.aspect) / 2 + LABEL_PAD_PX;
    const halfH = LABEL_PX / 2 + LABEL_PAD_PX;

    const clash = placed.some((r) =>
      Math.abs(r.cx - cx) < (r.halfW + halfW) && Math.abs(r.cy - cy) < (r.halfH + halfH));

    if (clash || placed.length >= LABEL_MAX_VISIBLE) {
      sprite.visible = false;
    } else {
      sprite.visible = true;
      placed.push({ cx, cy, halfW, halfH });
    }
  }
}

function buildObject(spec) {
  let obj;

  if (spec.type === "group") {
    obj = new THREE.Group();
  } else if (spec.type === "line") {
    const pts = (spec.points || [[0, 0, 0], [1, 1, 1]]).map((p) => new THREE.Vector3(...p));
    obj = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: new THREE.Color(spec.color || "#8899ff"), transparent: true, opacity: spec.opacity ?? 1 })
    );
  } else if (spec.type === "points") {
    const n = spec.count ?? 200;
    const spread = spec.spread ?? 5;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n * 3; i++) pos[i] = (Math.random() - 0.5) * spread * 2;
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    obj = new THREE.Points(geo, new THREE.PointsMaterial({
      color: new THREE.Color(spec.color || "#ffffff"), size: 0.06, transparent: true, opacity: spec.opacity ?? 0.9,
    }));
  } else if (spec.type === "text") {
    obj = makeLabelSprite(spec.text || spec.label || "?", spec.color || "#ffffff");
    obj.scale.multiplyScalar(2.2);
  } else if (spec.type === "arrow") {
    obj = new THREE.Group();
    const len = (spec.size && spec.size.length) || 2;
    const shaft = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, len * 0.8, 16), makeMaterial(spec));
    shaft.position.y = len * 0.4;
    const head = new THREE.Mesh(new THREE.ConeGeometry(0.14, len * 0.2, 16), makeMaterial(spec));
    head.position.y = len * 0.9;
    obj.add(shaft, head);
  } else {
    obj = new THREE.Mesh(makeGeometry(spec), makeMaterial(spec));
    obj.castShadow = true;
    obj.receiveShadow = true;
  }

  obj.position.set(...spec.position);
  obj.rotation.set(...spec.rotation);
  obj.scale.set(...spec.scale);
  obj.visible = !spec.hidden;
  obj.userData.spec = spec;

  if (spec.label && spec.type !== "text") {
    const sprite = makeLabelSprite(spec.label);
    const bbox = new THREE.Box3().setFromObject(obj);
    const height = Math.max(1e-3, bbox.max.y - bbox.min.y);
    // Sit just above the object, scaled to it — a fixed offset floated small
    // parts' labels far away and buried big ones inside the mesh.
    sprite.position.y = (bbox.max.y - obj.position.y) + height * 0.12 + 0.05;
    obj.add(sprite);
  }
  if (spec.highlighted) applyHighlight(obj, true);
  return obj;
}

function applyHighlight(obj, on) {
  obj.traverse((c) => {
    if (c.isMesh && c.material && !c.userData.isLabel) {
      if (on) {
        c.userData._origEmissive = c.material.emissive ? c.material.emissive.clone() : null;
        c.material.emissive = new THREE.Color("#e5b567");
        c.material.emissiveIntensity = 0.85;
      } else if (c.userData._origEmissive !== undefined) {
        c.material.emissive = c.userData._origEmissive || new THREE.Color(0x000000);
        c.material.emissiveIntensity = 0.9;
      }
    }
  });
  obj.userData.spec.highlighted = on;
}

// ---------- Scene lifecycle ----------

function buildScene(spec) {
  const three = new THREE.Scene();
  const env = spec.environment || {};
  three.background = new THREE.Color(env.background || "#0b0d10");
  if (pmremEnv) three.environment = pmremEnv;

  three.add(new THREE.AmbientLight(0xffffff, env.ambient ?? 0.7));
  const dir = new THREE.DirectionalLight(0xffffff, 1.15);
  dir.position.set(6, 12, 8);
  dir.castShadow = true;
  three.add(dir);

  if (env.grid) {
    const grid = new THREE.GridHelper(30, 30, 0x334, 0x223);
    grid.position.y = -0.01;
    three.add(grid);
  }
  if (env.stars) {
    const n = 1200, pos = new Float32Array(n * 3);
    for (let i = 0; i < n * 3; i++) pos[i] = (Math.random() - 0.5) * 900;
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    three.add(new THREE.Points(geo, new THREE.PointsMaterial({ color: 0xffffff, size: 0.7, sizeAttenuation: true })));
  }

  const entry = { spec, three, objects: {} };

  // Two passes: groups first so children can attach to parents
  const specs = Object.values(spec.objects || {});
  specs.filter((s) => s.type === "group").forEach((s) => {
    entry.objects[s.id] = buildObject(s);
  });
  specs.filter((s) => s.type !== "group").forEach((s) => {
    entry.objects[s.id] = buildObject(s);
  });
  specs.forEach((s) => {
    const o = entry.objects[s.id];
    const parent = s.parent && entry.objects[s.parent];
    (parent || three).add(o);
  });

  return entry;
}

function disposeScene(entry) {
  entry.three.traverse((c) => {
    if (c.geometry) c.geometry.dispose();
    if (c.material) {
      if (c.material.map) c.material.map.dispose();
      c.material.dispose();
    }
  });
}

function setActiveScene(sid) {
  activeSceneId = sid;
  renderTabs();
  const entry = workspace[sid];
  emptyEl.style.display = entry ? "none" : "flex";
  if (!entry) return;
  initRenderer();
  const cam = (entry.spec.environment || {}).camera;
  if (cam) {
    camera.position.set(...cam.position);
    controls.target.set(...cam.target);
  } else {
    camera.position.set(8, 6, 12);
    controls.target.set(0, 0, 0);
  }
  controls.update();
  hideInfo();
}

function renderTabs() {
  tabsEl.innerHTML = "";
  Object.values(workspace).forEach((entry) => {
    const b = document.createElement("button");
    b.className = "sve-tab" + (entry.spec.id === activeSceneId ? " active" : "");
    b.textContent = entry.spec.name;
    b.onclick = () => setActiveScene(entry.spec.id);
    const x = document.createElement("span");
    x.className = "sve-tab-close";
    x.textContent = "×";
    x.onclick = (e) => {
      e.stopPropagation();
      window.sveSend?.({ type: "sve_user_action", scene_id: entry.spec.id, action: "delete_scene" });
      removeScene(entry.spec.id);
    };
    b.appendChild(x);
    tabsEl.appendChild(b);
  });
}

function removeScene(sid) {
  const entry = workspace[sid];
  if (!entry) return;
  disposeScene(entry);
  delete workspace[sid];
  if (activeSceneId === sid) {
    const ids = Object.keys(workspace);
    setActiveScene(ids.length ? ids[ids.length - 1] : null);
  } else {
    renderTabs();
  }
  window.syncGesturesToWorkspace?.();
}

// ---------- Op application (incremental — no scene rebuilds) ----------

function rebuildOne(entry, spec) {
  const old = entry.objects[spec.id];
  const parent = old ? old.parent : (spec.parent && entry.objects[spec.parent]) || entry.three;
  if (old) {
    parent.remove(old);
    old.traverse((c) => { c.geometry?.dispose(); c.material?.dispose?.(); });
  }
  const fresh = buildObject(spec);
  entry.objects[spec.id] = fresh;
  ((spec.parent && entry.objects[spec.parent]) || entry.three).add(fresh);
  entry.spec.objects[spec.id] = spec;
}

function applyOps(sid, ops) {
  const entry = workspace[sid];
  if (!entry) return;
  ops.forEach((op) => {
    const obj = op.id && entry.objects[op.id];
    switch (op.action) {
      case "add":
      case "update":
        rebuildOne(entry, op.object);
        break;
      case "remove":
        if (obj) {
          obj.parent.remove(obj);
          delete entry.objects[op.id];
          delete entry.spec.objects[op.id];
        }
        break;
      case "hide": if (obj) { obj.visible = false; entry.spec.objects[op.id].hidden = true; } break;
      case "show": if (obj) { obj.visible = true; delete entry.spec.objects[op.id].hidden; } break;
      case "highlight": if (obj) applyHighlight(obj, true); break;
      case "unhighlight": if (obj) applyHighlight(obj, false); break;
      case "focus":
        if (obj && sid === activeSceneId) focusOn(obj);
        break;
      case "camera":
        if (sid === activeSceneId && op.camera) {
          camera.position.set(...op.camera.position);
          controls.target.set(...op.camera.target);
          controls.update();
        }
        entry.spec.environment.camera = op.camera;
        break;
      case "environment": {
        Object.assign(entry.spec.environment, op.environment || {});
        if (op.environment?.background) entry.three.background = new THREE.Color(op.environment.background);
        break;
      }
      case "explode":
        Object.values(entry.spec.objects).forEach((s) => {
          s.position = s.position.map((c) => c * op.factor);
          const o = entry.objects[s.id];
          if (o) o.position.set(...s.position);
        });
        break;
      case "style":
        Object.values(entry.spec.objects).forEach((s) => {
          s.wireframe = op.mode === "wireframe";
          entry.objects[s.id]?.traverse((c) => {
            if (c.isMesh && c.material && !c.userData.isLabel) c.material.wireframe = s.wireframe;
          });
        });
        break;
    }
  });
}

function focusOn(obj) {
  const bbox = new THREE.Box3().setFromObject(obj);
  const center = bbox.getCenter(new THREE.Vector3());
  const size = bbox.getSize(new THREE.Vector3()).length() || 2;
  controls.target.copy(center);
  const dirv = camera.position.clone().sub(controls.target).normalize();
  camera.position.copy(center.clone().add(dirv.multiplyScalar(size * 1.8)));
  controls.update();
}

// ---------- Interaction (mouse/touch; gesture sources pluggable) ----------

function onPointerDown(e) {
  if (!activeSceneId) return;
  const entry = workspace[activeSceneId];
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(Object.values(entry.objects), true);
  const hit = hits.find((h) => !h.object.userData.isLabel);
  if (!hit) { selectObject(entry, null); return; }
  let node = hit.object;
  while (node && !node.userData.spec) node = node.parent;
  if (node) selectObject(entry, node);
}

function selectObject(entry, obj) {
  if (entry.selectedObj && entry.selectedObj !== obj) applyHighlight(entry.selectedObj, false);
  entry.selectedObj = obj;
  if (obj) {
    applyHighlight(obj, true);
    const spec = obj.userData.spec;
    entry.spec.selected = spec.id;
    showInfo(`${spec.label || spec.id} — [F] focus · [Del] remove`);
    window.sveSend?.({ type: "sve_user_action", scene_id: entry.spec.id, action: "select", object_id: spec.id });
  } else {
    entry.spec.selected = null;
    hideInfo();
    window.sveSend?.({ type: "sve_user_action", scene_id: entry.spec.id, action: "select", object_id: null });
  }
}

function onKeyDown(e) {
  if (!activeSceneId) return;
  if (document.activeElement && document.activeElement.tagName === "INPUT") return;
  const entry = workspace[activeSceneId];
  const obj = entry?.selectedObj;
  if (!obj) return;
  const spec = obj.userData.spec;
  if (e.key === "f" || e.key === "F") focusOn(obj);
  if (e.key === "Delete" || e.key === "Backspace") {
    applyOps(activeSceneId, [{ action: "remove", id: spec.id }]);
    entry.selectedObj = null;
    hideInfo();
    window.sveSend?.({ type: "sve_user_action", scene_id: entry.spec.id, action: "delete_object", object_id: spec.id });
  }
}

function showInfo(text) { infoEl.textContent = text; infoEl.style.display = "block"; }
function hideInfo() { infoEl.style.display = "none"; }

// ---------- Animation loop ----------

function tick() {
  const dt = clock.getDelta();
  const t = clock.elapsedTime;
  controls?.update();
  inputSources.forEach((src) => src.update?.(dt));

  const entry = activeSceneId && workspace[activeSceneId];
  if (!entry) return;

  Object.values(entry.spec.objects).forEach((spec) => {
    const anim = spec.animation;
    const obj = entry.objects[spec.id];
    if (!anim || !obj || anim.type === "none") return;
    const speed = anim.speed ?? 0.5;
    if (anim.type === "spin") {
      obj.rotation[anim.axis || "y"] += speed * dt;
    } else if (anim.type === "orbit") {
      const c = anim.center || [0, 0, 0];
      const r = anim.radius ?? Math.hypot(spec.position[0] - c[0], spec.position[2] - c[2]) ?? 3;
      const phase = (spec.id.charCodeAt(0) % 10) * 0.7;
      const a = t * speed + phase;
      if ((anim.axis || "y") === "y") {
        obj.position.set(c[0] + Math.cos(a) * r, spec.position[1], c[2] + Math.sin(a) * r);
      } else {
        obj.position.set(spec.position[0], c[1] + Math.cos(a) * r, c[2] + Math.sin(a) * r);
      }
    } else if (anim.type === "pulse") {
      const s = 1 + Math.sin(t * speed * 4) * 0.08;
      obj.scale.set(spec.scale[0] * s, spec.scale[1] * s, spec.scale[2] * s);
    } else if (anim.type === "bounce") {
      obj.position.y = spec.position[1] + Math.abs(Math.sin(t * speed * 3)) * 0.6;
    }
  });

  updateLabels(entry);
  renderer.render(entry.three, camera);
}

// ---------- Public API (window.SVE) ----------

window.SVE = {
  handleEvent(msg) {
    switch (msg.type) {
      case "sve_workspace":
        Object.values(workspace).forEach(disposeScene);
        Object.keys(workspace).forEach((k) => delete workspace[k]);
        Object.values(msg.scenes || {}).forEach((spec) => {
          workspace[spec.id] = buildScene(spec);
        });
        {
          const ids = Object.keys(workspace);
          if (ids.length) setActiveScene(ids[ids.length - 1]);
          else { renderTabs(); emptyEl.style.display = "flex"; }
        }
        break;
      case "sve_scene_create":
        workspace[msg.scene.id] = buildScene(msg.scene);
        setActiveScene(msg.scene.id);
        return true; // caller may want to switch to Spatial tab
      case "sve_scene_update":
        applyOps(msg.scene_id, msg.ops);
        if (msg.scene_id !== activeSceneId) setActiveScene(msg.scene_id);
        return true;
      case "sve_scene_delete":
        removeScene(msg.scene_id);
        break;
    }
    return false;
  },
  resetCamera() {
    if (activeSceneId) setActiveScene(activeSceneId);
  },

  /** Re-fit the renderer to its container — called while the spatial layout
   *  transition runs so the viewport never stretches mid-animation. */
  resize,

  registerInputSource(src) {
    // Gesture/XR extension point: src = {update(dt), dispose()} manipulating
    // the exported state below. See SVE.md "Gesture Integration Layer".
    initRenderer();
    inputSources.push(src);
  },
  unregisterInputSource(src) {
    const i = inputSources.indexOf(src);
    if (i >= 0) inputSources.splice(i, 1);
  },

  // ---- helpers for gesture/controller input sources ----

  /** Raycast at normalized device coords (-1..1). Returns the spec'd Object3D or null. */
  pickAt(ndcX, ndcY) {
    const entry = activeSceneId && workspace[activeSceneId];
    if (!entry || !camera) return null;
    pointer.set(ndcX, ndcY);
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObjects(Object.values(entry.objects), true);
    const hit = hits.find((h) => !h.object.userData.isLabel);
    if (!hit) return null;
    let node = hit.object;
    while (node && !node.userData.spec) node = node.parent;
    return node || null;
  },

  /** Select (highlight + sync) an object, or null to clear. */
  select(obj) {
    const entry = activeSceneId && workspace[activeSceneId];
    if (entry) selectObject(entry, obj);
  },

  get selected() {
    const entry = activeSceneId && workspace[activeSceneId];
    return entry ? entry.selectedObj || null : null;
  },

  /** Move the selected object so it sits under NDC coords on its current camera-distance plane. */
  moveSelectedTo(ndcX, ndcY) {
    const entry = activeSceneId && workspace[activeSceneId];
    const obj = entry && entry.selectedObj;
    if (!obj || !camera) return;
    const dist = obj.getWorldPosition(new THREE.Vector3()).distanceTo(camera.position);
    const v = new THREE.Vector3(ndcX, ndcY, 0.5).unproject(camera);
    const dir = v.sub(camera.position).normalize();
    const world = camera.position.clone().add(dir.multiplyScalar(dist));
    const local = obj.parent ? obj.parent.worldToLocal(world.clone()) : world;
    obj.position.copy(local);
    obj.userData.spec.position = [local.x, local.y, local.z];
  },

  /** Commit the selected object's position to the backend store. */
  commitSelectedMove() {
    const entry = activeSceneId && workspace[activeSceneId];
    const obj = entry && entry.selectedObj;
    if (!obj) return;
    window.sveSend?.({
      type: "sve_user_action",
      scene_id: entry.spec.id,
      action: "move",
      object_id: obj.userData.spec.id,
      data: { position: obj.userData.spec.position },
    });
  },

  /** Orbit the camera by azimuth/polar deltas (radians). */
  orbitCamera(dAzimuth, dPolar) {
    if (!camera || !controls) return;
    const offset = camera.position.clone().sub(controls.target);
    const spherical = new THREE.Spherical().setFromVector3(offset);
    spherical.theta -= dAzimuth;
    spherical.phi = Math.max(0.05, Math.min(Math.PI - 0.05, spherical.phi - dPolar));
    camera.position.copy(controls.target).add(new THREE.Vector3().setFromSpherical(spherical));
    controls.update();
  },

  /** Dolly the camera: factor >1 zooms out, <1 zooms in. */
  dollyCamera(factor) {
    if (!camera || !controls) return;
    const offset = camera.position.clone().sub(controls.target);
    const len = Math.max(0.5, Math.min(600, offset.length() * factor));
    camera.position.copy(controls.target).add(offset.normalize().multiplyScalar(len));
    controls.update();
  },

  hasActiveScene() {
    return !!(activeSceneId && workspace[activeSceneId]);
  },

  get state() {
    return { workspace, activeSceneId, camera, controls };
  },
};
