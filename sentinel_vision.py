"""
Visual perception for the Screen Sentinel.

Used only when the accessibility tree yields nothing readable — canvas apps,
terminals, some Electron builds. Two things keep this cheap:

  * the capture is cropped to the frontmost window, not the whole desktop, so a
    three-monitor setup does not turn into a panorama; and
  * a perceptual hash gates it, so an idle screen never reaches the model.
"""

import io

import numpy as np
import Quartz
from PIL import Image

MAX_DIM = 1280          # plenty for reading text, small enough to stay cheap
JPEG_QUALITY = 72
HASH_SIZE = 8           # dHash grid -> 64-bit fingerprint


def frontmost_window() -> dict:
    """Frontmost normal window not owned by us, including its window id."""
    import os
    own_pid = os.getpid()
    try:
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
    except Exception:
        return {}
    for w in wins or []:
        if w.get("kCGWindowLayer") != 0 or w.get("kCGWindowOwnerPID") == own_pid:
            continue
        b = w.get("kCGWindowBounds", {})
        if b.get("Width", 0) < 120 or b.get("Height", 0) < 80:
            continue
        return {
            "id": w.get("kCGWindowNumber"),
            "app": w.get("kCGWindowOwnerName", "?"),
            "title": w.get("kCGWindowName") or "",
            "x": b.get("X", 0), "y": b.get("Y", 0),
            "w": b.get("Width", 0), "h": b.get("Height", 0),
        }
    return {}


def _cgimage_to_pil(img_ref):
    if img_ref is None:
        return None
    w = Quartz.CGImageGetWidth(img_ref)
    h = Quartz.CGImageGetHeight(img_ref)
    if not w or not h:
        return None
    bpr = Quartz.CGImageGetBytesPerRow(img_ref)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img_ref))
    arr = np.frombuffer(data, dtype=np.uint8)
    if arr.size < h * bpr:
        return None
    arr = arr[: h * bpr].reshape(h, bpr // 4, 4)[:, :w, :]
    return Image.fromarray(arr[:, :, [2, 1, 0]])       # BGRA -> RGB


def capture_active_window():
    """(PIL image, window meta) cropped to the frontmost window, or (None, {}).

    Needs Screen Recording permission; without it Quartz hands back an empty or
    black image rather than raising, so callers treat None as "no vision".
    """
    win = frontmost_window()
    if not win or win.get("id") is None:
        return None, {}
    try:
        img_ref = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            win["id"],
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        image = _cgimage_to_pil(img_ref)
    except Exception:
        return None, win
    return image, win


def encode_jpeg(image: Image.Image, max_dim: int = MAX_DIM) -> bytes:
    if image is None:
        return b""
    if max(image.size) > max_dim:
        scale = max_dim / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def dhash(image: Image.Image, size: int = HASH_SIZE) -> int:
    """Difference hash: compares each pixel with its right neighbour.

    Robust to the small rendering jitter (cursor blink, antialiasing) that a raw
    frame diff would flag on every poll.
    """
    if image is None:
        return 0
    small = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = np.asarray(small, dtype=np.int16)
    bits = pixels[:, 1:] > pixels[:, :-1]
    out = 0
    for bit in bits.flatten():
        out = (out << 1) | int(bit)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class FrameWatcher:
    """Reports a window capture only when it has meaningfully changed.

    `threshold` is in bits of a 64-bit hash: a blinking cursor moves 1-2, a line
    of new text moves considerably more.
    """

    def __init__(self, threshold: int = 8):
        self.threshold = threshold
        self._last_hash = None
        self._last_window = None

    def poll(self):
        """Returns (image, window) when the frame changed enough, else (None, meta)."""
        image, win = capture_active_window()
        if image is None:
            return None, win

        h = dhash(image)
        window_key = (win.get("id"), win.get("app"))
        switched = window_key != self._last_window
        distance = 64 if self._last_hash is None else hamming(h, self._last_hash)

        self._last_hash = h
        self._last_window = window_key

        # A window switch is not itself worth inspecting; wait for real activity.
        if switched:
            return None, win
        if distance < self.threshold:
            return None, win
        return image, win

    def reset(self):
        self._last_hash = None
        self._last_window = None
