"""
Global push-to-talk key, implemented with a Quartz event tap.

The tap runs its own CFRunLoop on a dedicated thread, because the hub itself
runs in a daemon thread under app_desktop.py and never owns the main run loop.
The chord is swallowed rather than passed through, so holding it does not type
a space into whatever the user is focused on.
"""

import threading

import Quartz

# Cmd-Shift-Space. Space is the only key in the chord we have to match.
KEY_SPACE = 0x31
REQUIRED_FLAGS = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
# Caps lock and numeric-keypad bits ride along on ordinary keystrokes.
IGNORED_FLAGS = Quartz.kCGEventFlagMaskAlphaShift | Quartz.kCGEventFlagMaskNumericPad

HOTKEY_LABEL = "⌘⇧Space"


class HotkeyManager:
    """Calls on_press when the chord goes down and on_release when it comes up."""

    def __init__(self, on_press, on_release, keycode: int = KEY_SPACE,
                 required_flags: int = REQUIRED_FLAGS):
        self.on_press = on_press
        self.on_release = on_release
        self.keycode = keycode
        self.required_flags = required_flags
        self._held = False
        self._thread = None
        self._runloop = None
        self._tap = None
        self._ready = threading.Event()
        self.failure = None

    # -- tap ---------------------------------------------------------------
    def _matches(self, event) -> bool:
        if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode) != self.keycode:
            return False
        flags = Quartz.CGEventGetFlags(event) & ~IGNORED_FLAGS
        modifiers = flags & (Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
                             | Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate)
        return modifiers == self.required_flags

    def _callback(self, proxy, event_type, event, refcon):
        # macOS disables a tap that is too slow or that the user revokes
        # permission for; re-enabling is the documented recovery.
        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            if self._tap:
                Quartz.CGEventTapEnable(self._tap, True)
            return event

        try:
            if event_type == Quartz.kCGEventKeyDown and self._matches(event):
                if not self._held:          # ignore auto-repeat while held
                    self._held = True
                    self._safely(self.on_press)
                return None                 # swallow it
            if event_type == Quartz.kCGEventKeyUp and self._held \
                    and Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode) == self.keycode:
                self._held = False
                self._safely(self.on_release)
                return None
            # Releasing Cmd or Shift before Space arrives as flagsChanged, and
            # the KeyUp that follows may no longer carry the modifiers.
            if event_type == Quartz.kCGEventFlagsChanged and self._held:
                flags = Quartz.CGEventGetFlags(event)
                if (flags & self.required_flags) != self.required_flags:
                    self._held = False
                    self._safely(self.on_release)
        except Exception:
            self._held = False
        return event

    def _safely(self, fn):
        try:
            fn()
        except Exception:
            pass

    # -- lifecycle ---------------------------------------------------------
    def _run(self):
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault, mask, self._callback, None)
        if not self._tap:
            self.failure = "accessibility permission is required for the push-to-talk key"
            self._ready.set()
            return

        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._runloop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._runloop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self._ready.set()
        Quartz.CFRunLoopRun()

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ultron-hotkey")
        self._thread.start()
        # Wait for the tap to exist, not for the thread to finish — it runs a
        # CFRunLoop and never finishes. Joining here stalled every launch by a
        # full second.
        self._ready.wait(timeout=2.0)
        return self.failure is None

    def stop(self):
        if self._tap:
            try:
                Quartz.CGEventTapEnable(self._tap, False)
            except Exception:
                pass
        if self._runloop:
            try:
                Quartz.CFRunLoopStop(self._runloop)
            except Exception:
                pass
        self._thread = None
