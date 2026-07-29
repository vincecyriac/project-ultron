"""
sentry_action.py - Desktop UI automation module for Project Ultron.

Mouse events are posted through Quartz CGEvent so they work across ALL
monitors in global desktop coordinates (PyAutoGUI clamps to the primary
display). Keyboard input still uses PyAutoGUI.

Coordinate convention: the AI reports positions in normalized 0-1000
space relative to the LAST SCREENSHOT it saw. sentry_vision records the
global desktop rectangle of every capture (LAST_CAPTURE_BOUNDS), and
clicks are mapped through it — so a click lands on whichever monitor the
screenshot showed.
"""

import time
import subprocess
import Quartz
import pyautogui

import sentry_vision

pyautogui.FAILSAFE = False  # mouse handled via Quartz; failsafe corner irrelevant
pyautogui.PAUSE = 0.05

_BUTTON_MAP = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
             Quartz.kCGEventLeftMouseDragged, Quartz.kCGMouseButtonLeft),
    "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
              Quartz.kCGEventRightMouseDragged, Quartz.kCGMouseButtonRight),
    "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
               Quartz.kCGEventOtherMouseDragged, Quartz.kCGMouseButtonCenter),
}


def _denormalize(x: int, y: int) -> tuple:
    """Maps normalized 0-1000 coords onto the global desktop rect of the last screenshot."""
    bounds = sentry_vision.LAST_CAPTURE_BOUNDS
    if bounds is None:
        d = sentry_vision.get_active_display()
        bounds = (d["x"], d["y"], d["w"], d["h"])
    bx, by, bw, bh = bounds
    px = bx + (max(0, min(1000, x)) / 1000.0) * bw
    py = by + (max(0, min(1000, y)) / 1000.0) * bh
    return float(px), float(py)


def _post(event_type, px: float, py: float, button=Quartz.kCGMouseButtonLeft, click_state: int = 0):
    ev = Quartz.CGEventCreateMouseEvent(None, event_type, (px, py), button)
    if click_state:
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, click_state)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _move_cursor(px: float, py: float):
    _post(Quartz.kCGEventMouseMoved, px, py)


def get_screen_info() -> str:
    mx, my = sentry_vision.get_mouse_position()
    return f"{sentry_vision.describe_displays()}\nMouse at global ({int(mx)},{int(my)})."


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Clicks at a normalized (0-1000) coordinate of the last screenshot's display area."""
    try:
        px, py = _denormalize(x, y)
        down, up, _, btn = _BUTTON_MAP.get(button, _BUTTON_MAP["left"])
        clicks = max(1, min(3, int(clicks)))
        _move_cursor(px, py)
        time.sleep(0.08)
        for c in range(1, clicks + 1):
            _post(down, px, py, btn, click_state=c)
            time.sleep(0.03)
            _post(up, px, py, btn, click_state=c)
            time.sleep(0.06)
        return f"Clicked {button} x{clicks} at normalized ({x},{y}) -> global ({int(px)},{int(py)})."
    except Exception as e:
        return f"[Error]: Click failed: {e}"


def move_mouse(x: int, y: int) -> str:
    """Moves the mouse pointer to a normalized (0-1000) coordinate without clicking."""
    try:
        px, py = _denormalize(x, y)
        _move_cursor(px, py)
        return f"Mouse moved to normalized ({x},{y}) -> global ({int(px)},{int(py)})."
    except Exception as e:
        return f"[Error]: Mouse move failed: {e}"


def drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """Drags with the left button between two normalized coordinates."""
    try:
        sx, sy = _denormalize(x1, y1)
        ex, ey = _denormalize(x2, y2)
        down, up, dragged, btn = _BUTTON_MAP["left"]
        _move_cursor(sx, sy)
        time.sleep(0.1)
        _post(down, sx, sy, btn, click_state=1)
        steps = 12
        for i in range(1, steps + 1):
            ix = sx + (ex - sx) * i / steps
            iy = sy + (ey - sy) * i / steps
            _post(dragged, ix, iy, btn)
            time.sleep(0.02)
        _post(up, ex, ey, btn, click_state=1)
        return f"Dragged from global ({int(sx)},{int(sy)}) to ({int(ex)},{int(ey)})."
    except Exception as e:
        return f"[Error]: Drag failed: {e}"


def scroll(amount: int, x: int = None, y: int = None) -> str:
    """Scrolls vertically. Positive = up, negative = down. Optional normalized position first."""
    try:
        if x is not None and y is not None:
            px, py = _denormalize(x, y)
            _move_cursor(px, py)
            time.sleep(0.05)
        remaining = int(amount)
        step = 5 if remaining > 0 else -5
        while remaining != 0:
            delta = step if abs(remaining) >= abs(step) else remaining
            ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, delta)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            remaining -= delta
            time.sleep(0.02)
        return f"Scrolled {amount} units."
    except Exception as e:
        return f"[Error]: Scroll failed: {e}"


def type_text(text: str, press_enter: bool = False) -> str:
    """Types text into the currently focused UI element."""
    try:
        pyautogui.typewrite(text, interval=0.02)
        if press_enter:
            pyautogui.press("enter")
        return f"Typed {len(text)} characters" + (" and pressed Enter." if press_enter else ".")
    except Exception as e:
        return f"[Error]: Typing failed: {e}"


def press_keys(keys: list) -> str:
    """Presses a key combo simultaneously, e.g. ['command','c'] or single key ['enter']."""
    try:
        keys = [k.strip().lower() for k in keys if k and isinstance(k, str)]
        if not keys:
            return "[Error]: No keys provided."
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return f"Pressed keys: {'+'.join(keys)}."
    except Exception as e:
        return f"[Error]: Key press failed: {e}"


_UI_TREE_SCRIPT = '''
on elemInfo(elem)
    tell application "System Events"
        set elemRole to ""
        set elemName to ""
        set elemPos to ""
        try
            set elemRole to role description of elem
        end try
        try
            set elemName to name of elem
        end try
        try
            set p to position of elem
            set s to size of elem
            set elemPos to ((item 1 of p) as string) & "," & ((item 2 of p) as string) & " " & ((item 1 of s) as string) & "x" & ((item 2 of s) as string)
        end try
        if elemName is missing value then set elemName to ""
        return elemRole & " | " & elemName & " | " & elemPos
    end tell
end elemInfo

tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    set output to "Frontmost app: " & appName & linefeed
    try
        set frontWin to front window of frontApp
        set output to output & "Window: " & (name of frontWin) & linefeed & "--- UI Elements (role | name | x,y wxh) ---" & linefeed
        set elems to entire contents of frontWin
        set maxCount to 120
        set n to 0
        repeat with e in elems
            if n >= maxCount then exit repeat
            set info to my elemInfo(e)
            set output to output & info & linefeed
            set n to n + 1
        end repeat
    on error errMsg
        set output to output & "(No accessible window: " & errMsg & ")"
    end try
    return output
end tell
'''


def read_ui_elements() -> str:
    """Reads the accessibility UI tree of the frontmost app: element roles, names, positions.
    Positions are GLOBAL screen pixels (CG coordinates, same space as click mapping)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", _UI_TREE_SCRIPT],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0 or not result.stdout.strip():
            err = result.stderr.strip()
            return (f"[Error]: Could not read UI tree: {err or 'empty output'}. "
                    "Verify Accessibility permission is granted to the terminal/app running Ultron.")
        out = result.stdout.strip()
        if len(out) > 6000:
            out = out[:6000] + "\n...[truncated]"
        return (f"{out}\n{sentry_vision.describe_displays()}\n"
                "(Element positions are global pixels. To click one, use click_at_pixel semantics: "
                "capture the screen containing it, then convert (pixel - capture origin) / capture size * 1000.)")
    except subprocess.TimeoutExpired:
        return "[Error]: UI tree read timed out."
    except Exception as e:
        return f"[Error]: UI tree read failed: {e}"


if __name__ == "__main__":
    print(get_screen_info())
