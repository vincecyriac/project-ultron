# Spatial Visualization Engine (SVE)

Ultron's visualization system. Replaces the former HTML-page generator with
live, persistent, interactive 3D scenes rendered inside the GUI's **Spatial**
tab. Scenes behave like digital objects in a workspace: they stay active after
creation, can be edited conversationally at object level, and are manipulated
directly with mouse/touch/keyboard.

## Architecture

```
User request (voice/text)
        │
        ▼
LLM (Gemini Live or local model)          ← visualization planner: decides
        │  tool calls                        objects, ids, layout, animation
        ▼
create_3d_scene / update_3d_scene / delete_3d_scene / list_3d_scenes / inspect_3d_scene
        │  JSON scene-graph specs & ops
        ▼
sentry_scene.SceneManager                 ← validation, persistence
        │                                    (ultron_scenes.json), state, diffing
        │  incremental op broadcast (WebSocket)
        ▼
web_gui/sve.js                            ← rendering backend (Three.js)
        │                                    object cache, animation loop,
        ▼                                    interaction manager
Spatial tab in Ultron GUI                 ← live scene; user actions stream
                                             back to SceneManager
```

Key property: **object-level updates**. "Highlight the left ventricle" becomes
one `{action:"highlight", id:"left_ventricle"}` op — the renderer touches one
mesh; nothing is regenerated.

## Scene graph

A scene: `{id, name, objects: {id → spec}, environment, selected}`.

Object spec (renderer-neutral JSON):

| Field | Meaning |
|---|---|
| `id` | Stable handle the AI and user actions refer to (`"sun"`, `"left_ventricle"`) |
| `type` | `sphere` `box` `cylinder` `cone` `torus` `ring` `plane` `line` `text` `points` `group` `arrow` `capsule` |
| `position/rotation/scale` | Transform; `parent` nests under a `group` |
| `color/opacity/emissive/metalness/roughness/wireframe` | Material |
| `size` | Type-specific dims (`radius`, `width`, `tube`, …) |
| `label` | Floating annotation sprite |
| `points` | Polyline vertices (`line`) |
| `count`, `spread` | Particle systems (`points`) |
| `animation` | `{type: orbit\|spin\|pulse\|bounce, speed, radius, center, axis}` |
| `hidden`, `highlighted` | State flags |

Environment: `{background, grid, stars, ambient, camera:{position,target}}`.

Edit ops (`update_3d_scene`): `add`, `update` (merge `changes`), `remove`,
`highlight`/`unhighlight`, `hide`/`show`, `focus`, `camera`, `environment`,
`explode` (factor), `style` (wireframe/solid).

## Persistence & memory

- Scenes persist in `ultron_scenes.json`; reload on restart, pushed to every
  connecting GUI as an `sve_workspace` snapshot.
- The AI recalls stage state via `list_3d_scenes` / `inspect_3d_scene` —
  including which object the **user selected by clicking**, so "make it red"
  can resolve "it".
- User GUI actions (select, delete object, close scene) stream back via
  `sve_user_action` and update the same store — AI and GUI never diverge.

## Interaction manager

Built-in sources (`web_gui/sve.js`):
- **Mouse/touch**: OrbitControls (rotate/zoom/pan), raycast click-select.
- **Keyboard**: `F` focus selected, `Del` remove selected.
- **Voice**: inherently — any spoken edit becomes scene ops via the LLM.

### Hand tracking (implemented — `web_gui/gestures.js`)

MediaPipe HandLandmarker, fully local (vendored wasm + model, ~27MB in
`web_gui/vendor/`). Toggle with the **✋ Hands** button in the Spatial toolbar;
shares the webcam stream with the preview card (refcounted `UltronCamera`).

| Gesture | Action |
|---|---|
| Point (index finger) | Cursor + hover info |
| Pinch on an object | Grab and move it (release drops + syncs to backend) |
| Pinch on empty space | Orbit camera |
| Open palm move | Orbit camera |
| Two hands pinching | Zoom: spread = in, squeeze = out |

Cursor is mirrored (hand right → cursor right) and EMA-smoothed.

### Other input sources (extension point)

`SVE.registerInputSource({update(dt), dispose()})` runs every frame. Helper
API for sources: `SVE.pickAt(ndc)`, `SVE.select(obj)`, `SVE.selected`,
`SVE.moveSelectedTo(ndc)`, `SVE.commitSelectedMove()`,
`SVE.orbitCamera(dAz, dPolar)`, `SVE.dollyCamera(factor)`. The same contract
fits OpenXR hand tracking, Leap Motion, or game controllers; the scene graph
does not know about them.

## Rendering backend abstraction

The scene graph and op stream are engine-neutral JSON. `sve.js` is the
Three.js implementation. To swap engines (Babylon.js, native Metal/Vulkan
viewer, WebXR renderer): implement the same three entry points —

- consume `sve_workspace` (full state), `sve_scene_create`,
  `sve_scene_update` (ops), `sve_scene_delete`;
- emit `sve_user_action` messages;
- honor the object spec table above.

No Python changes needed.

## Extending the vocabulary ("plugins")

Domain plugins are additions at two layers:

1. **New primitive types** (only when composition can't express it): add a
   case to `_sanitize_object` (`sentry_scene.py`) and to `buildObject`
   (`sve.js`). Example: `molecule_bond`, `terrain`, `gltf` (load external
   models).
2. **Domain knowledge** lives in the LLM prompt/tool description — e.g. a
   Chemistry preset is a prompt fragment teaching element colors (CPK) and
   bond conventions, not code. Add such fragments to the system instruction
   when needed.

Live data feeds (CPU, telemetry): broadcast `sve_scene_update` ops from any
backend task (e.g. a psutil loop emitting `{action:"update", id:"cpu_bar",
changes:{scale:[1, load, 1]}}`) — the renderer already applies streamed ops
continuously; no new mechanism required.

## Current limits (honest list)

- Hand tracking is single-viewport 2D-projected (no depth grab); rotation and
  scale gestures per-object not bound yet — two-hand zoom moves the camera.
- VR/AR/OpenXR, Unity/Unreal backends: out of scope; abstraction supports them.
- `text` rendering uses canvas sprites (billboards), not extruded 3D type.
- One viewport; scenes switch via tabs (all stay resident and animated state
  is preserved — only the active one renders).
- Drag-moving objects with the mouse: not bound yet (select/focus/delete are).
```
