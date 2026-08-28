"""
Writes transformed text back where the user is typing.

Two strategies, in order of politeness:

  1. Accessibility, and only to replace a selection: AXSelectedText swaps the
     highlighted run exactly, with no clipboard involvement and no synthetic
     keystrokes.
  2. Clipboard + Cmd-V. Works in the many apps that expose their text through
     AX but refuse writes — Electron editors and Mail among them. The previous
     clipboard contents are restored afterwards.

Strategy 1 fails silently in more apps than its API surface suggests, which is
why the fallback is not optional.
"""

import time

import Quartz
from AppKit import NSPasteboard, NSPasteboardTypeString, NSWorkspace
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementSetAttributeValue,
    AXUIElementIsAttributeSettable,
    kAXErrorSuccess,
)

AX_FOCUSED_ELEMENT = "AXFocusedUIElement"
AX_SELECTED_TEXT = "AXSelectedText"
AX_VALUE = "AXValue"

KEY_CMD = 0x37
KEY_V = 0x09
KEY_C = 0x08
PASTE_SETTLE_S = 0.12
CLIPBOARD_RESTORE_S = 0.35


def _copy(element, attribute):
    if element is None:
        return None
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
        return value if err == kAXErrorSuccess else None
    except Exception:
        return None


def _settable(element, attribute) -> bool:
    try:
        err, ok = AXUIElementIsAttributeSettable(element, attribute, None)
        return err == kAXErrorSuccess and bool(ok)
    except Exception:
        return False


def focused_element(pid: int = None):
    if not pid:
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            pid = int(app.processIdentifier()) if app else None
        except Exception:
            pid = None
    if not pid:
        return None
    return _copy(AXUIElementCreateApplication(pid), AX_FOCUSED_ELEMENT)


def _press_command(keycode: int):
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for key, down in ((KEY_CMD, True), (keycode, True), (keycode, False), (KEY_CMD, False)):
        event = Quartz.CGEventCreateKeyboardEvent(source, key, down)
        if key == keycode:
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.008)


def copy_selection(preserve_clipboard: bool = True):
    """Read the focused app's selection by pressing Cmd-C.

    The last resort for reading, needed because some editors refuse to publish
    their text over accessibility at all — VS Code's Monaco answers every text
    attribute with an empty string and puts "The editor is not accessible at
    this time" in its title unless screen-reader mode is switched on.

    Returns the selected text, or None when nothing was selected. The pasteboard's
    changeCount is what distinguishes "copied an empty selection" from "Cmd-C did
    nothing", since an empty selection leaves the clipboard untouched.
    """
    pb = NSPasteboard.generalPasteboard()
    previous = pb.stringForType_(NSPasteboardTypeString) if preserve_clipboard else None
    before = pb.changeCount()

    try:
        _press_command(KEY_C)
    except Exception:
        return None

    text = None
    for _ in range(12):                      # up to ~360ms for the app to answer
        time.sleep(0.03)
        if pb.changeCount() != before:
            text = pb.stringForType_(NSPasteboardTypeString)
            break

    if preserve_clipboard and previous is not None and pb.changeCount() != before:
        pb.clearContents()
        pb.setString_forType_(previous, NSPasteboardTypeString)
    return text


def inject_via_accessibility(element, text: str, replacing_selection: bool) -> bool:
    """Write through AX. Returns False whenever the app will not take it."""
    if element is None:
        return False

    if replacing_selection and _settable(element, AX_SELECTED_TEXT):
        try:
            if AXUIElementSetAttributeValue(element, AX_SELECTED_TEXT, text) == kAXErrorSuccess:
                return True
        except Exception:
            pass

    # With no selection there is deliberately no AX path. Writing AXValue would
    # mean read-append-write, which appends at the END of the buffer rather than
    # at the caret, and which bakes a field's placeholder string into its real
    # value when the field is empty (observed in Electron apps). Paste is both
    # more correct and more widely supported here.
    return False


def inject_via_paste(text: str, preserve_clipboard: bool = True) -> bool:
    """Put the text on the clipboard and press Cmd-V at the focused app."""
    pb = NSPasteboard.generalPasteboard()
    previous = pb.stringForType_(NSPasteboardTypeString) if preserve_clipboard else None

    pb.clearContents()
    if not pb.setString_forType_(text, NSPasteboardTypeString):
        return False

    try:
        source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        for key, down in ((KEY_CMD, True), (KEY_V, True), (KEY_V, False), (KEY_CMD, False)):
            event = Quartz.CGEventCreateKeyboardEvent(source, key, down)
            if key == KEY_V:
                Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.008)
    except Exception:
        return False

    if preserve_clipboard and previous is not None:
        # The paste has to land before the clipboard is handed back.
        time.sleep(CLIPBOARD_RESTORE_S)
        pb.clearContents()
        pb.setString_forType_(previous, NSPasteboardTypeString)
    return True


def inject_text(text: str, element=None, pid: int = None,
                replacing_selection: bool = False,
                preserve_clipboard: bool = True) -> tuple:
    """Insert `text` at the cursor. Returns (ok, how) where how is 'accessibility',
    'paste' or a reason it failed."""
    if not text:
        return False, "nothing to insert"

    target = element if element is not None else focused_element(pid)
    if inject_via_accessibility(target, text, replacing_selection):
        return True, "accessibility"

    time.sleep(PASTE_SETTLE_S)
    if inject_via_paste(text, preserve_clipboard):
        return True, "paste"
    return False, "the app refused both accessibility and paste"
