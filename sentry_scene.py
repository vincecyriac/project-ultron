"""
sentry_scene.py - Spatial Visualization Engine (SVE) backend for Project Ultron.

Replaces HTML visualization generation with a persistent scene-graph
architecture. The LLM emits structured object specs (JSON), this module
validates and stores them, and the GUI's Three.js layer renders them live.

Pipeline:  LLM tool call -> SceneManager (validate + persist + diff)
           -> WebSocket ops broadcast -> GUI renderer (incremental update)

Scenes persist in ultron_scenes.json across restarts. Edits are expressed
as object-level operations (add/update/remove/highlight/hide/show/focus/
camera) so the renderer never rebuilds a whole scene for a small change.

The renderer backend is abstracted: the scene graph is renderer-neutral
JSON. web_gui/sve.js is the Three.js backend; any engine that can consume
the same op stream (Babylon, native, XR) can replace it. See SVE.md.
"""

import os
import json
import time
import uuid

SCENES_FILE = "ultron_scenes.json"

VALID_TYPES = {
    "sphere", "box", "cylinder", "cone", "torus", "ring", "plane",
    "line", "text", "points", "group", "arrow", "capsule",
}

VALID_ANIMATIONS = {"orbit", "spin", "pulse", "bounce", "none"}

VALID_OPS = {
    "add", "update", "remove", "highlight", "unhighlight",
    "hide", "show", "focus", "camera", "environment", "explode", "style",
}

# Set by the hub so scene changes stream to connected GUIs.
_broadcaster = None


def set_broadcaster(fn):
    global _broadcaster
    _broadcaster = fn


def _emit(event: dict):
    if _broadcaster:
        try:
            _broadcaster(event)
        except Exception:
            pass


def _num_list(v, n, default):
    try:
        vals = [float(x) for x in v][:n]
        while len(vals) < n:
            vals.append(0.0)
        return vals
    except Exception:
        return list(default)


def _sanitize_object(obj: dict) -> dict:
    """Validates and normalizes one scene-graph node."""
    if not isinstance(obj, dict):
        raise ValueError("object spec must be a dict")
    otype = str(obj.get("type", "sphere")).lower()
    if otype not in VALID_TYPES:
        raise ValueError(f"unknown object type '{otype}' (valid: {sorted(VALID_TYPES)})")

    out = {
        "id": str(obj.get("id") or f"obj_{uuid.uuid4().hex[:8]}"),
        "type": otype,
        "position": _num_list(obj.get("position", [0, 0, 0]), 3, [0, 0, 0]),
        "rotation": _num_list(obj.get("rotation", [0, 0, 0]), 3, [0, 0, 0]),
        "color": str(obj.get("color", "#8899ff")),
        "opacity": max(0.0, min(1.0, float(obj.get("opacity", 1.0)))),
    }

    scale = obj.get("scale", 1)
    out["scale"] = _num_list(scale, 3, [1, 1, 1]) if isinstance(scale, (list, tuple)) else [float(scale)] * 3

    for key in ("label", "parent", "emissive"):
        if obj.get(key):
            out[key] = str(obj[key])
    for key in ("wireframe",):
        if key in obj:
            out[key] = bool(obj[key])
    for key in ("metalness", "roughness"):
        if key in obj:
            out[key] = max(0.0, min(1.0, float(obj[key])))

    size = obj.get("size", {})
    if isinstance(size, dict):
        out["size"] = {k: float(v) for k, v in size.items()
                       if k in ("radius", "width", "height", "depth", "tube", "innerRadius",
                                "outerRadius", "radiusTop", "radiusBottom", "length") and _is_num(v)}
    if otype == "line" and isinstance(obj.get("points"), list):
        out["points"] = [_num_list(p, 3, [0, 0, 0]) for p in obj["points"]][:200]
    if otype == "points":
        out["count"] = int(min(5000, max(1, obj.get("count", 200))))
        out["spread"] = float(obj.get("spread", 5))
    if otype == "text":
        out["text"] = str(obj.get("text", out.get("label", "?")))[:120]

    anim = obj.get("animation")
    if isinstance(anim, dict) and str(anim.get("type", "none")).lower() in VALID_ANIMATIONS:
        a = {"type": str(anim["type"]).lower()}
        for k in ("speed", "radius"):
            if k in anim and _is_num(anim[k]):
                a[k] = float(anim[k])
        if "axis" in anim and str(anim["axis"]) in ("x", "y", "z"):
            a["axis"] = str(anim["axis"])
        if "center" in anim:
            a["center"] = _num_list(anim["center"], 3, [0, 0, 0])
        out["animation"] = a

    return out


def _is_num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _sanitize_environment(env: dict) -> dict:
    if not isinstance(env, dict):
        return {}
    out = {}
    if "background" in env:
        out["background"] = str(env["background"])
    if "grid" in env:
        out["grid"] = bool(env["grid"])
    if "ambient" in env and _is_num(env["ambient"]):
        out["ambient"] = max(0.0, min(3.0, float(env["ambient"])))
    if "stars" in env:
        out["stars"] = bool(env["stars"])
    cam = env.get("camera")
    if isinstance(cam, dict):
        out["camera"] = {
            "position": _num_list(cam.get("position", [8, 6, 12]), 3, [8, 6, 12]),
            "target": _num_list(cam.get("target", [0, 0, 0]), 3, [0, 0, 0]),
        }
    return out


class SceneManager:
    def __init__(self):
        self.scenes = {}
        self._load()

    # ---------- persistence ----------

    def _load(self):
        if os.path.exists(SCENES_FILE):
            try:
                with open(SCENES_FILE, "r") as f:
                    self.scenes = json.load(f)
            except Exception:
                self.scenes = {}

    def _save(self):
        try:
            with open(SCENES_FILE, "w") as f:
                json.dump(self.scenes, f)
        except Exception:
            pass

    # ---------- lookup ----------

    def resolve(self, ref: str):
        """Finds a scene by id or (case-insensitive) name."""
        if ref in self.scenes:
            return ref
        low = str(ref).strip().lower()
        for sid, sc in self.scenes.items():
            if sc.get("name", "").strip().lower() == low:
                return sid
        return None

    def workspace_snapshot(self) -> dict:
        return {"type": "sve_workspace", "scenes": self.scenes}

    # ---------- API: create ----------

    def create_scene(self, name: str, objects: list, environment: dict = None) -> str:
        sid = f"scene_{uuid.uuid4().hex[:8]}"
        sanitized, errors = [], []
        for i, obj in enumerate(objects or []):
            try:
                sanitized.append(_sanitize_object(obj))
            except Exception as e:
                errors.append(f"object[{i}]: {e}")
        scene = {
            "id": sid,
            "name": str(name or "Untitled"),
            "objects": {o["id"]: o for o in sanitized},
            "environment": _sanitize_environment(environment or {}),
            "created": time.time(),
            "selected": None,
        }
        self.scenes[sid] = scene
        self._save()
        _emit({"type": "sve_scene_create", "scene": scene})
        msg = (f"Scene '{scene['name']}' created (id: {sid}) with {len(sanitized)} objects: "
               f"{', '.join(list(scene['objects'].keys())[:20])}. It is now live in the Spatial workspace "
               "and stays active. Edit it with update_3d_scene ops instead of recreating.")
        if errors:
            msg += f" Skipped invalid objects: {'; '.join(errors[:5])}"
        return msg

    # ---------- API: edit ----------

    def update_scene(self, ref: str, operations: list) -> str:
        sid = self.resolve(ref)
        if not sid:
            return (f"[Error]: No scene matching '{ref}'. Existing scenes: "
                    f"{[s['name'] for s in self.scenes.values()] or 'none'}. Use create_3d_scene for new ones.")
        scene = self.scenes[sid]
        applied, errors, ops_out = [], [], []

        for i, op in enumerate(operations or []):
            try:
                action = str(op.get("action", "")).lower()
                if action not in VALID_OPS:
                    raise ValueError(f"unknown action '{action}' (valid: {sorted(VALID_OPS)})")

                if action == "add":
                    o = _sanitize_object(op.get("object", {}))
                    scene["objects"][o["id"]] = o
                    ops_out.append({"action": "add", "object": o})
                    applied.append(f"add {o['id']}")

                elif action == "update":
                    oid = str(op.get("id", ""))
                    if oid not in scene["objects"]:
                        raise ValueError(f"no object '{oid}' in scene")
                    merged = {**scene["objects"][oid], **(op.get("changes") or {})}
                    merged["id"] = oid
                    o = _sanitize_object(merged)
                    scene["objects"][oid] = o
                    ops_out.append({"action": "update", "object": o})
                    applied.append(f"update {oid}")

                elif action == "remove":
                    oid = str(op.get("id", ""))
                    scene["objects"].pop(oid, None)
                    ops_out.append({"action": "remove", "id": oid})
                    applied.append(f"remove {oid}")

                elif action in ("highlight", "unhighlight", "hide", "show", "focus"):
                    oid = str(op.get("id", ""))
                    if oid not in scene["objects"]:
                        raise ValueError(f"no object '{oid}' in scene")
                    if action == "hide":
                        scene["objects"][oid]["hidden"] = True
                    elif action == "show":
                        scene["objects"][oid].pop("hidden", None)
                    elif action == "highlight":
                        scene["objects"][oid]["highlighted"] = True
                    elif action == "unhighlight":
                        scene["objects"][oid].pop("highlighted", None)
                    ops_out.append({"action": action, "id": oid})
                    applied.append(f"{action} {oid}")

                elif action == "camera":
                    cam = _sanitize_environment({"camera": op.get("camera", {})}).get("camera")
                    if cam:
                        scene["environment"]["camera"] = cam
                        ops_out.append({"action": "camera", "camera": cam})
                        applied.append("camera")

                elif action == "environment":
                    env = _sanitize_environment(op.get("environment", {}))
                    scene["environment"].update(env)
                    ops_out.append({"action": "environment", "environment": env})
                    applied.append("environment")

                elif action == "explode":
                    factor = float(op.get("factor", 1.6))
                    for o in scene["objects"].values():
                        o["position"] = [c * factor for c in o["position"]]
                    ops_out.append({"action": "explode", "factor": factor})
                    applied.append(f"explode x{factor}")

                elif action == "style":
                    style = str(op.get("mode", "solid")).lower()
                    wire = style == "wireframe"
                    for o in scene["objects"].values():
                        o["wireframe"] = wire
                    ops_out.append({"action": "style", "mode": style})
                    applied.append(f"style {style}")

            except Exception as e:
                errors.append(f"op[{i}]: {e}")

        self._save()
        if ops_out:
            _emit({"type": "sve_scene_update", "scene_id": sid, "ops": ops_out})
        msg = f"Scene '{scene['name']}': applied {len(applied)} ops ({', '.join(applied[:15])})."
        if errors:
            msg += f" Errors: {'; '.join(errors[:5])}"
        return msg

    # ---------- API: delete / list ----------

    def delete_scene(self, ref: str) -> str:
        sid = self.resolve(ref)
        if not sid:
            return f"[Error]: No scene matching '{ref}'."
        name = self.scenes[sid].get("name")
        del self.scenes[sid]
        self._save()
        _emit({"type": "sve_scene_delete", "scene_id": sid})
        return f"Scene '{name}' deleted from the workspace."

    def focus_context(self) -> str:
        """One-line description of what the user is pointing at / has selected,
        for injection into the AI's conversational context. Empty if nothing."""
        for sc in self.scenes.values():
            oid = sc.get("pointed") or sc.get("selected")
            if oid and oid in sc.get("objects", {}):
                o = sc["objects"][oid]
                return (f"object '{oid}'" + (f" (labeled '{o['label']}')" if o.get("label") else "")
                        + f" in scene '{sc['name']}'")
        return ""

    def list_scenes(self) -> str:
        if not self.scenes:
            return "Workspace is empty — no active scenes."
        lines = ["Active scenes in the Spatial workspace:"]
        for sid, sc in self.scenes.items():
            objs = sc.get("objects", {})
            sel = f" | user selected: {sc['selected']}" if sc.get("selected") else ""
            lines.append(f"  {sc['name']} (id: {sid}) — {len(objs)} objects: "
                         f"{', '.join(list(objs.keys())[:15])}{sel}")
        return "\n".join(lines)

    def describe_scene(self, ref: str) -> str:
        sid = self.resolve(ref)
        if not sid:
            return f"[Error]: No scene matching '{ref}'."
        return json.dumps(self.scenes[sid])

    # ---------- user interaction feedback (from GUI) ----------

    def user_action(self, scene_id: str, action: str, object_id: str = None, data: dict = None):
        """Records interactions performed directly in the GUI so the AI stays in sync."""
        scene = self.scenes.get(scene_id)
        if not scene:
            return
        if action == "select":
            scene["selected"] = object_id
        elif action == "point_at":
            scene["pointed"] = object_id
        elif action == "move" and object_id in scene.get("objects", {}) and data:
            scene["objects"][object_id]["position"] = _num_list(data.get("position", [0, 0, 0]), 3, [0, 0, 0])
        elif action == "delete_object" and object_id:
            scene["objects"].pop(object_id, None)
        elif action == "delete_scene":
            self.scenes.pop(scene_id, None)
        self._save()


manager = SceneManager()
