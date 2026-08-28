"""
Reads what the user is looking at, so a spoken instruction has something to act on.

Everything here is read-only and every call degrades to None rather than raising:
accessibility is permission-gated and any application may refuse an attribute at
any moment.
"""

from ApplicationServices import (
    AXIsProcessTrustedWithOptions,
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    kAXErrorSuccess,
)
from AppKit import NSWorkspace

import text_injector

AX_FOCUSED_ELEMENT = "AXFocusedUIElement"
AX_VALUE = "AXValue"
AX_SELECTED_TEXT = "AXSelectedText"
AX_SELECTED_RANGE = "AXSelectedTextRange"
AX_ROLE = "AXRole"
AX_TITLE = "AXTitle"
AX_DESCRIPTION = "AXDescription"

MAX_CONTEXT_CHARS = 12000

# Apps whose "buffer" is a shell transcript rather than an editable document.
TERMINAL_APPS = ("terminal", "iterm", "warp", "ghostty", "alacritty", "kitty", "wezterm")
CODE_APPS = ("code", "cursor", "xcode", "sublime", "zed", "intellij", "pycharm", "webstorm", "nova")
MESSAGE_APPS = ("mail", "slack", "messages", "outlook", "spark", "superhuman", "discord", "teams")


def is_trusted(prompt: bool = False) -> bool:
    """Whether this process may read the accessibility tree."""
    try:
        return bool(AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": bool(prompt)}))
    except Exception:
        return False


def _copy(element, attribute):
    if element is None:
        return None
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
        return value if err == kAXErrorSuccess else None
    except Exception:
        return None


def frontmost_app() -> dict:
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return {}
        return {"name": str(app.localizedName() or "?"),
                "pid": int(app.processIdentifier())}
    except Exception:
        return {}


def classify(app_name: str) -> str:
    """Which house style the transformation should follow."""
    lowered = (app_name or "").lower()
    if any(k in lowered for k in TERMINAL_APPS):
        return "terminal"
    if any(k in lowered for k in CODE_APPS):
        return "code"
    if any(k in lowered for k in MESSAGE_APPS):
        return "message"
    return "text"


# Apps whose editors publish nothing over accessibility. Detected by content
# rather than by name where possible, since the list is not closed.
AX_WITHHELD_HINT = "not accessible at this time"


def get_focused_window_context() -> dict:
    """A snapshot of the focused editable element.

    `element` is the live AX handle and is deliberately kept out of anything
    that gets serialised — the injector needs it later, by which point focus may
    have moved.
    """
    app = frontmost_app()
    ctx = {
        "app_name": app.get("name", "?"),
        "pid": app.get("pid"),
        "kind": classify(app.get("name", "")),
        "role": None,
        "title": None,
        "selected_text": None,
        "full_text": None,
        "has_selection": False,
        "editable": False,
        "element": None,
        "trusted": is_trusted(),
    }
    if not ctx["pid"] or not ctx["trusted"]:
        return ctx

    app_ref = AXUIElementCreateApplication(ctx["pid"])
    element = _copy(app_ref, AX_FOCUSED_ELEMENT)
    if element is None:
        return ctx
    ctx["element"] = element

    role = _copy(element, AX_ROLE)
    ctx["role"] = str(role) if role else None
    title = _copy(element, AX_TITLE) or _copy(element, AX_DESCRIPTION)
    ctx["title"] = str(title) if title else None

    selected = _copy(element, AX_SELECTED_TEXT)
    if isinstance(selected, str) and selected.strip():
        ctx["selected_text"] = selected[:MAX_CONTEXT_CHARS]
        ctx["has_selection"] = True

    value = _copy(element, AX_VALUE)
    if isinstance(value, str):
        ctx["full_text"] = value[:MAX_CONTEXT_CHARS]
        ctx["editable"] = True

    # Monaco (VS Code, Cursor) and some other canvas-drawn editors answer every
    # text attribute with an empty string. Without this, the agent receives no
    # context at all and invents something to write into the user's document.
    if not ctx["has_selection"] and not (ctx["full_text"] or "").strip():
        ctx["ax_withheld"] = AX_WITHHELD_HINT in (ctx["title"] or "").lower() \
            or ctx["role"] in ("AXTextArea", "AXTextField", "AXWebArea")
        if ctx["ax_withheld"]:
            copied = text_injector.copy_selection()
            if copied and copied.strip():
                ctx["selected_text"] = copied[:MAX_CONTEXT_CHARS]
                ctx["has_selection"] = True
                ctx["via_clipboard"] = True

    return ctx


def context_for_model(ctx: dict) -> str:
    """The slice of the window the model should actually reason over.

    A selection is an explicit instruction about scope, so it wins. Otherwise
    the buffer is trimmed to its tail — the end of a document or a terminal
    transcript is what the user is working at, and sending 12k characters of
    history costs latency for nothing.
    """
    if ctx.get("has_selection"):
        return ctx["selected_text"]
    text = ctx.get("full_text") or ""
    if len(text) > 4000:
        return "…" + text[-4000:]
    return text
