"""
Accessibility perception for the Screen Sentinel.

Reads what the user is actually typing straight from the macOS accessibility
tree, so the common case never costs a screenshot or a vision token. Only when
AX yields nothing usable (canvas apps, terminals, some Electron builds) does the
Sentinel fall back to sentinel_vision.

Everything here is read-only, and every call degrades to a None rather than
raising: accessibility is permission-gated and any app can refuse an attribute
at any time.
"""

import os
import time

import objc
from ApplicationServices import (
    AXIsProcessTrustedWithOptions,
    AXUIElementCreateSystemWide,
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    kAXErrorSuccess,
)
from AppKit import NSWorkspace

# Attribute names are plain strings in the AX API.
AX_FOCUSED_APP = "AXFocusedApplication"
AX_FOCUSED_ELEMENT = "AXFocusedUIElement"
AX_VALUE = "AXValue"
AX_SELECTED_TEXT = "AXSelectedText"
AX_SELECTED_RANGE = "AXSelectedTextRange"
AX_ROLE = "AXRole"
AX_SUBROLE = "AXSubrole"
AX_TITLE = "AXTitle"
AX_DESCRIPTION = "AXDescription"

# Roles that can hold prose or code worth checking.
EDITABLE_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}

MAX_TEXT_CHARS = 8000       # nothing useful past this, and it keeps prompts small


def is_trusted(prompt: bool = False) -> bool:
    """Whether this process may read the accessibility tree.

    Without it every query returns nothing, so the Sentinel reports the reason
    rather than looking silently broken.
    """
    try:
        options = {"AXTrustedCheckOptionPrompt": bool(prompt)}
        return bool(AXIsProcessTrustedWithOptions(options))
    except Exception:
        return False


def _copy(element, attribute):
    """AXUIElementCopyAttributeValue as a plain optional getter."""
    if element is None:
        return None
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
        return value if err == kAXErrorSuccess else None
    except Exception:
        return None


def frontmost_app() -> dict:
    """Name + pid of the app the user is in, via NSWorkspace (no AX needed)."""
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return {}
        return {"name": str(app.localizedName() or "?"), "pid": int(app.processIdentifier())}
    except Exception:
        return {}


def focused_element():
    """The element with keyboard focus, or None."""
    system = AXUIElementCreateSystemWide()
    element = _copy(system, AX_FOCUSED_ELEMENT)
    if element is not None:
        return element
    # Some apps only expose focus through their own application element.
    app = frontmost_app()
    if app.get("pid"):
        return _copy(AXUIElementCreateApplication(app["pid"]), AX_FOCUSED_ELEMENT)
    return None


def read_focus() -> dict:
    """A snapshot of the focused editable field.

    Returns {app, pid, role, title, text, selection, editable} — text is None
    when the element will not give it up, which is the cue to fall back to
    vision rather than to guess.
    """
    app = frontmost_app()
    snapshot = {
        "app": app.get("name", "?"),
        "pid": app.get("pid"),
        "role": None,
        "title": None,
        "text": None,
        "selection": None,
        "editable": False,
    }

    element = focused_element()
    if element is None:
        return snapshot

    role = _copy(element, AX_ROLE)
    snapshot["role"] = str(role) if role else None
    title = _copy(element, AX_TITLE) or _copy(element, AX_DESCRIPTION)
    snapshot["title"] = str(title) if title else None

    value = _copy(element, AX_VALUE)
    if isinstance(value, str):
        snapshot["text"] = value[:MAX_TEXT_CHARS]
    selected = _copy(element, AX_SELECTED_TEXT)
    if isinstance(selected, str) and selected:
        snapshot["selection"] = selected[:MAX_TEXT_CHARS]

    snapshot["editable"] = bool(
        snapshot["role"] in EDITABLE_ROLES
        or (snapshot["text"] is not None and snapshot["role"] not in ("AXStaticText",))
    )
    return snapshot


class TypingWatcher:
    """Fires once after the user pauses typing.

    Polling the focused value is cheap; the point is to notice a *pause*. A
    change arms the watcher, and `poll()` reports the settled text only after
    `debounce` seconds without further change — so a burst of typing produces
    one inspection, not one per keystroke.
    """

    def __init__(self, debounce: float = 2.0, min_chars: int = 12):
        self.debounce = debounce
        self.min_chars = min_chars
        self._last_text = None
        self._last_change = 0.0
        self._armed = False
        self._reported = None

    def poll(self, snapshot: dict):
        """Feed a read_focus() snapshot. Returns the snapshot when it settles."""
        text = snapshot.get("text")
        if text is None or not snapshot.get("editable"):
            self._armed = False
            self._last_text = None
            return None

        now = time.monotonic()
        if text != self._last_text:
            self._last_text = text
            self._last_change = now
            self._armed = True
            return None

        if not self._armed:
            return None
        if now - self._last_change < self.debounce:
            return None

        self._armed = False
        if len(text.strip()) < self.min_chars:
            return None
        if text == self._reported:       # already looked at this exact text
            return None
        self._reported = text
        return snapshot

    def forget(self):
        """Allow the current text to be re-examined (after a dismissed hint)."""
        self._reported = None
