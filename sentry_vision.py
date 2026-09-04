"""
sentry_vision.py - Screen and webcam capture for Project FRIDAY.

Multi-monitor aware: enumerates displays via Quartz, captures the ACTIVE
display (the one holding the mouse cursor) by default, a specific display
by number, or a composite of all displays laid out in their real
arrangement.

Every screen capture records LAST_CAPTURE_BOUNDS — the global desktop
rectangle the image covers — so sentry_action can map normalized 0-1000
click coordinates back onto the correct monitor.
"""

import os
import io
import numpy as np
import cv2
import Quartz
from PIL import Image, ImageDraw

# Global desktop rect (x, y, w, h) covered by the most recent screen capture.
# Coordinate space: CoreGraphics global coords (origin = top-left of main
# display, y grows downward). Clicks are mapped through this.
LAST_CAPTURE_BOUNDS = None

MAX_CAPTURE_DIM = 1024
JPEG_QUALITY = 80


def get_displays() -> list:
    """Returns [{'id', 'index', 'x', 'y', 'w', 'h', 'main'}] for all active displays."""
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    displays = []
    for i, did in enumerate(ids[:count]):
        b = Quartz.CGDisplayBounds(did)
        displays.append({
            "id": did,
            "index": i + 1,
            "x": int(b.origin.x),
            "y": int(b.origin.y),
            "w": int(b.size.width),
            "h": int(b.size.height),
            "main": bool(Quartz.CGDisplayIsMain(did)),
        })
    return displays


def get_mouse_position() -> tuple:
    """Global mouse position in CG coordinates (top-left origin, y down)."""
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return float(loc.x), float(loc.y)


def _display_for_point(displays: list, px: float, py: float) -> dict:
    for d in displays:
        if d["x"] <= px < d["x"] + d["w"] and d["y"] <= py < d["y"] + d["h"]:
            return d
    return None


def get_frontmost_window() -> dict:
    """The frontmost normal window not owned by this process (the user's focus).
    Returns {'app', 'title', 'x', 'y', 'w', 'h'} or None."""
    try:
        own_pid = os.getpid()
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        for w in wins:
            if w.get("kCGWindowLayer") != 0:
                continue
            if w.get("kCGWindowOwnerPID") == own_pid:
                continue
            b = w.get("kCGWindowBounds", {})
            if b.get("Width", 0) < 80 or b.get("Height", 0) < 60:
                continue
            return {
                "app": w.get("kCGWindowOwnerName", "?"),
                "title": w.get("kCGWindowName") or "",
                "x": b.get("X", 0), "y": b.get("Y", 0),
                "w": b.get("Width", 0), "h": b.get("Height", 0),
            }
    except Exception:
        pass
    return None


def get_active_display() -> dict:
    """The display the user is most likely working on: the one holding the
    frontmost window; falls back to the mouse cursor's display, then main."""
    displays = get_displays()

    front = get_frontmost_window()
    if front:
        cx, cy = front["x"] + front["w"] / 2, front["y"] + front["h"] / 2
        d = _display_for_point(displays, cx, cy)
        if d:
            return d

    mx, my = get_mouse_position()
    d = _display_for_point(displays, mx, my)
    if d:
        return d

    for d in displays:
        if d["main"]:
            return d
    return displays[0]


def list_open_windows() -> str:
    """Lists windows across ALL desktops (Spaces): app, title, display or
    'another desktop'. Lets the AI find work that isn't currently visible."""
    try:
        own_pid = os.getpid()
        displays = get_displays()
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
        )
        visible, hidden = [], []
        seen = set()
        for w in wins:
            if w.get("kCGWindowLayer") != 0 or w.get("kCGWindowOwnerPID") == own_pid:
                continue
            b = w.get("kCGWindowBounds", {})
            if b.get("Width", 0) < 120 or b.get("Height", 0) < 80:
                continue
            app = w.get("kCGWindowOwnerName", "?")
            title = w.get("kCGWindowName") or ""
            key = (app, title, b.get("X"), b.get("Y"))
            if key in seen:
                continue
            seen.add(key)
            onscreen = bool(w.get("kCGWindowIsOnscreen"))
            if onscreen:
                d = _display_for_point(displays, b["X"] + b["Width"] / 2, b["Y"] + b["Height"] / 2)
                disp = f"display {d['index']}" if d else "offscreen"
                visible.append(f"  {app} — {title or '(untitled)'} [{disp}]")
            else:
                hidden.append(f"  {app} — {title or '(untitled)'}")
        lines = ["VISIBLE NOW (on current desktops):"] + (visible or ["  (none)"])
        lines += ["", "ON OTHER DESKTOPS / MINIMIZED (not capturable until brought forward):"] + (hidden[:40] or ["  (none)"])
        lines.append("")
        lines.append("To see a hidden window: activate its app (AppleScript: tell application \"AppName\" to activate) — macOS switches to its desktop — then capture the screen again.")
        return "\n".join(lines)
    except Exception as e:
        return f"[Error]: Window listing failed: {e}"


def describe_displays() -> str:
    displays = get_displays()
    active = get_active_display()
    lines = [f"{len(displays)} display(s):"]
    for d in displays:
        tags = []
        if d["main"]:
            tags.append("main")
        if d["id"] == active["id"]:
            tags.append("ACTIVE - user focus here")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"  Display {d['index']}: {d['w']}x{d['h']} at ({d['x']},{d['y']}){tag_str}")
    front = get_frontmost_window()
    if front:
        lines.append(f"Frontmost window: {front['app']} — {front['title'] or '(untitled)'}")
    return "\n".join(lines)


def _capture_display_image(display_id) -> Image.Image:
    """Captures one display via Quartz and returns a PIL RGB image (native pixels)."""
    img_ref = Quartz.CGDisplayCreateImage(display_id)
    if img_ref is None:
        return None
    w = Quartz.CGImageGetWidth(img_ref)
    h = Quartz.CGImageGetHeight(img_ref)
    bpr = Quartz.CGImageGetBytesPerRow(img_ref)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img_ref))
    arr = np.frombuffer(data, dtype=np.uint8)
    if arr.size < h * bpr:
        return None
    arr = arr[: h * bpr].reshape(h, bpr // 4, 4)[:, :w, :]
    return Image.fromarray(arr[:, :, [2, 1, 0]])  # BGRA -> RGB


def _encode_jpeg(image: Image.Image, max_dim: int = MAX_CAPTURE_DIM) -> bytes:
    if max(image.size) > max_dim:
        scale = max_dim / max(image.size)
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                             Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def capture_screen(display: str = "active"):
    """
    Captures the desktop and returns JPEG bytes.

    display: 'active' (monitor with the mouse, default), 'all' (composite of
    every monitor in real arrangement), or a display number ('1', '2', ...).
    Updates LAST_CAPTURE_BOUNDS for click-coordinate mapping.
    """
    global LAST_CAPTURE_BOUNDS
    try:
        displays = get_displays()
        sel = str(display).strip().lower() if display is not None else "active"

        if sel == "all" and len(displays) > 1:
            min_x = min(d["x"] for d in displays)
            min_y = min(d["y"] for d in displays)
            max_x = max(d["x"] + d["w"] for d in displays)
            max_y = max(d["y"] + d["h"] for d in displays)
            canvas = Image.new("RGB", (max_x - min_x, max_y - min_y), (20, 20, 20))
            draw = ImageDraw.Draw(canvas)
            for d in displays:
                img = _capture_display_image(d["id"])
                if img is None:
                    continue
                if img.size != (d["w"], d["h"]):  # Retina backing store -> logical size
                    img = img.resize((d["w"], d["h"]), Image.Resampling.LANCZOS)
                canvas.paste(img, (d["x"] - min_x, d["y"] - min_y))
                # Number badge so the model can name the display it wants
                bx, by = d["x"] - min_x + 12, d["y"] - min_y + 12
                draw.rectangle([bx, by, bx + 150, by + 60], fill=(200, 30, 30))
                draw.text((bx + 14, by + 8), f"DISPLAY {d['index']}", fill=(255, 255, 255), font_size=36)
            LAST_CAPTURE_BOUNDS = (min_x, min_y, max_x - min_x, max_y - min_y)
            return _encode_jpeg(canvas, max_dim=1536)

        if sel in ("active", "", "all"):
            target = get_active_display()
        else:
            target = next((d for d in displays if str(d["index"]) == sel), None) or get_active_display()

        img = _capture_display_image(target["id"])
        if img is None:
            print("[sentry_vision] Screen capture returned no image (check Screen Recording permission).")
            return None
        LAST_CAPTURE_BOUNDS = (target["x"], target["y"], target["w"], target["h"])
        return _encode_jpeg(img)
    except Exception as e:
        print(f"[sentry_vision] Screen capture failed: {e}")
        return None


def capture_webcam(output_size=(768, 768)):
    """Captures a frame from the default webcam, returns JPEG bytes."""
    cap = None
    try:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("[sentry_vision] Default webcam could not be opened.")
            return None
        for _ in range(5):  # warm up auto-exposure
            cap.read()
        ret, frame = cap.read()
        if not ret:
            print("[sentry_vision] Failed to grab frame from webcam.")
            return None
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb_frame)
        image = image.resize(output_size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        return buffer.getvalue()
    except Exception as e:
        print(f"[sentry_vision] Webcam capture failed: {e}")
        return None
    finally:
        if cap is not None:
            cap.release()


class PersistentWebcam:
    def __init__(self):
        self.cap = None

    def start(self):
        if self.cap is None or not self.cap.isOpened():
            self.cap = cv2.VideoCapture(0)
            if self.cap.isOpened():
                for _ in range(5):
                    self.cap.read()
            else:
                self.cap = None

    def read_frame(self, output_size=(768, 768)):
        if self.cap is None or not self.cap.isOpened():
            self.start()
        if self.cap is None:
            return None
        try:
            ret, frame = self.cap.read()
            if not ret:
                return None
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb_frame)
            image = image.resize(output_size, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=80)
            return buffer.getvalue()
        except Exception as e:
            print(f"[sentry_vision] Webcam read failed: {e}")
            return None

    def stop(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None


if __name__ == "__main__":
    print(describe_displays())
    for mode in ("active", "all"):
        data = capture_screen(mode)
        print(f"capture_screen('{mode}'): {len(data) if data else 0} bytes, bounds={LAST_CAPTURE_BOUNDS}")
