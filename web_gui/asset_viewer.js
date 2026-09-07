/**
 * asset_viewer.js — renders a generated .glb inside a HUD card.
 *
 * app.js is a classic script and cannot import ES modules, so this follows the
 * same shape as orb.js and gestures.js: a module that hangs its API off window.
 *
 * Every mounted card owns a WebGL context and browsers cap those (~16), so a
 * dismissed card MUST be disposed — mount() and the deck's dismiss path both
 * call dispose() to guarantee it.
 *
 * A focused card also acts as a gesture target: it implements the same surface
 * gestures.js drives on the SVE (pickAt / select / moveSelectedTo / orbitCamera
 * / dollyCamera / commitSelectedMove), so hand tracking works on a generated
 * model exactly as it does on a scene.
 */

import * as THREE from "three";
import { GLTFLoader } from "./vendor/GLTFLoader.js";
import { OrbitControls } from "./vendor/OrbitControls.js";
import { RoomEnvironment } from "./vendor/RoomEnvironment.js";

const loader = new GLTFLoader();
const viewers = new Map();          // widget id -> viewer state
let focusedId = null;               // which card hand gestures currently drive

const raycaster = new THREE.Raycaster();

function disposeSceneGraph(root) {
  root.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
    for (const m of mats) {
      for (const k of Object.keys(m)) {
        const v = m[k];
        if (v && v.isTexture) v.dispose();
      }
      m.dispose();
    }
  });
}

function dispose(id) {
  const v = viewers.get(id);
  if (!v) return;
  viewers.delete(id);
  if (focusedId === id) focusedId = null;
  cancelAnimationFrame(v.raf);
  v.resizeObserver?.disconnect();
  v.controls?.dispose();
  if (v.root) disposeSceneGraph(v.root);
  v.pmrem?.dispose();
  v.renderer?.dispose();
  v.renderer?.domElement?.remove();
}

/** Render glbUrl into mountNode. Replaces any viewer already under that id. */
function mount(mountNode, glbUrl, id, label) {
  dispose(id);                       // never leak a context on re-render
  mountNode.innerHTML = "";

  const width = Math.max(1, mountNode.clientWidth);
  const height = Math.max(1, mountNode.clientHeight || 260);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, width / height, 0.01, 1000);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(width, height);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.1;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  mountNode.appendChild(renderer.domElement);

  // Real PBR needs an environment to reflect; the coloured lights on their own
  // would leave metal and rough surfaces reading flat.
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(renderer), 0.04).texture;

  scene.add(new THREE.HemisphereLight(0xffffff, 0x111122, 1.6));

  const keyLight = new THREE.DirectionalLight(0x00f2fe, 2.2);
  keyLight.position.set(3, 5, 3);
  scene.add(keyLight);

  const rimLight = new THREE.DirectionalLight(0xa18cd1, 1.5);
  rimLight.position.set(-3, -2, -3);
  scene.add(rimLight);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.enablePan = false;

  const state = {
    id, scene, camera, renderer, controls, pmrem, stage: mountNode,
    root: null, raf: 0, resizeObserver: null,
    inputSources: new Set(),        // gestures.js registers its tick here
    grabbed: false, spin: true,
  };
  viewers.set(id, state);

  const resizeObserver = new ResizeObserver(() => {
    const w = Math.max(1, mountNode.clientWidth);
    const h = Math.max(1, mountNode.clientHeight);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  });
  resizeObserver.observe(mountNode);
  state.resizeObserver = resizeObserver;

  loader.load(
    glbUrl,
    (gltf) => {
      if (!viewers.has(id)) return;         // dismissed mid-download
      const root = gltf.scene;

      // Tripo returns models at arbitrary scale and offset. Centring alone
      // leaves most of them a speck or clipped, so frame the bounding sphere:
      // normalise to unit size, then pull the camera back to fit it.
      const box = new THREE.Box3().setFromObject(root);
      const size = box.getSize(new THREE.Vector3());
      const center = box.getCenter(new THREE.Vector3());
      const maxDim = Math.max(size.x, size.y, size.z) || 1;

      root.position.sub(center);
      root.scale.multiplyScalar(1 / maxDim);

      // gestures.js reads label/id off the hit object to drive its HUD.
      root.userData.spec = { id, label: label || id };

      const dist = 1 / (2 * Math.tan((camera.fov * Math.PI) / 360));
      camera.position.set(0, 0.35, dist * 1.9);
      camera.lookAt(0, 0, 0);
      controls.target.set(0, 0, 0);
      controls.update();
      // Remembered so "Reset view" can put the framing back after gesturing.
      state.home = { pos: camera.position.clone(), target: controls.target.clone() };

      scene.add(root);
      state.root = root;

      const animate = () => {
        state.raf = requestAnimationFrame(animate);
        if (state.visible === false) return;      // hidden in the model slot
        // Hand-tracking ticks here so gestures keep working even when no SVE
        // scene is live to drive them.
        for (const src of state.inputSources) {
          try { src.update(); } catch { /* one bad frame must not kill the loop */ }
        }
        if (state.spin && !state.grabbed) root.rotation.y += 0.006;
        controls.update();
        renderer.render(scene, camera);
      };
      state.animate = animate;
      animate();
    },
    undefined,
    (err) => {
      // A silent black card is the worst outcome — say what went wrong.
      console.warn("GLB load failed", glbUrl, err);
      dispose(id);
      mountNode.innerHTML =
        '<div class="hud-note">Could not load the 3D model — the file may be '
        + "unreachable or blocked by its host.</div>";
    }
  );
}

// ---------- Gesture target ----------
// Mirrors the surface gestures.js drives on window.SVE, so the same hand
// tracking works here with no special-casing in the gesture loop.

function focused() {
  return focusedId ? viewers.get(focusedId) : null;
}

const gestureTarget = {
  get selected() { return focused()?.selected || null; },

  viewportEl() { return focused()?.stage || null; },

  pickAt(nx, ny) {
    const v = focused();
    if (!v || !v.root) return null;
    // Do not assume a frame has rendered since the camera last moved: the
    // raycast reads matrixWorld, and a stale one silently misses everything.
    v.camera.updateMatrixWorld();
    v.root.updateMatrixWorld();
    raycaster.setFromCamera(new THREE.Vector2(nx, ny), v.camera);
    return raycaster.intersectObject(v.root, true).length ? v.root : null;
  },

  select(hit) {
    const v = focused();
    if (v) { v.selected = hit; v.grabbed = true; }
  },

  /** Slide the model across the plane it currently sits on. */
  moveSelectedTo(nx, ny) {
    const v = focused();
    if (!v || !v.root) return;
    v.camera.updateMatrixWorld();
    const depth = v.camera.position.distanceTo(v.controls.target);
    const p = new THREE.Vector3(nx, ny, 0.5).unproject(v.camera);
    p.sub(v.camera.position).normalize().multiplyScalar(depth).add(v.camera.position);
    v.root.position.copy(p);
  },

  commitSelectedMove() {
    const v = focused();
    if (v) { v.selected = null; v.grabbed = false; }   // nothing to persist
  },

  orbitCamera(dx, dy) {
    const v = focused();
    if (!v) return;
    const offset = v.camera.position.clone().sub(v.controls.target);
    const sph = new THREE.Spherical().setFromVector3(offset);
    sph.theta -= dx;
    sph.phi = Math.max(0.08, Math.min(Math.PI - 0.08, sph.phi - dy));
    v.camera.position.copy(v.controls.target).add(new THREE.Vector3().setFromSpherical(sph));
    v.camera.lookAt(v.controls.target);
    v.controls.update();
  },

  dollyCamera(factor) {
    const v = focused();
    if (!v) return;
    const offset = v.camera.position.clone().sub(v.controls.target);
    const len = Math.max(0.4, Math.min(40, offset.length() * factor));
    v.camera.position.copy(v.controls.target).add(offset.setLength(len));
    v.controls.update();
  },

  registerInputSource(src) { focused()?.inputSources.add(src); },
  unregisterInputSource(src) { for (const v of viewers.values()) v.inputSources.delete(src); },
};

/** Hand gestures follow the focused card; null hands them back to the SVE. */
function focus(id) {
  if (id && !viewers.has(id)) return false;
  const prev = focusedId;
  focusedId = id || null;
  if (prev && prev !== focusedId) viewers.get(prev)?.inputSources.clear();
  window.dispatchEvent(new CustomEvent("friday-asset-focus", { detail: { id: focusedId } }));
  return true;
}

/** Pause a card that is not the visible model; resume it when it is.
 *  The context and camera state are kept, only the drawing stops. */
function setVisible(id, on) {
  const v = viewers.get(id);
  if (!v) return;
  v.visible = on !== false;
  if (v.visible) {
    // A resumed card may have been resized while hidden.
    const w = Math.max(1, v.stage.clientWidth), h = Math.max(1, v.stage.clientHeight);
    v.camera.aspect = w / h;
    v.camera.updateProjectionMatrix();
    v.renderer.setSize(w, h);
  }
}

/** Restore the framing chosen when the model was first fitted. */
function resetCamera(id) {
  const v = viewers.get(id || focusedId);
  if (!v || !v.home) return;
  v.camera.position.copy(v.home.pos);
  v.controls.target.copy(v.home.target);
  v.camera.lookAt(v.controls.target);
  v.controls.update();
  if (v.root) v.root.position.set(0, 0, 0);     // undo any gesture drag too
}

window.FridayAssetViewer = {
  mount, dispose, focus, resetCamera, setVisible,
  get focusedId() { return focusedId; },
  gestureTarget,
  has: (id) => viewers.has(id),
  disposeAll: () => [...viewers.keys()].forEach(dispose),
};
