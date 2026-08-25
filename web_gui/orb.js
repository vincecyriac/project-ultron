/**
 * Holographic Orb — Ultron's ambient presence.
 *
 * A self-contained Three.js scene (own WebGL context, independent of the SVE
 * renderer) that expresses system state purely through colour and energy:
 *
 *   idle      calm cyan        #00F2FE
 *   listening pulsing blue     #0077FF
 *   thinking  glowing amber    #FFB800
 *   speaking  neon emerald     #00FF88
 *   offline   dim ember        #E5726F
 *
 * The core is an additively-blended plasma sphere wrapped in two fresnel glow
 * shells (a shader-side stand-in for a bloom pass — no post-processing deps)
 * and a shell of drifting particles. The look is driven purely by state: colour
 * and energy change only when the state does, never with the live audio.
 */

import * as THREE from "three";

const stage = document.getElementById("orb-stage");

const STATE_COLORS = {
  idle:      "#00F2FE",
  listening: "#0077FF",
  thinking:  "#FFB800",
  speaking:  "#00FF88",
  offline:   "#E5726F",
};

// Per-state energy: how "awake" the orb looks. This is the ONLY thing a state
// changes about the animation — the motion itself (breath, spin, shader pulse)
// is the same steady loop in every state, and nothing is driven by live audio.
const STATE_ENERGY = {
  idle: 0.10,
  listening: 0.34,
  thinking: 0.46,
  speaking: 0.40,
  offline: 0.03,
};

let renderer = null;
let scene = null;
let camera = null;
let coreMesh = null;
let haloMesh = null;
let aureoleMesh = null;
let particles = null;
let orbGroup = null;

const clock = new THREE.Clock();

let currentState = "idle";
const colorCurrent = new THREE.Color(STATE_COLORS.idle);
const colorTarget = new THREE.Color(STATE_COLORS.idle);

let rawLevel = 0;                     // live audio envelope pushed by app.js
let smoothLevel = STATE_ENERGY.idle;  // eased, what the shaders actually see
let energyFloor = STATE_ENERGY.idle;

// ---------- Shader chunks ----------

// Classic Ashima/Gustavson simplex noise — drives the core's organic wobble.
const SIMPLEX_3D = /* glsl */ `
vec3 mod289(vec3 x){ return x - floor(x * (1.0/289.0)) * 289.0; }
vec4 mod289(vec4 x){ return x - floor(x * (1.0/289.0)) * 289.0; }
vec4 permute(vec4 x){ return mod289(((x*34.0)+1.0)*x); }
vec4 taylorInvSqrt(vec4 r){ return 1.79284291400159 - 0.85373472095314 * r; }

float snoise(vec3 v){
  const vec2 C = vec2(1.0/6.0, 1.0/3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);

  vec3 i  = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);

  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);

  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;

  i = mod289(i);
  vec4 p = permute(permute(permute(
             i.z + vec4(0.0, i1.z, i2.z, 1.0))
           + i.y + vec4(0.0, i1.y, i2.y, 1.0))
           + i.x + vec4(0.0, i1.x, i2.x, 1.0));

  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;

  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);

  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);

  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);

  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);

  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));

  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;

  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);

  vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2,p2), dot(p3,p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;

  vec4 m = max(0.6 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m*m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
}
`;

const CORE_VERT = /* glsl */ `
uniform float uTime;
uniform float uLevel;
varying vec3 vNormal;
varying vec3 vView;
varying float vNoise;

${SIMPLEX_3D}

void main() {
  vec3 n = normalize(normal);
  float t = uTime * 0.32;

  float n1 = snoise(n * 1.5 + vec3(0.0, t, 0.0));
  float n2 = snoise(n * 3.2 - vec3(t * 1.4, 0.0, t * 0.8));
  float n3 = snoise(n * 6.1 + vec3(t * 0.6, t * 0.4, 0.0));
  float disp = n1 * 0.55 + n2 * 0.32 + n3 * 0.13;
  vNoise = disp;

  float amp = 0.045 + uLevel * 0.26;
  vec3 p = position + n * disp * amp;

  vNormal = normalize(normalMatrix * n);
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  vView = -mv.xyz;
  gl_Position = projectionMatrix * mv;
}
`;

const CORE_FRAG = /* glsl */ `
uniform vec3 uColor;
uniform float uLevel;
uniform float uTime;
varying vec3 vNormal;
varying vec3 vView;
varying float vNoise;

void main() {
  vec3 N = normalize(vNormal);
  vec3 V = normalize(vView);
  float facing = clamp(dot(N, V), 0.0, 1.0);

  float rim   = pow(1.0 - facing, 2.1);
  float body  = pow(facing, 1.5) * 0.30;
  float veins = smoothstep(0.20, 0.92, abs(vNoise)) * (0.22 + uLevel * 0.5);
  float pulse = 0.06 * sin(uTime * 1.7);

  float e = body + rim * 0.92 + veins + uLevel * 0.22 + pulse;

  // Hue is locked to the state. Two things used to break it:
  //   1. adding white in proportion to uLevel — louder literally meant whiter;
  //   2. letting uColor * e overdrive, so the brightest channel clipped at 1.0
  //      first and dragged the hue toward white.
  // A constant rim keeps the hot silhouette, and normalising by the peak
  // channel keeps the R:G:B ratio — and therefore the hue — exactly fixed
  // while energy still changes how bright the orb reads.
  vec3 col = uColor * e + vec3(1.0) * pow(rim, 2.4) * 0.12;
  float peak = max(col.r, max(col.g, col.b));
  if (peak > 1.0) col /= peak;

  gl_FragColor = vec4(col, 1.0);
}
`;

// Bloom stand-in: a camera-facing quad with a radial falloff. A glow *shell*
// peaks exactly at its own silhouette and leaves a hard ring; this fades to
// nothing well inside the frame, so the core reads as a light source.
const GLOW_VERT = /* glsl */ `
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const GLOW_FRAG = /* glsl */ `
uniform vec3 uColor;
uniform float uLevel;
uniform float uPower;
uniform float uStrength;
varying vec2 vUv;
void main() {
  float r = length(vUv - 0.5) * 2.0;
  float g = pow(max(0.0, 1.0 - r), uPower) * uStrength * (0.72 + uLevel * 0.22);
  gl_FragColor = vec4(uColor * g, g);
}
`;

const PARTICLE_VERT = /* glsl */ `
attribute float aPhase;
attribute float aSize;
uniform float uTime;
uniform float uLevel;
uniform float uPixelRatio;
varying float vAlpha;
void main() {
  float breathe = 1.0 + sin(uTime * 0.75 + aPhase * 6.2831) * 0.05 + uLevel * 0.20;
  vec4 mv = modelViewMatrix * vec4(position * breathe, 1.0);
  gl_Position = projectionMatrix * mv;

  float twinkle = 0.45 + 0.55 * sin(uTime * 2.1 + aPhase * 12.566);
  vAlpha = twinkle * (0.30 + uLevel * 0.75);
  gl_PointSize = aSize * uPixelRatio * (14.0 / max(0.001, -mv.z));
}
`;

const PARTICLE_FRAG = /* glsl */ `
uniform vec3 uColor;
varying float vAlpha;
void main() {
  vec2 d = gl_PointCoord - vec2(0.5);
  float r = length(d);
  if (r > 0.5) discard;
  float falloff = pow(1.0 - r * 2.0, 2.0);
  gl_FragColor = vec4(uColor * falloff * vAlpha, falloff * vAlpha);
}
`;

// ---------- Build ----------

function buildParticles() {
  const COUNT = 1400;
  const pos = new Float32Array(COUNT * 3);
  const phase = new Float32Array(COUNT);
  const size = new Float32Array(COUNT);

  for (let i = 0; i < COUNT; i++) {
    // Uniform direction on the sphere, radius biased toward the inner shell.
    const u = Math.random() * 2 - 1;
    const theta = Math.random() * Math.PI * 2;
    const s = Math.sqrt(1 - u * u);
    const r = 1.15 + Math.pow(Math.random(), 1.7) * 0.60;

    pos[i * 3] = s * Math.cos(theta) * r;
    pos[i * 3 + 1] = u * r;
    pos[i * 3 + 2] = s * Math.sin(theta) * r;
    phase[i] = Math.random();
    size[i] = 1.1 + Math.random() * 2.4;
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("aPhase", new THREE.BufferAttribute(phase, 1));
  geo.setAttribute("aSize", new THREE.BufferAttribute(size, 1));

  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uLevel: { value: 0 },
      uColor: { value: colorCurrent },
      uPixelRatio: { value: Math.min(window.devicePixelRatio || 1, 2) },
    },
    vertexShader: PARTICLE_VERT,
    fragmentShader: PARTICLE_FRAG,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });

  return new THREE.Points(geo, mat);
}

function glowSprite(size, power, strength, order) {
  const mesh = new THREE.Mesh(
    new THREE.PlaneGeometry(size, size),
    new THREE.ShaderMaterial({
      uniforms: {
        uColor: { value: colorCurrent },
        uLevel: { value: 0 },
        uPower: { value: power },
        uStrength: { value: strength },
      },
      vertexShader: GLOW_VERT,
      fragmentShader: GLOW_FRAG,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
      side: THREE.DoubleSide,
    })
  );
  mesh.renderOrder = order;
  return mesh;
}

function init() {
  if (renderer || !stage) return;

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  stage.appendChild(renderer.domElement);

  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
  camera.position.set(0, 0, 6.2);

  orbGroup = new THREE.Group();
  scene.add(orbGroup);

  coreMesh = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1, 48),
    new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
        uLevel: { value: 0 },
        uColor: { value: colorCurrent },
      },
      vertexShader: CORE_VERT,
      fragmentShader: CORE_FRAG,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    })
  );
  coreMesh.renderOrder = 1;
  orbGroup.add(coreMesh);

  // Kept off orbGroup: a rotating quad would turn edge-on and vanish.
  haloMesh = glowSprite(3.9, 2.2, 0.85, -1);
  aureoleMesh = glowSprite(4.7, 3.2, 0.42, -2);
  scene.add(haloMesh, aureoleMesh);

  particles = buildParticles();
  orbGroup.add(particles);

  new ResizeObserver(resize).observe(stage);
  window.addEventListener("resize", resize);
  resize();

  renderer.setAnimationLoop(tick);
}

function resize() {
  if (!renderer || !stage) return;
  const w = stage.clientWidth;
  const h = stage.clientHeight;
  if (!w || !h) return;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  if (particles) {
    particles.material.uniforms.uPixelRatio.value = Math.min(window.devicePixelRatio || 1, 2);
  }
}

// ---------- Loop ----------

function tick() {
  const dt = Math.min(clock.getDelta(), 0.1);
  const t = clock.elapsedTime;

  // Colour and energy ease toward the active state so a change glides rather
  // than snapping. Between changes both are constant, so the look holds for the
  // whole time a state lasts — no live audio feeds in anywhere.
  colorCurrent.lerp(colorTarget, 1 - Math.pow(0.0025, dt));

  // Energy = state floor + live audio, attack fast / release slow. This drives
  // motion and brightness only; hue is fixed by the state (see CORE_FRAG).
  const target = Math.min(1, energyFloor + rawLevel);
  const k = target > smoothLevel ? 1 - Math.pow(0.002, dt) : 1 - Math.pow(0.35, dt);
  smoothLevel += (target - smoothLevel) * k;

  const level = smoothLevel;

  coreMesh.material.uniforms.uTime.value = t;
  coreMesh.material.uniforms.uLevel.value = level;
  haloMesh.material.uniforms.uLevel.value = level;
  aureoleMesh.material.uniforms.uLevel.value = level;
  particles.material.uniforms.uTime.value = t;
  particles.material.uniforms.uLevel.value = level;

  const breath = 1 + Math.sin(t * 0.9) * 0.018 + level * 0.06;
  coreMesh.scale.setScalar(breath);
  haloMesh.scale.setScalar(1 + level * 0.14);
  aureoleMesh.scale.setScalar(1 + level * 0.08);

  orbGroup.rotation.y += dt * (0.09 + level * 0.22);
  orbGroup.rotation.x = Math.sin(t * 0.21) * 0.12;
  particles.rotation.y -= dt * (0.14 + level * 0.30);
  particles.rotation.z += dt * 0.03;

  renderer.render(scene, camera);
}

// ---------- Public API ----------

window.UltronOrb = {
  /** Switch state: idle | listening | thinking | speaking | offline.
   *  Colour and animation follow from the state and hold until it changes. */
  setState(name) {
    if (!STATE_COLORS[name] || name === currentState) return;
    currentState = name;
    colorTarget.set(STATE_COLORS[name]);
    energyFloor = STATE_ENERGY[name];
  },
  get state() { return currentState; },

  /** Live audio envelope, 0..1. Drives displacement, glow and particle spread.
   *  It cannot alter the hue — CORE_FRAG scales the state colour rather than
   *  mixing white into it, so louder means brighter, never whiter. */
  setLevel(v) {
    rawLevel = Math.max(0, Math.min(1, v || 0));
  },

  /** Called by app.js while the layout transition runs. */
  resize,
};

init();
