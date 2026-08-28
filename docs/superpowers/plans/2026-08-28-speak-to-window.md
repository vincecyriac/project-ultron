# Speak-to-Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hold a global hotkey, speak an instruction, and have Gemini replace the selected text in whatever macOS app is frontmost — no clipboard juggling, no window switching, no conversation with the assistant.

**Architecture:** Six new modules imported by `ultron_hub.py`. A `CGEventTap` on a daemon thread detects push-to-talk; on key-down the frontmost app's Accessibility context is captured immediately and mic audio is teed away from Gemini Live; on key-up the WAV plus context goes to `gemini-3.7-flash` in a single call, and the reply is injected by simulated paste (falling back to a direct AX write). A native floating `NSPanel` shows state over whatever app is in front, and the same state is mirrored into the Ultron window over the existing WebSocket.

**Tech Stack:** Python 3.13, PyObjC (`ApplicationServices`, `AppKit`, `Quartz`, `libdispatch`), `google-genai`, `pytest`. **No new runtime dependency** — every native symbol used is already available through the installed `pyobjc-framework-Quartz` / `pyobjc-framework-Cocoa`.

**Spec:** [`docs/superpowers/specs/2026-08-28-speak-to-window-design.md`](../specs/2026-08-28-speak-to-window-design.md)

## Global Constraints

- **Branch:** all work happens on `feature/speak-to-window-automation`. It already exists and is checked out.
- **Do not create git commits.** The user's `CLAUDE.md` forbids it without exception. The standard plan template ends each task with a commit step; those are replaced here by a **Checkpoint** step. Leave all work in the working tree.
- **`kAX*` constants come from `ApplicationServices`, never `Cocoa`.** Verified in this venv: `Cocoa.kAXFocusedUIElementAttribute` raises `AttributeError`.
- **Secure fields are refused before any text is read.** Focused role `AXSecureTextField` → return `blocked: True` and capture nothing.
- **No injected text ever ends in a newline.** In a terminal that is the difference between offering a command and running it.
- **The clipboard is restored on every path**, including exceptions, via `try/finally`.
- **Context sent to Gemini is windowed:** ±2000 chars around the caret, 4000 cap.
- **Default hotkey `cmd+shift+space`**, overridable via `ULTRON_PTT_HOTKEY` in `.env`.
- **Do not fix the `mic_audio_buffer` double-feed** (`ultron_hub.py:360` and `ultron_hub.py:541`). It is real, it is noted in the spec, and it is explicitly out of scope.
- Match the surrounding code's style: module docstring explaining *why*, guarded native calls, `[Error]:`-prefixed failure strings.

---

## File Structure

| File | Responsibility |
|---|---|
| `window_context.py` | **Create.** Read frontmost app + focused element via AX. Secure-field refusal, caret-windowed buffer, caret rect. |
| `text_injector.py` | **Create.** Paste-first injection, AX fallback, pasteboard save/restore. |
| `speak_to_window_agent.py` | **Create.** Prompt assembly, one Gemini call with inline audio, response cleaning. |
| `hotkey_manager.py` | **Create.** Binding parser + `CGEventTap` thread. Knows nothing about audio or Gemini. |
| `native_hud.py` | **Create.** Floating `NSPanel` status pill, AX→Cocoa coordinate flip, headless degradation. |
| `speak_to_window.py` | **Create.** Coordinator wiring capture → audio → transform → inject → HUD. All collaborators injected, so the whole flow is testable with fakes. |
| `ultron_hub.py` | **Modify.** PTT flag gating both mic producers, audio tee, start/stop wiring, logging + broadcast. |
| `web_gui/app.js`, `web_gui/style.css` | **Modify.** `window_action` pill mirroring the four states. |
| `tests/` | **Create.** pytest suite for every non-native unit. |
| `test_speak_to_window.py` | **Create.** Manual smoke harness for the layers that need a real machine. |

**One deviation from the spec's module list:** the spec names five modules plus "hub integration". This plan adds a sixth, `speak_to_window.py`, to hold the coordination the spec's flow diagram describes. `ultron_hub.py` is already ~2000 lines; putting the orchestration there would grow the largest file in the project and make the flow untestable without native calls. Hub changes stay limited to the audio tee, the flag, and wiring.

---

### Task 1: `window_context.py` — Accessibility capture

**Files:**
- Create: `window_context.py`
- Create: `tests/__init__.py`, `tests/test_window_context.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `window_around_caret(text: str, caret: int, radius: int = 2000, cap: int = 4000) -> str`
  - `build_context(app_name, pid, bundle_id, role, selected, full_text, caret_offset, caret_rect) -> dict`
  - `get_focused_window_context() -> dict`
  - Context dict keys: `app_name, pid, bundle_id, role, selected_text, context_text, has_selection, caret_rect, blocked`
  - `SECURE_ROLES: set[str]`, `CARET_MARKER: str`

- [ ] **Step 1: Add pytest and create the test package**

```bash
printf 'pytest\n' >> requirements.txt
mkdir -p tests && touch tests/__init__.py
.venv/bin/pip install pytest
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_window_context.py`:

```python
import window_context as wc


def test_short_text_returned_whole_without_marker_loss():
    out = wc.window_around_caret("hello world", 5)
    assert "hello world" in out
    assert wc.CARET_MARKER in out


def test_marker_sits_at_the_caret_offset():
    out = wc.window_around_caret("abcdef", 3)
    assert out == "abc" + wc.CARET_MARKER + "def"


def test_caret_at_start_and_end():
    assert wc.window_around_caret("abc", 0) == wc.CARET_MARKER + "abc"
    assert wc.window_around_caret("abc", 3) == "abc" + wc.CARET_MARKER


def test_long_buffer_is_windowed_around_the_caret():
    text = ("a" * 5000) + "NEEDLE" + ("b" * 5000)
    out = wc.window_around_caret(text, 5000, radius=100, cap=400)
    assert "NEEDLE" in out
    assert len(out) <= 400 + len(wc.CARET_MARKER)
    assert "a" * 5000 not in out


def test_cap_is_respected_even_with_a_large_radius():
    text = "x" * 20000
    out = wc.window_around_caret(text, 10000, radius=9000, cap=1000)
    assert len(out) <= 1000 + len(wc.CARET_MARKER)


def test_caret_offset_out_of_range_is_clamped():
    assert wc.window_around_caret("abc", 99) == "abc" + wc.CARET_MARKER
    assert wc.window_around_caret("abc", -5) == wc.CARET_MARKER + "abc"


def test_secure_field_is_blocked_and_carries_no_text():
    ctx = wc.build_context(
        app_name="Safari", pid=1, bundle_id="com.apple.Safari",
        role="AXSecureTextField", selected="hunter2", full_text="hunter2",
        caret_offset=0, caret_rect=None,
    )
    assert ctx["blocked"] is True
    assert ctx["selected_text"] is None
    assert ctx["context_text"] is None
    assert ctx["has_selection"] is False


def test_selection_wins_over_buffer():
    ctx = wc.build_context(
        app_name="Mail", pid=2, bundle_id="com.apple.mail", role="AXTextArea",
        selected="pick me", full_text="a much longer body of text",
        caret_offset=3, caret_rect=None,
    )
    assert ctx["has_selection"] is True
    assert ctx["selected_text"] == "pick me"


def test_whitespace_only_selection_is_not_a_selection():
    ctx = wc.build_context(
        app_name="Mail", pid=2, bundle_id=None, role="AXTextArea",
        selected="   \n ", full_text="body", caret_offset=0, caret_rect=None,
    )
    assert ctx["has_selection"] is False


def test_no_selection_falls_back_to_caret_window():
    ctx = wc.build_context(
        app_name="Code", pid=3, bundle_id="com.microsoft.VSCode",
        role="AXTextArea", selected="", full_text="abcdef",
        caret_offset=3, caret_rect=None,
    )
    assert ctx["has_selection"] is False
    assert ctx["context_text"] == "abc" + wc.CARET_MARKER + "def"


def test_empty_element_is_a_valid_context_not_an_error():
    ctx = wc.build_context(
        app_name="Finder", pid=4, bundle_id=None, role=None,
        selected=None, full_text=None, caret_offset=None, caret_rect=None,
    )
    assert ctx["blocked"] is False
    assert ctx["app_name"] == "Finder"
    assert ctx["context_text"] is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_window_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'window_context'`

- [ ] **Step 4: Write `window_context.py`**

The native probes below were run against this machine and confirmed:
`AXUIElementCopyAttributeValue(elem, attr, None)` returns `(err, value)`;
`AXValueGetValue(rng, kAXValueCFRangeType, None)` returns `(True, (location, length))`;
`AXUIElementCopyParameterizedAttributeValue(elem, kAXBoundsForRangeParameterizedAttribute, rng, None)` returns `(err, AXValueRef)` which unwraps with `kAXValueCGRectType`.

```python
"""
window_context.py - Accessibility capture of the frontmost app for Speak-to-Window.

Reads what the user has selected (or the text around their caret) in whatever
application is in front, so a spoken instruction can be applied to it.

Two rules govern everything here:

  * Secure fields are refused outright. A focused AXSecureTextField is a
    password box; its contents must never reach a model. The role is checked
    before any text is read, not after.
  * The buffer is windowed, never dumped. A focused editor's kAXValue can be an
    entire file; sending it would cost latency, tokens and privacy for nothing.

Note for anyone extending this: the kAX* constants live on ApplicationServices,
NOT on Cocoa. Cocoa.kAXFocusedUIElementAttribute is an AttributeError.
"""

import ApplicationServices as AS
from AppKit import NSWorkspace

CONTEXT_RADIUS = 2000
CONTEXT_CAP = 4000
CARET_MARKER = "⟨CARET⟩"

# Focused roles whose contents we refuse to read at all.
SECURE_ROLES = {"AXSecureTextField"}


def window_around_caret(text: str, caret: int,
                        radius: int = CONTEXT_RADIUS,
                        cap: int = CONTEXT_CAP) -> str:
    """Text around the caret, marked, bounded by both radius and cap.

    The marker tells the model where the cursor sits, which is what makes
    "finish this sentence" mean anything.
    """
    if not text:
        return CARET_MARKER
    caret = max(0, min(len(text), int(caret or 0)))

    half = min(radius, max(1, cap // 2))
    start = max(0, caret - half)
    end = min(len(text), caret + half)

    # Cap governs the total; trim the far side first so the caret stays centred.
    while (end - start) > cap:
        if (caret - start) >= (end - caret):
            start += 1
        else:
            end -= 1

    return text[start:caret] + CARET_MARKER + text[caret:end]


def build_context(app_name, pid, bundle_id, role, selected, full_text,
                  caret_offset, caret_rect) -> dict:
    """Assemble the context dict. Pure — all native reads happen in the caller."""
    blocked = role in SECURE_ROLES
    if blocked:
        return {
            "app_name": app_name, "pid": pid, "bundle_id": bundle_id,
            "role": role, "selected_text": None, "context_text": None,
            "has_selection": False, "caret_rect": caret_rect, "blocked": True,
        }

    has_selection = bool(selected and selected.strip())
    if has_selection:
        context_text = selected
    elif full_text:
        context_text = window_around_caret(full_text, caret_offset or 0)
    else:
        context_text = None

    return {
        "app_name": app_name, "pid": pid, "bundle_id": bundle_id, "role": role,
        "selected_text": selected if has_selection else None,
        "context_text": context_text, "has_selection": has_selection,
        "caret_rect": caret_rect, "blocked": False,
    }


def _ax(element, attribute):
    """One guarded AX read. Returns None rather than raising or leaking codes."""
    try:
        err, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        return value if err == 0 else None
    except Exception:
        return None


def _selection_range(element):
    """(location, length) of the selection, or None."""
    raw = _ax(element, AS.kAXSelectedTextRangeAttribute)
    if raw is None:
        return None
    try:
        ok, rng = AS.AXValueGetValue(raw, AS.kAXValueCFRangeType, None)
        return tuple(rng) if ok else None
    except Exception:
        return None


def _caret_rect(element):
    """Screen rect of the caret in AX space (global, top-left origin)."""
    raw = _ax(element, AS.kAXSelectedTextRangeAttribute)
    if raw is None:
        return None
    try:
        err, bounds = AS.AXUIElementCopyParameterizedAttributeValue(
            element, AS.kAXBoundsForRangeParameterizedAttribute, raw, None)
        if err != 0 or bounds is None:
            return None
        ok, rect = AS.AXValueGetValue(bounds, AS.kAXValueCGRectType, None)
        if not ok:
            return None
        return (float(rect.origin.x), float(rect.origin.y),
                float(rect.size.width), float(rect.size.height))
    except Exception:
        return None


def get_focused_window_context() -> dict:
    """Context for the frontmost app's focused element.

    Never raises. An app that answers nothing yields a context with app_name
    set and text fields None — a valid result meaning "generate fresh text
    here", not a failure.
    """
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return build_context(None, None, None, None, None, None, None, None)

    app_name = str(front.localizedName() or "")
    pid = int(front.processIdentifier())
    bundle_id = str(front.bundleIdentifier() or "") or None

    try:
        app_ref = AS.AXUIElementCreateApplication(pid)
        element = _ax(app_ref, AS.kAXFocusedUIElementAttribute)
    except Exception:
        element = None

    if element is None:
        return build_context(app_name, pid, bundle_id, None, None, None, None, None)

    role = _ax(element, AS.kAXRoleAttribute)
    role = str(role) if role is not None else None

    # Role gate FIRST: nothing is read out of a secure field.
    if role in SECURE_ROLES:
        return build_context(app_name, pid, bundle_id, role,
                             None, None, None, _caret_rect(element))

    selected = _ax(element, AS.kAXSelectedTextAttribute)
    full_text = _ax(element, AS.kAXValueAttribute)
    rng = _selection_range(element)

    return build_context(
        app_name, pid, bundle_id, role,
        str(selected) if selected is not None else None,
        str(full_text) if full_text is not None else None,
        rng[0] if rng else None,
        _caret_rect(element),
    )


if __name__ == "__main__":
    import json
    ctx = get_focused_window_context()
    ctx.pop("caret_rect", None)
    print(json.dumps(ctx, indent=2, ensure_ascii=False)[:2000])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_window_context.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 6: Verify against a real app**

Focus a TextEdit window with some text selected, then run:
`.venv/bin/python window_context.py`
Expected: JSON naming TextEdit with `selected_text` matching the highlight and `has_selection: true`.

- [ ] **Step 7: Checkpoint** — leave changes in the working tree, do not commit.

---

### Task 2: `text_injector.py` — paste-first injection

**Files:**
- Create: `text_injector.py`
- Create: `tests/test_text_injector.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `strip_injectable(text: str) -> str`
  - `snapshot_pasteboard() -> list[tuple[str, bytes]]`
  - `restore_pasteboard(snapshot: list) -> None`
  - `paste_text(text: str, preserve_clipboard: bool = True) -> bool`
  - `ax_set_selected_text(text: str) -> bool`
  - `inject_text(text: str, preserve_clipboard: bool = True) -> tuple[bool, str]` — strategy is `"paste"`, `"ax"`, `"empty"`, or `"failed"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_text_injector.py`:

```python
import text_injector as ti


def test_trailing_newlines_are_stripped():
    assert ti.strip_injectable("echo hi\n") == "echo hi"
    assert ti.strip_injectable("echo hi\n\n\n") == "echo hi"
    assert ti.strip_injectable("echo hi\r\n") == "echo hi"


def test_trailing_whitespace_before_a_newline_goes_too():
    assert ti.strip_injectable("echo hi  \n  \n") == "echo hi"


def test_interior_newlines_survive():
    assert ti.strip_injectable("line one\nline two\n") == "line one\nline two"


def test_leading_whitespace_is_preserved_for_indentation():
    assert ti.strip_injectable("    indented()\n") == "    indented()"


def test_empty_and_whitespace_only_collapse_to_empty():
    assert ti.strip_injectable("") == ""
    assert ti.strip_injectable("\n\n") == ""
    assert ti.strip_injectable(None) == ""


def test_pasteboard_round_trip_restores_the_original_string():
    from AppKit import NSPasteboard, NSPasteboardTypeString
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_("ORIGINAL", NSPasteboardTypeString)

    snap = ti.snapshot_pasteboard()
    pb.clearContents()
    pb.setString_forType_("REPLACED", NSPasteboardTypeString)
    assert pb.stringForType_(NSPasteboardTypeString) == "REPLACED"

    ti.restore_pasteboard(snap)
    assert pb.stringForType_(NSPasteboardTypeString) == "ORIGINAL"


def test_restore_tolerates_an_empty_snapshot():
    ti.restore_pasteboard([])       # must not raise
    ti.restore_pasteboard(None)


def test_empty_text_is_never_injected(monkeypatch):
    called = []
    monkeypatch.setattr(ti, "paste_text", lambda *a, **k: called.append("paste") or True)
    ok, strategy = ti.inject_text("   \n  ")
    assert ok is False
    assert strategy == "empty"
    assert called == []


def test_paste_success_short_circuits_the_ax_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(ti, "paste_text", lambda *a, **k: calls.append("paste") or True)
    monkeypatch.setattr(ti, "ax_set_selected_text", lambda *a: calls.append("ax") or True)
    ok, strategy = ti.inject_text("hello")
    assert (ok, strategy) == (True, "paste")
    assert calls == ["paste"]


def test_ax_runs_only_when_paste_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(ti, "paste_text", lambda *a, **k: calls.append("paste") or False)
    monkeypatch.setattr(ti, "ax_set_selected_text", lambda *a: calls.append("ax") or True)
    ok, strategy = ti.inject_text("hello")
    assert (ok, strategy) == (True, "ax")
    assert calls == ["paste", "ax"]


def test_both_strategies_failing_reports_failed(monkeypatch):
    monkeypatch.setattr(ti, "paste_text", lambda *a, **k: False)
    monkeypatch.setattr(ti, "ax_set_selected_text", lambda *a: False)
    assert ti.inject_text("hello") == (False, "failed")


def test_injected_text_is_stripped_before_either_strategy(monkeypatch):
    seen = []
    monkeypatch.setattr(ti, "paste_text", lambda t, **k: seen.append(t) or True)
    ti.inject_text("echo hi\n\n")
    assert seen == ["echo hi"]


def test_clipboard_is_restored_even_when_the_paste_raises(monkeypatch):
    from AppKit import NSPasteboard, NSPasteboardTypeString
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_("KEEPME", NSPasteboardTypeString)

    def boom(*a, **k):
        raise RuntimeError("CGEventPost exploded")

    monkeypatch.setattr(ti, "_post_paste_keystroke", boom)
    assert ti.paste_text("junk") is False
    assert pb.stringForType_(NSPasteboardTypeString) == "KEEPME"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_text_injector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'text_injector'`

- [ ] **Step 3: Write `text_injector.py`**

```python
"""
text_injector.py - Puts transformed text where the user's cursor is.

Paste comes FIRST and the Accessibility write is the fallback, which is the
opposite of what looks cleanest. The reason is undo. A synthetic Cmd+V lands in
the target application's own undo stack, so Cmd+Z recovers from a bad transform;
an AX write to kAXSelectedTextAttribute generally does not. Paste also behaves
consistently in Electron apps and terminals, where AX writes frequently no-op in
silence. AX stays as the fallback for apps that block synthetic paste.

Nothing here ever emits a trailing newline. In a terminal that is the difference
between offering a command and running it.
"""

import time

import ApplicationServices as AS
import Quartz
from AppKit import NSWorkspace, NSPasteboard

KEY_CMD = 0x37
KEY_V = 0x09
CLIPBOARD_RESTORE_DELAY = 0.15


def strip_injectable(text) -> str:
    """Trailing newlines and the whitespace hanging off them, removed.

    Leading whitespace is untouched — it is the indentation of the line being
    replaced and matters in code.
    """
    if not text:
        return ""
    return str(text).rstrip("\r\n \t")


def snapshot_pasteboard():
    """Every type on the pasteboard, not just the string.

    Snapshotting only NSPasteboardTypeString would silently destroy an image or
    a file reference the user had copied.
    """
    try:
        pb = NSPasteboard.generalPasteboard()
        snapshot = []
        for ptype in (pb.types() or []):
            data = pb.dataForType_(ptype)
            if data is not None:
                snapshot.append((str(ptype), bytes(data)))
        return snapshot
    except Exception:
        return []


def restore_pasteboard(snapshot) -> None:
    if not snapshot:
        return
    try:
        from Foundation import NSData
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.declareTypes_owner_([t for t, _ in snapshot], None)
        for ptype, data in snapshot:
            pb.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), ptype)
    except Exception:
        pass


def _post_paste_keystroke() -> None:
    """Cmd+V through the HID tap. Split out so tests can make it explode."""
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    cmd_down = Quartz.CGEventCreateKeyboardEvent(src, KEY_CMD, True)
    v_down = Quartz.CGEventCreateKeyboardEvent(src, KEY_V, True)
    v_up = Quartz.CGEventCreateKeyboardEvent(src, KEY_V, False)
    cmd_up = Quartz.CGEventCreateKeyboardEvent(src, KEY_CMD, False)

    # The flag must be set on the key-up too; apps that watch only key-down
    # otherwise see a bare V and type a literal character.
    Quartz.CGEventSetFlags(v_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(v_up, Quartz.kCGEventFlagMaskCommand)

    for event in (cmd_down, v_down, v_up, cmd_up):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.01)


def paste_text(text: str, preserve_clipboard: bool = True) -> bool:
    """Strategy 1. The clipboard is restored on every exit path."""
    from AppKit import NSPasteboardTypeString

    snapshot = snapshot_pasteboard() if preserve_clipboard else []
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        if not pb.setString_forType_(text, NSPasteboardTypeString):
            return False
        _post_paste_keystroke()
        time.sleep(CLIPBOARD_RESTORE_DELAY)
        return True
    except Exception:
        return False
    finally:
        if preserve_clipboard:
            restore_pasteboard(snapshot)


def ax_set_selected_text(text: str) -> bool:
    """Strategy 2. AX writes fail silently, so the value is read back."""
    try:
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None:
            return False
        app_ref = AS.AXUIElementCreateApplication(front.processIdentifier())
        err, element = AS.AXUIElementCopyAttributeValue(
            app_ref, AS.kAXFocusedUIElementAttribute, None)
        if err != 0 or element is None:
            return False

        if AS.AXUIElementSetAttributeValue(
                element, AS.kAXSelectedTextAttribute, text) != 0:
            return False

        # Silent failure is the norm here; confirm something actually changed.
        err2, value = AS.AXUIElementCopyAttributeValue(
            element, AS.kAXValueAttribute, None)
        if err2 == 0 and value is not None and text not in str(value):
            return False
        return True
    except Exception:
        return False


def inject_text(text: str, preserve_clipboard: bool = True) -> tuple:
    """Returns (ok, strategy) so the HUD and the log can say which path ran."""
    payload = strip_injectable(text)
    if not payload:
        return (False, "empty")
    if paste_text(payload, preserve_clipboard=preserve_clipboard):
        return (True, "paste")
    if ax_set_selected_text(payload):
        return (True, "ax")
    return (False, "failed")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_text_injector.py -v`
Expected: PASS, 13 tests. The pasteboard tests touch the real system pasteboard and restore it.

- [ ] **Step 5: Checkpoint** — leave changes in the working tree, do not commit.

---

### Task 3: `speak_to_window_agent.py` — one Gemini call

**Files:**
- Create: `speak_to_window_agent.py`
- Create: `tests/test_speak_to_window_agent.py`

**Interfaces:**
- Consumes: the context dict from Task 1 (`app_name`, `bundle_id`, `selected_text`, `context_text`, `has_selection`).
- Produces:
  - `pcm_to_wav(pcm: bytes, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes`
  - `is_code_target(ctx: dict) -> bool`
  - `build_prompt(ctx: dict) -> str`
  - `clean_response(raw: str, keep_fences: bool = False) -> str`
  - `is_refusal(text: str) -> bool`
  - `async process_window_voice_action(client, ctx: dict, wav_bytes: bytes) -> str`
  - `TRANSFORM_MODEL`, `SYSTEM_TRANSFORM_PROMPT`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speak_to_window_agent.py`:

```python
import io
import wave

import pytest

import speak_to_window_agent as agent


def test_pcm_becomes_a_readable_wav():
    pcm = b"\x00\x01" * 8000
    data = agent.pcm_to_wav(pcm, rate=16000, channels=1, width=2)
    with wave.open(io.BytesIO(data), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.readframes(w.getnframes()) == pcm


def test_code_targets_recognised_by_bundle_id_and_name():
    assert agent.is_code_target({"bundle_id": "com.microsoft.VSCode", "app_name": "Code"})
    assert agent.is_code_target({"bundle_id": "com.apple.Terminal", "app_name": "Terminal"})
    assert agent.is_code_target({"bundle_id": None, "app_name": "iTerm2"})


def test_prose_targets_are_not_code_targets():
    assert not agent.is_code_target({"bundle_id": "com.apple.mail", "app_name": "Mail"})
    assert not agent.is_code_target({"bundle_id": None, "app_name": None})


def test_prompt_names_the_app_and_carries_the_selection():
    prompt = agent.build_prompt({
        "app_name": "Mail", "bundle_id": "com.apple.mail",
        "selected_text": "Dear sir", "context_text": "Dear sir",
        "has_selection": True,
    })
    assert "Mail" in prompt
    assert "Dear sir" in prompt
    assert "SELECTION" in prompt


def test_prompt_says_so_when_there_is_no_selection():
    prompt = agent.build_prompt({
        "app_name": "Code", "bundle_id": "com.microsoft.VSCode",
        "selected_text": None, "context_text": "def f():⟨CARET⟩",
        "has_selection": False,
    })
    assert "SURROUNDING" in prompt
    assert "⟨CARET⟩" in prompt


def test_prompt_handles_a_completely_empty_context():
    prompt = agent.build_prompt({
        "app_name": "Finder", "bundle_id": None, "selected_text": None,
        "context_text": None, "has_selection": False,
    })
    assert "Finder" in prompt
    assert prompt.strip()


def test_fences_are_stripped_for_prose_targets():
    assert agent.clean_response("```\nhello there\n```") == "hello there"
    assert agent.clean_response("```python\nx = 1\n```") == "x = 1"


def test_fences_are_kept_for_code_targets_only_when_asked():
    out = agent.clean_response("```python\nx = 1\n```", keep_fences=True)
    assert out == "```python\nx = 1\n```"


def test_cleaning_always_removes_the_trailing_newline():
    assert agent.clean_response("echo hi\n\n") == "echo hi"
    assert agent.clean_response("```\necho hi\n```\n") == "echo hi"


def test_interior_structure_survives_cleaning():
    assert agent.clean_response("one\n\ntwo\n") == "one\n\ntwo"


def test_refusals_are_detected():
    assert agent.is_refusal("I'm sorry, I can't help with that.")
    assert agent.is_refusal("I cannot assist with this request")
    assert agent.is_refusal("As an AI language model, I am unable to")
    assert agent.is_refusal("")
    assert agent.is_refusal("   ")


def test_ordinary_text_is_not_a_refusal():
    assert not agent.is_refusal("I can help you with that later, he wrote.")
    assert not agent.is_refusal("rm -rf ./build")
    assert not agent.is_refusal("Sorry for the delay — here is the report.")


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, text):
        self._text = text
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._text)


class _FakeAio:
    def __init__(self, text):
        self.models = _FakeModels(text)


class _FakeClient:
    def __init__(self, text):
        self.aio = _FakeAio(text)


@pytest.mark.asyncio
async def test_transform_returns_cleaned_text():
    client = _FakeClient("```\nHello there\n```")
    ctx = {"app_name": "Mail", "bundle_id": "com.apple.mail",
           "selected_text": "hi", "context_text": "hi", "has_selection": True}
    out = await agent.process_window_voice_action(client, ctx, b"RIFFfake")
    assert out == "Hello there"


@pytest.mark.asyncio
async def test_a_refusal_yields_empty_so_nothing_is_injected():
    client = _FakeClient("I'm sorry, I can't help with that.")
    ctx = {"app_name": "Mail", "bundle_id": None, "selected_text": "hi",
           "context_text": "hi", "has_selection": True}
    assert await agent.process_window_voice_action(client, ctx, b"RIFFfake") == ""


@pytest.mark.asyncio
async def test_the_wav_is_sent_with_an_audio_mime_type():
    client = _FakeClient("ok")
    ctx = {"app_name": "Mail", "bundle_id": None, "selected_text": None,
           "context_text": None, "has_selection": False}
    await agent.process_window_voice_action(client, ctx, b"RIFFfake")
    contents = client.aio.models.calls[0]["contents"]
    blob = str(contents)
    assert "audio/wav" in blob
```

Add `pytest-asyncio` and a config so `@pytest.mark.asyncio` works:

```bash
printf 'pytest-asyncio\n' >> requirements.txt
.venv/bin/pip install pytest-asyncio
cat > pytest.ini <<'EOF'
[pytest]
asyncio_mode = auto
testpaths = tests
EOF
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_speak_to_window_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speak_to_window_agent'`

- [ ] **Step 3: Write `speak_to_window_agent.py`**

```python
"""
speak_to_window_agent.py - Turns a spoken instruction into text to inject.

One Gemini call, not two. The WAV goes inline alongside the app context and the
model both hears the instruction and emits the replacement. Transcribing first
would make failures legible — you would know whether it misheard or
misunderstood — but it doubles the latency, and this runs in the middle of
someone's typing. The accepted cost is that a bad result is ambiguous.

Follows the shape of widget_generator_agent.py: module-level system prompt, one
generate_content call, a timeout, cleaning on the way out.
"""

import asyncio
import io
import re
import wave

from google.genai import types

TRANSFORM_MODEL = "gemini-3.7-flash"
TRANSFORM_TIMEOUT_S = 20.0

# Targets where fenced code and raw commands are the point, not noise.
CODE_BUNDLE_IDS = {
    "com.microsoft.VSCode", "com.apple.dt.Xcode", "com.apple.Terminal",
    "com.googlecode.iterm2", "com.jetbrains.pycharm", "com.sublimetext.4",
    "dev.warp.Warp-Stable", "com.github.wez.wezterm", "net.kovidgoyal.kitty",
}
CODE_APP_NAMES = {
    "code", "xcode", "terminal", "iterm", "iterm2", "warp", "wezterm",
    "kitty", "pycharm", "sublime text", "nvim", "vim", "ghostty",
}

_REFUSAL_PATTERNS = (
    r"^i'?m sorry",
    r"^i am sorry",
    r"^sorry, (?:i|but)",
    r"^i can(?:no|')t (?:help|assist|do|comply)",
    r"^i cannot (?:help|assist|do|comply|fulfil)",
    r"^i'?m (?:unable|not able) to",
    r"^as an ai",
    r"^unfortunately,? i (?:can|am)",
)

SYSTEM_TRANSFORM_PROMPT = """
You are Ultron's Speak-to-Window Native Text Synthesizer.

You receive the name of the macOS application the user is working in, the text
they have selected (or the text around their cursor), and a spoken instruction
as audio. Output STRICTLY the text to be typed in — nothing else.

RULES
1. PURE OUTPUT. No conversational preamble, no explanation, no quotes around the
   result, no markdown fences unless the target is a code editor or a terminal.
   Your entire response is inserted verbatim into the user's document.
2. NEVER end your output with a newline.
3. CONTEXTUAL RELEVANCE.
   - VS Code / Xcode / an editor: match the surrounding indentation, naming and
     syntax exactly.
   - Mail / Slack / Notes: match the register the instruction asks for.
   - Terminal: return the raw executable shell command and nothing else. Never
     append a newline; the user decides whether to run it.
4. If the instruction is a QUESTION rather than an edit, answer it as text to be
   inserted at the cursor. Do not converse, do not address the user.
5. If a selection was provided, your output REPLACES it. If there was no
   selection, your output is INSERTED at the cursor.
6. CONCISENESS. Only the text intended for insertion.
"""


def pcm_to_wav(pcm: bytes, rate: int = 16000, channels: int = 1,
               width: int = 2) -> bytes:
    """Wrap raw PyAudio PCM in a WAV container so Gemini can read it."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buffer.getvalue()


def is_code_target(ctx: dict) -> bool:
    bundle_id = (ctx.get("bundle_id") or "")
    if bundle_id in CODE_BUNDLE_IDS:
        return True
    name = (ctx.get("app_name") or "").strip().lower()
    return bool(name) and name in CODE_APP_NAMES


def build_prompt(ctx: dict) -> str:
    app_name = ctx.get("app_name") or "an unknown application"
    body = ctx.get("context_text")

    if ctx.get("has_selection"):
        block = f'SELECTION (your output replaces this):\n"""{body}"""'
    elif body:
        block = (f'SURROUNDING TEXT (⟨CARET⟩ marks the cursor; your output is '
                 f'inserted there):\n"""{body}"""')
    else:
        block = "There is no existing text. Generate what the instruction asks for."

    return (f"TARGET APP: {app_name}\n\n{block}\n\n"
            "The user's spoken instruction is in the attached audio.\n\n"
            "TEXT TO INSERT:")


def clean_response(raw: str, keep_fences: bool = False) -> str:
    text = (raw or "").strip()
    if not keep_fences:
        fenced = re.match(r"^```[a-zA-Z0-9_+-]*\n(.*?)\n?```$", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
    return text.rstrip("\r\n \t")


def is_refusal(text: str) -> bool:
    """Empty or refusal-shaped output must never be pasted into a document."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    return any(re.search(p, lowered) for p in _REFUSAL_PATTERNS)


async def process_window_voice_action(client, ctx: dict, wav_bytes: bytes) -> str:
    """Returns the text to inject, or "" if nothing should be injected."""
    parts = [
        types.Part.from_text(text=build_prompt(ctx)),
        types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
    ]

    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=TRANSFORM_MODEL,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_TRANSFORM_PROMPT,
                temperature=0.1,
            ),
        ),
        timeout=TRANSFORM_TIMEOUT_S,
    )

    text = clean_response(response.text, keep_fences=False)
    if is_refusal(text):
        return ""
    return text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_speak_to_window_agent.py -v`
Expected: PASS, 15 tests.

- [ ] **Step 5: Checkpoint** — leave changes in the working tree, do not commit.

---

### Task 4: `hotkey_manager.py` — binding parser and event tap

**Files:**
- Create: `hotkey_manager.py`
- Create: `tests/test_hotkey_manager.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_binding(spec: str) -> dict` — `{"keycode": int | None, "mods": int}`
  - `matches(binding: dict, keycode: int | None, flags: int) -> bool`
  - `class HotkeyManager(binding_spec, on_press, on_release)` with `.start() -> bool`, `.stop()`, `.held: bool`
  - `DEFAULT_BINDING = "cmd+shift+space"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hotkey_manager.py`:

```python
import Quartz
import pytest

import hotkey_manager as hm

CMD = Quartz.kCGEventFlagMaskCommand
SHIFT = Quartz.kCGEventFlagMaskShift
FN = Quartz.kCGEventFlagMaskSecondaryFn


def test_default_binding_parses_to_space_plus_cmd_shift():
    b = hm.parse_binding("cmd+shift+space")
    assert b["keycode"] == 49
    assert b["mods"] == CMD | SHIFT


def test_parsing_is_case_and_space_insensitive():
    assert hm.parse_binding("  CMD + Shift + SPACE ") == hm.parse_binding("cmd+shift+space")


def test_modifier_aliases_resolve():
    assert hm.parse_binding("command+space") == hm.parse_binding("cmd+space")
    assert hm.parse_binding("option+space") == hm.parse_binding("alt+space")
    assert hm.parse_binding("control+space") == hm.parse_binding("ctrl+space")


def test_fn_alone_is_a_modifier_only_binding():
    b = hm.parse_binding("fn")
    assert b["keycode"] is None
    assert b["mods"] == FN


def test_unknown_key_falls_back_to_the_default():
    assert hm.parse_binding("cmd+shift+bananas") == hm.parse_binding(hm.DEFAULT_BINDING)


def test_empty_binding_falls_back_to_the_default():
    assert hm.parse_binding("") == hm.parse_binding(hm.DEFAULT_BINDING)
    assert hm.parse_binding(None) == hm.parse_binding(hm.DEFAULT_BINDING)


def test_chord_matches_only_with_every_modifier_present():
    b = hm.parse_binding("cmd+shift+space")
    assert hm.matches(b, 49, CMD | SHIFT)
    assert not hm.matches(b, 49, CMD)
    assert not hm.matches(b, 49, 0)


def test_chord_does_not_match_a_different_key():
    b = hm.parse_binding("cmd+shift+space")
    assert not hm.matches(b, 50, CMD | SHIFT)


def test_extra_modifiers_still_match():
    b = hm.parse_binding("cmd+shift+space")
    assert hm.matches(b, 49, CMD | SHIFT | Quartz.kCGEventFlagMaskControl)


def test_modifier_only_binding_matches_on_flags_alone():
    b = hm.parse_binding("fn")
    assert hm.matches(b, None, FN)
    assert not hm.matches(b, None, 0)


def test_press_fires_once_and_release_fires_once():
    events = []
    mgr = hm.HotkeyManager("cmd+shift+space",
                           on_press=lambda: events.append("press"),
                           on_release=lambda: events.append("release"))
    mgr._handle(Quartz.kCGEventKeyDown, 49, CMD | SHIFT)
    mgr._handle(Quartz.kCGEventKeyUp, 49, CMD | SHIFT)
    assert events == ["press", "release"]


def test_key_repeat_does_not_fire_press_twice():
    events = []
    mgr = hm.HotkeyManager("cmd+shift+space",
                           on_press=lambda: events.append("press"),
                           on_release=lambda: events.append("release"))
    for _ in range(5):
        mgr._handle(Quartz.kCGEventKeyDown, 49, CMD | SHIFT)
    mgr._handle(Quartz.kCGEventKeyUp, 49, CMD | SHIFT)
    assert events == ["press", "release"]


def test_release_without_a_press_is_ignored():
    events = []
    mgr = hm.HotkeyManager("cmd+shift+space",
                           on_press=lambda: events.append("press"),
                           on_release=lambda: events.append("release"))
    mgr._handle(Quartz.kCGEventKeyUp, 49, CMD | SHIFT)
    assert events == []


def test_dropping_a_modifier_while_held_releases():
    events = []
    mgr = hm.HotkeyManager("cmd+shift+space",
                           on_press=lambda: events.append("press"),
                           on_release=lambda: events.append("release"))
    mgr._handle(Quartz.kCGEventKeyDown, 49, CMD | SHIFT)
    mgr._handle(Quartz.kCGEventFlagsChanged, None, CMD)   # shift let go
    assert events == ["press", "release"]


def test_modifier_only_binding_press_and_release():
    events = []
    mgr = hm.HotkeyManager("fn",
                           on_press=lambda: events.append("press"),
                           on_release=lambda: events.append("release"))
    mgr._handle(Quartz.kCGEventFlagsChanged, None, FN)
    mgr._handle(Quartz.kCGEventFlagsChanged, None, 0)
    assert events == ["press", "release"]


def test_a_raising_callback_does_not_wedge_the_held_state():
    def boom():
        raise RuntimeError("callback exploded")

    released = []
    mgr = hm.HotkeyManager("cmd+shift+space", on_press=boom,
                           on_release=lambda: released.append(1))
    mgr._handle(Quartz.kCGEventKeyDown, 49, CMD | SHIFT)
    assert mgr.held is True
    mgr._handle(Quartz.kCGEventKeyUp, 49, CMD | SHIFT)
    assert released == [1]
    assert mgr.held is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_hotkey_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hotkey_manager'`

- [ ] **Step 3: Write `hotkey_manager.py`**

```python
"""
hotkey_manager.py - Global push-to-talk detection for Speak-to-Window.

A Quartz CGEventTap on its own daemon thread with its own CFRunLoop. The tap is
kCGEventTapOptionListenOnly, so it observes the keyboard and can never swallow
or alter a keystroke — the failure mode of a broken tap here is "push-to-talk
stops working", never "the user's keyboard stops working".

pynput would also do this. Quartz is already a dependency for the mouse events
in sentry_action.py, so this adds nothing to the install.

This module knows nothing about audio, Gemini or injection. It turns events into
two callbacks.
"""

import threading

import Quartz

DEFAULT_BINDING = "cmd+shift+space"

MODIFIER_MASKS = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "control": Quartz.kCGEventFlagMaskControl,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "opt": Quartz.kCGEventFlagMaskAlternate,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}

KEYCODES = {
    "space": 49, "return": 36, "enter": 36, "tab": 48, "escape": 53, "esc": 53,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4, "i": 34,
    "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35, "q": 12,
    "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79,
}


def parse_binding(spec: str) -> dict:
    """'cmd+shift+space' -> {'keycode': 49, 'mods': <mask>}.

    A binding with no non-modifier key (e.g. 'fn') is modifier-only and is
    matched on flagsChanged alone. Anything unparseable falls back to the
    default rather than leaving the user with no hotkey at all.
    """
    if not spec or not str(spec).strip():
        return parse_binding(DEFAULT_BINDING)

    mods = 0
    keycode = None
    for token in str(spec).lower().replace(" ", "").split("+"):
        if not token:
            continue
        if token in MODIFIER_MASKS:
            mods |= MODIFIER_MASKS[token]
        elif token in KEYCODES:
            keycode = KEYCODES[token]
        else:
            if spec == DEFAULT_BINDING:      # guard against infinite recursion
                raise ValueError(f"Bad default binding: {spec}")
            return parse_binding(DEFAULT_BINDING)

    if keycode is None and mods == 0:
        return parse_binding(DEFAULT_BINDING)
    return {"keycode": keycode, "mods": mods}


def matches(binding: dict, keycode, flags: int) -> bool:
    """All required modifiers present, and the key matches if one is required.

    Extra modifiers are tolerated so a chord still fires with caps lock on.
    """
    if (flags & binding["mods"]) != binding["mods"]:
        return False
    if binding["keycode"] is None:
        return True
    return keycode == binding["keycode"]


class HotkeyManager:
    """Watches for the push-to-talk chord and calls on_press / on_release."""

    def __init__(self, binding_spec: str, on_press, on_release):
        self.binding = parse_binding(binding_spec)
        self.on_press = on_press
        self.on_release = on_release
        self.held = False
        self._thread = None
        self._runloop = None
        self._tap = None
        self._stop = threading.Event()

    # ---- state machine (unit-tested directly, no tap required) ----

    def _handle(self, event_type, keycode, flags):
        hit = matches(self.binding, keycode, flags)

        if not self.held:
            press = hit and event_type in (Quartz.kCGEventKeyDown,
                                           Quartz.kCGEventFlagsChanged)
            # A modifier-only binding has no key-down to wait for.
            if press and self.binding["keycode"] is not None \
                    and event_type == Quartz.kCGEventFlagsChanged:
                press = False
            if press:
                self.held = True
                self._fire(self.on_press)
            return

        # Held: release on the key going up, or on any required modifier going.
        released = (event_type == Quartz.kCGEventKeyUp
                    and (self.binding["keycode"] is None
                         or keycode == self.binding["keycode"])) or not hit
        if released:
            self.held = False
            self._fire(self.on_release)

    @staticmethod
    def _fire(callback):
        """A raising callback must never wedge the held state."""
        try:
            callback()
        except Exception as e:
            print(f"[Speak-to-Window] Hotkey callback failed: {e}")

    # ---- the tap itself ----

    def _callback(self, proxy, event_type, event, refcon):
        try:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            flags = Quartz.CGEventGetFlags(event)
            self._handle(event_type, keycode, flags)
        except Exception:
            pass
        return event        # listen-only, but return the event regardless

    def _run(self):
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))

        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask, self._callback, None)

        if not self._tap:
            print("[Speak-to-Window] Could not create the event tap. "
                  "Grant Accessibility permission to the app running Ultron.")
            return

        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._runloop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._runloop, source,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)

        while not self._stop.is_set():
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.25, False)

        Quartz.CGEventTapEnable(self._tap, False)

    def start(self) -> bool:
        """Arms the tap. False if Accessibility is not granted."""
        import ApplicationServices as AS
        if not AS.AXIsProcessTrusted():
            print("[Speak-to-Window] Accessibility permission not granted; "
                  "push-to-talk is disabled. The rest of Ultron is unaffected.")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="ultron-ptt-hotkey", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_hotkey_manager.py -v`
Expected: PASS, 16 tests.

- [ ] **Step 5: Checkpoint** — leave changes in the working tree, do not commit.

---

### Task 5: `native_hud.py` — floating status panel

**Files:**
- Create: `native_hud.py`
- Create: `tests/test_native_hud.py`

**Interfaces:**
- Consumes: `caret_rect` from Task 1 (an AX rect: `(x, y, w, h)`, global, top-left origin).
- Produces:
  - `flip_rect_to_cocoa(ax_rect: tuple, primary_height: float) -> tuple`
  - `STATES: dict[str, tuple[str, str]]` — state → (label, hex colour)
  - `class StatusHUD` with `.show(state, ax_rect=None)`, `.hide()`, `.available() -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_native_hud.py`:

```python
import native_hud


def test_flip_converts_top_left_origin_to_bottom_left():
    # A 20px-tall caret whose top edge is 100px down a 1080px screen sits
    # 1080 - 100 - 20 = 960px up from the bottom.
    assert native_hud.flip_rect_to_cocoa((50, 100, 2, 20), 1080) == (50, 960, 2, 20)


def test_flip_at_the_top_of_the_screen():
    assert native_hud.flip_rect_to_cocoa((0, 0, 2, 20), 1080) == (0, 1060, 2, 20)


def test_flip_at_the_bottom_of_the_screen():
    assert native_hud.flip_rect_to_cocoa((0, 1060, 2, 20), 1080) == (0, 0, 2, 20)


def test_flip_handles_a_secondary_display_to_the_right():
    # x is untouched; only the y axis differs between the two coordinate spaces.
    assert native_hud.flip_rect_to_cocoa((1920, 200, 2, 20), 1080) == (1920, 860, 2, 20)


def test_flip_of_none_is_none():
    assert native_hud.flip_rect_to_cocoa(None, 1080) is None


def test_every_state_has_a_label_and_a_colour():
    for state in ("listening", "transforming", "injected", "failed"):
        label, colour = native_hud.STATES[state]
        assert label and colour.startswith("#")


def test_hud_degrades_instead_of_raising_when_no_app_is_running(monkeypatch):
    monkeypatch.setattr(native_hud, "_app_is_running", lambda: False)
    hud = native_hud.StatusHUD()
    assert hud.available() is False
    hud.show("listening", (0, 0, 2, 20))    # must not raise
    hud.hide()


def test_show_with_an_unknown_state_is_ignored(monkeypatch):
    monkeypatch.setattr(native_hud, "_app_is_running", lambda: False)
    hud = native_hud.StatusHUD()
    hud.show("nonsense")                     # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_native_hud.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'native_hud'`

- [ ] **Step 3: Write `native_hud.py`**

```python
"""
native_hud.py - The floating status pill for Speak-to-Window.

The Ultron window is a normal PyWebView window. During a speak-to-window action
the frontmost app is VS Code or Mail or a terminal, so a pill drawn inside the
Ultron page is behind the thing the user is looking at — invisible exactly when
it matters. This is a borderless non-activating NSPanel at floating window
level, which appears over any application without stealing focus.

Two constraints shape the code:

  * AppKit is not thread-safe and PyWebView owns the macOS main thread, so every
    UI call is dispatched to the main queue.
  * In the headless path (app_desktop.run_headless) there is no NSApplication
    running. The HUD must degrade to nothing rather than raise; the WebSocket
    mirror in the Ultron window is the fallback feedback.
"""

import libdispatch
from AppKit import (NSApplication, NSBackingStoreBuffered, NSColor,
                    NSFloatingWindowLevel, NSFont, NSMakeRect, NSPanel, NSScreen,
                    NSTextField, NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorIgnoresCycle,
                    NSWindowCollectionBehaviorStationary,
                    NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel)

# state -> (label, accent colour)
STATES = {
    "listening":    ("LISTENING",   "#22d3ee"),
    "transforming": ("TRANSFORMING", "#a78bfa"),
    "injected":     ("INJECTED ✓",  "#4ade80"),
    "failed":       ("FAILED",      "#fbbf24"),
}

PANEL_W, PANEL_H = 176.0, 34.0
CARET_GAP = 12.0        # panel sits this far below the caret


def flip_rect_to_cocoa(ax_rect, primary_height: float):
    """AX reports a global top-left origin; Cocoa windows use bottom-left.

    The flip is always against the PRIMARY screen's height — the screen whose
    Cocoa origin is (0,0) — not the screen the caret happens to be on. Getting
    this wrong puts the panel on the wrong monitor.
    """
    if not ax_rect:
        return None
    x, y, w, h = ax_rect
    return (x, primary_height - y - h, w, h)


def _app_is_running() -> bool:
    """False in the headless path, where there is no NSApplication run loop."""
    try:
        return bool(NSApplication.sharedApplication().isRunning())
    except Exception:
        return False


def _on_main(fn):
    """AppKit work belongs on the main queue; PyWebView owns that thread."""
    try:
        libdispatch.dispatch_async(libdispatch.dispatch_get_main_queue(), fn)
    except Exception:
        pass


def _colour(hex_str: str):
    h = hex_str.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


class StatusHUD:
    def __init__(self):
        self._panel = None
        self._label = None

    def available(self) -> bool:
        return _app_is_running()

    def _ensure_panel(self):
        if self._panel is not None:
            return
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.03, 0.05, 0.08, 0.86))
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setIgnoresMouseEvents_(True)
        panel.setHasShadow_(True)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorIgnoresCycle)

        label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, 7, PANEL_W, 20))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setAlignment_(1)          # NSTextAlignmentCenter
        label.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11.5, 0.3))
        panel.contentView().addSubview_(label)

        self._panel, self._label = panel, label

    def show(self, state: str, ax_rect=None):
        if state not in STATES:
            return
        if not self.available():
            return                       # headless: the WS mirror is the feedback
        text, colour = STATES[state]

        def paint():
            try:
                self._ensure_panel()
                self._label.setStringValue_(text)
                self._label.setTextColor_(_colour(colour))

                screens = NSScreen.screens()
                primary_h = (screens[0].frame().size.height if screens else 1080.0)
                flipped = flip_rect_to_cocoa(ax_rect, primary_h)
                if flipped:
                    x, y, _w, _h = flipped
                    px = x - PANEL_W / 2.0
                    py = y - PANEL_H - CARET_GAP
                else:
                    from Quartz import NSEvent
                    loc = NSEvent.mouseLocation()
                    px, py = loc.x - PANEL_W / 2.0, loc.y - PANEL_H - CARET_GAP

                self._panel.setFrameOrigin_((px, py))
                self._panel.orderFrontRegardless()
            except Exception as e:
                print(f"[Speak-to-Window] HUD paint failed: {e}")

        _on_main(paint)

    def hide(self):
        if not self.available() or self._panel is None:
            return

        def dismiss():
            try:
                self._panel.orderOut_(None)
            except Exception:
                pass

        _on_main(dismiss)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_native_hud.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Checkpoint** — leave changes in the working tree, do not commit.

---

### Task 6: `speak_to_window.py` — the coordinator

**Files:**
- Create: `speak_to_window.py`
- Create: `tests/test_speak_to_window.py`

**Interfaces:**
- Consumes: `window_context.get_focused_window_context`, `text_injector.inject_text`, `speak_to_window_agent.{pcm_to_wav, process_window_voice_action}`, `hotkey_manager.HotkeyManager`, `native_hud.StatusHUD`.
- Produces:
  - `class SpeakToWindow(get_client, broadcast, log_event, set_ptt, take_audio, loop, binding=None, capture=None, inject=None, transform=None, hud=None)`
  - `.on_press()`, `.on_release()`, `async .run_action(ctx, pcm)`, `.start() -> bool`, `.stop()`

Every collaborator is injectable so the whole flow can be exercised with fakes and no native calls.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speak_to_window.py`:

```python
import asyncio

import pytest

import speak_to_window as stw


class FakeHUD:
    def __init__(self):
        self.states = []

    def available(self):
        return True

    def show(self, state, ax_rect=None):
        self.states.append(state)

    def hide(self):
        self.states.append("hidden")


def make(ctx=None, transformed="TRANSFORMED", inject_result=(True, "paste"),
         pcm=b"\x00\x01" * 4000):
    """A coordinator with every collaborator faked."""
    recorder = {"broadcast": [], "log": [], "ptt": [], "injected": []}
    ctx = ctx if ctx is not None else {
        "app_name": "Mail", "bundle_id": "com.apple.mail", "role": "AXTextArea",
        "selected_text": "hi", "context_text": "hi", "has_selection": True,
        "caret_rect": (10, 20, 2, 18), "blocked": False,
    }

    async def transform(client, c, wav):
        return transformed

    def inject(text):
        recorder["injected"].append(text)
        return inject_result

    hud = FakeHUD()
    coordinator = stw.SpeakToWindow(
        get_client=lambda: object(),
        broadcast=lambda ev: recorder["broadcast"].append(ev),
        log_event=lambda kind, details: recorder["log"].append((kind, details)),
        set_ptt=lambda active: recorder["ptt"].append(active),
        take_audio=lambda: pcm,
        loop=None,
        capture=lambda: ctx,
        inject=inject,
        transform=transform,
        hud=hud,
    )
    return coordinator, recorder, hud


def test_press_captures_context_and_opens_the_ptt_gate():
    coordinator, recorder, hud = make()
    coordinator.on_press()
    assert coordinator.pending_ctx["app_name"] == "Mail"
    assert recorder["ptt"] == [True]
    assert hud.states == ["listening"]


def test_press_captures_before_release_so_focus_cannot_drift():
    """The context must be the one captured at key-down, not re-read later."""
    coordinator, recorder, hud = make()
    coordinator.on_press()
    captured = coordinator.pending_ctx
    coordinator.capture = lambda: {"app_name": "SomethingElse"}
    coordinator.on_release()
    assert captured["app_name"] == "Mail"


def test_a_blocked_secure_field_never_reaches_the_model():
    ctx = {"app_name": "Safari", "bundle_id": None, "role": "AXSecureTextField",
           "selected_text": None, "context_text": None, "has_selection": False,
           "caret_rect": None, "blocked": True}
    coordinator, recorder, hud = make(ctx=ctx)
    coordinator.on_press()
    assert recorder["ptt"] == [True, False]     # gate opened then immediately shut
    assert coordinator.pending_ctx is None
    assert "failed" in hud.states


async def test_full_flow_injects_the_transformed_text():
    coordinator, recorder, hud = make()
    coordinator.on_press()
    await coordinator.run_action(coordinator.pending_ctx, b"\x00\x01" * 4000)
    assert recorder["injected"] == ["TRANSFORMED"]
    assert hud.states[-2:] == ["injected", "hidden"]


async def test_the_ptt_gate_always_closes_after_a_run():
    coordinator, recorder, hud = make()
    coordinator.on_press()
    coordinator.on_release()
    await asyncio.sleep(0)
    assert recorder["ptt"][-1] is False


async def test_an_empty_transform_injects_nothing():
    coordinator, recorder, hud = make(transformed="")
    await coordinator.run_action({"app_name": "Mail", "caret_rect": None,
                                  "blocked": False}, b"\x00\x01" * 4000)
    assert recorder["injected"] == []
    assert "failed" in hud.states


async def test_a_failed_injection_is_reported_as_failed():
    coordinator, recorder, hud = make(inject_result=(False, "failed"))
    await coordinator.run_action({"app_name": "Mail", "caret_rect": None,
                                  "blocked": False}, b"\x00\x01" * 4000)
    assert "failed" in hud.states


async def test_a_transform_exception_does_not_escape():
    coordinator, recorder, hud = make()

    async def boom(client, ctx, wav):
        raise RuntimeError("gemini exploded")

    coordinator.transform = boom
    await coordinator.run_action({"app_name": "Mail", "caret_rect": None,
                                  "blocked": False}, b"\x00\x01" * 4000)
    assert "failed" in hud.states
    assert recorder["injected"] == []


async def test_silence_is_not_sent_to_the_model():
    coordinator, recorder, hud = make(pcm=b"")
    await coordinator.run_action({"app_name": "Mail", "caret_rect": None,
                                  "blocked": False}, b"")
    assert recorder["injected"] == []


async def test_every_run_broadcasts_states_and_logs_once():
    coordinator, recorder, hud = make()
    await coordinator.run_action({"app_name": "Mail", "caret_rect": None,
                                  "blocked": False}, b"\x00\x01" * 4000)
    states = [e.get("state") for e in recorder["broadcast"]]
    assert "transforming" in states
    assert "complete" in states
    assert len(recorder["log"]) == 1
    kind, details = recorder["log"][0]
    assert kind == "window_action"
    assert details["app"] == "Mail"


def test_release_without_a_pending_context_is_a_no_op():
    coordinator, recorder, hud = make()
    coordinator.on_release()            # must not raise
    assert recorder["injected"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_speak_to_window.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speak_to_window'`

- [ ] **Step 3: Write `speak_to_window.py`**

```python
"""
speak_to_window.py - Coordinates one push-to-talk action end to end.

    key down -> capture context, open the PTT gate, HUD: LISTENING
    key up   -> close the gate, take the audio, HUD: TRANSFORMING
                one Gemini call -> inject -> HUD: INJECTED / FAILED

Context is captured on key DOWN, never on key up. By the time the key is
released the frontmost application may have changed, and capturing then would
read the wrong window.

Every collaborator is injected rather than imported at the call site, so the
whole flow above is testable with fakes and no Accessibility permission, no
microphone and no network.
"""

import asyncio
import os

import hotkey_manager
import native_hud
import speak_to_window_agent as agent
import text_injector
import window_context

MIN_PCM_BYTES = 4000        # ~0.125s at 16kHz/16-bit; below this it is a stray tap


class SpeakToWindow:
    def __init__(self, get_client, broadcast, log_event, set_ptt, take_audio,
                 loop, binding=None, capture=None, inject=None, transform=None,
                 hud=None):
        self.get_client = get_client
        self.broadcast = broadcast
        self.log_event = log_event
        self.set_ptt = set_ptt
        self.take_audio = take_audio
        self.loop = loop

        self.capture = capture or window_context.get_focused_window_context
        self.inject = inject or text_injector.inject_text
        self.transform = transform or agent.process_window_voice_action
        self.hud = hud or native_hud.StatusHUD()

        self.binding = binding or os.getenv("ULTRON_PTT_HOTKEY",
                                            hotkey_manager.DEFAULT_BINDING)
        self.pending_ctx = None
        self._manager = None

    # ---- lifecycle ----

    def start(self) -> bool:
        self._manager = hotkey_manager.HotkeyManager(
            self.binding, on_press=self.on_press, on_release=self.on_release)
        started = self._manager.start()
        if started:
            print(f"[Speak-to-Window] Push-to-talk armed on '{self.binding}'.")
        return started

    def stop(self):
        if self._manager:
            self._manager.stop()
            self._manager = None
        self.hud.hide()

    # ---- hotkey callbacks (called from the tap thread) ----

    def on_press(self):
        ctx = self.capture()
        self.set_ptt(True)

        if ctx.get("blocked"):
            # A password field. Nothing is read, nothing is sent, and the gate
            # closes again immediately.
            self.set_ptt(False)
            self.pending_ctx = None
            self._announce("failed", ctx.get("app_name"), "secure field")
            return

        self.pending_ctx = ctx
        self.hud.show("listening", ctx.get("caret_rect"))
        self._emit({"type": "window_action", "state": "recording",
                    "app": ctx.get("app_name")})

    def on_release(self):
        ctx, self.pending_ctx = self.pending_ctx, None
        if ctx is None:
            self.set_ptt(False)
            return

        pcm = self.take_audio()
        self.set_ptt(False)

        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.run_action(ctx, pcm), self.loop)
        else:
            # Tests drive run_action directly.
            self._pending_pcm = pcm

    # ---- the action ----

    async def run_action(self, ctx, pcm):
        app_name = ctx.get("app_name")
        rect = ctx.get("caret_rect")

        if not pcm or len(pcm) < MIN_PCM_BYTES:
            self._announce("failed", app_name, "no speech captured", rect)
            return

        self.hud.show("transforming", rect)
        self._emit({"type": "window_action", "state": "processing",
                    "app": app_name})

        try:
            wav = agent.pcm_to_wav(pcm)
            text = await self.transform(self.get_client(), ctx, wav)
        except Exception as e:
            self._announce("failed", app_name, f"transform failed: {e}", rect)
            return

        if not text:
            self._announce("failed", app_name, "nothing to inject", rect)
            return

        ok, strategy = self.inject(text)
        if not ok:
            self._announce("failed", app_name, f"injection {strategy}", rect)
            return

        self.hud.show("injected", rect)
        self._emit({"type": "window_action", "state": "complete", "app": app_name})
        self._log(app_name, True, strategy, len(text))
        await asyncio.sleep(1.2)
        self.hud.hide()

    # ---- plumbing ----

    def _announce(self, state, app_name, reason, rect=None):
        print(f"[Speak-to-Window] {reason}")
        self.hud.show(state, rect)
        self._emit({"type": "window_action", "state": "failed",
                    "app": app_name, "reason": reason})
        self._log(app_name, False, reason, 0)
        self.hud.hide()

    def _emit(self, event):
        try:
            self.broadcast(event)
        except Exception:
            pass

    def _log(self, app_name, ok, detail, length):
        try:
            self.log_event("window_action", {
                "app": app_name, "ok": ok, "detail": detail, "chars": length})
        except Exception:
            pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_speak_to_window.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: PASS, 74 tests across six files.

- [ ] **Step 6: Checkpoint** — leave changes in the working tree, do not commit.

---

### Task 7: `ultron_hub.py` integration

**Files:**
- Modify: `ultron_hub.py` — imports (~line 38), globals (~line 82), `ws_handler` audio_in (356–367), `send_audio_task` (531–558), `run_ultron` startup (~1967) and teardown.

**Interfaces:**
- Consumes: `SpeakToWindow` from Task 6.
- Produces: `ptt_active: bool`, `_ptt_frames: list[bytes]`, `set_ptt(active)`, `take_ptt_audio() -> bytes`, `speak_to_window_manager`.

- [ ] **Step 1: Add the import**

After `import widget_generator_agent` (line 38):

```python
import speak_to_window
```

- [ ] **Step 2: Add the PTT globals and helpers**

After the `mic_audio_buffer` / `MAX_BUFFER_SIZE` block (~line 83):

```python
# ---------- Speak-to-Window push-to-talk ----------
# While the hotkey is held, mic audio is diverted here INSTEAD of going to
# Gemini Live. Without that gate Ultron hears the instruction meant for the
# focused app and answers aloud over the top of the injection.
#
# This deliberately does not reuse mic_audio_buffer: that buffer is fed by both
# the PyAudio task and the browser's audio_in frames, so with the GUI open it
# holds two interleaved copies of the same microphone, and it is capped at 10s.
ptt_active = False
_ptt_frames = []
PTT_MAX_BYTES = 32000 * 30      # 30s of 16kHz 16-bit PCM

speak_to_window_manager = None


def set_ptt(active: bool):
    """Open or close the push-to-talk gate. Called from the hotkey thread."""
    global ptt_active, _ptt_frames
    if active:
        _ptt_frames = []
    ptt_active = bool(active)


def take_ptt_audio() -> bytes:
    """Drain the frames captured while the hotkey was held."""
    global _ptt_frames
    pcm = b"".join(_ptt_frames)
    _ptt_frames = []
    return pcm
```

- [ ] **Step 3: Tee the PyAudio stream and gate Live**

Replace lines 540–552 of `send_audio_task` (the `if data:` block) with:

```python
            if data:
                mic_audio_buffer.extend(data)
                if len(mic_audio_buffer) > MAX_BUFFER_SIZE:
                    mic_audio_buffer = mic_audio_buffer[-MAX_BUFFER_SIZE:]

                if ptt_active:
                    # Push-to-talk owns the mic: the utterance is for the
                    # focused app, not for Ultron. Divert and do not forward.
                    _ptt_frames.append(data)
                    if sum(len(f) for f in _ptt_frames) > PTT_MAX_BYTES:
                        _ptt_frames.pop(0)
                    continue

                # Only send PyAudio mic data to Gemini if NO WebSocket GUI client is connected
                if not model_is_speaking and not connected_ws_clients:
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=data,
                            mime_type="audio/pcm;rate=16000"
                        )
                    )
```

- [ ] **Step 4: Gate the browser audio path too**

In `ws_handler`, replace line 363 (`if global_live_session:`) with:

```python
                        if global_live_session and not ptt_active:
```

The browser frames are not teed into `_ptt_frames` — the PyAudio stream is the single authoritative source for push-to-talk, so the capture stays one clean mono stream.

- [ ] **Step 5: Start the manager in `run_ultron`**

After `agent_results_task = asyncio.create_task(agent_result_dispatcher())` (~line 1973):

```python
    global speak_to_window_manager
    speak_to_window_manager = speak_to_window.SpeakToWindow(
        get_client=lambda: genai_client,
        broadcast=broadcast_event,
        log_event=log_interaction,
        set_ptt=set_ptt,
        take_audio=take_ptt_audio,
        loop=main_loop,
    )
    speak_to_window_manager.start()
```

- [ ] **Step 6: Stop it in the teardown**

In the `finally` block, immediately before the task cancellations:

```python
        if speak_to_window_manager:
            speak_to_window_manager.stop()
```

- [ ] **Step 7: Verify the module still imports and the suite still passes**

Run: `.venv/bin/python -c "import ultron_hub; print('import ok', ultron_hub.ptt_active)"`
Expected: `import ok False`

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS, 74 tests.

- [ ] **Step 8: Checkpoint** — leave changes in the working tree, do not commit.

---

### Task 8: GUI mirror — `app.js` and `style.css`

**Files:**
- Modify: `web_gui/app.js` — a `window_action` case in the `handleServerMessage` switch (starts line 306)
- Modify: `web_gui/style.css`
- Modify: `web_gui/index.html`

**Interfaces:**
- Consumes: `{"type": "window_action", "state": "recording"|"processing"|"complete"|"failed", "app": str, "reason": str?}` from Task 7.

- [ ] **Step 1: Add the pill element**

In `index.html`, immediately after the `agent-rail` div:

```html
  <!-- Speak-to-Window: mirrors the native floating pill so the Ultron window
       carries a record of what was injected where -->
  <div class="window-action-pill" id="window-action-pill">
    <span class="wa-dot"></span>
    <span class="wa-label" id="wa-label">LISTENING</span>
    <span class="wa-app" id="wa-app"></span>
  </div>
```

- [ ] **Step 2: Add the case to `handleServerMessage`**

In `app.js`, inside the `switch (msg.type)` block, after the `case "system_telemetry":` block:

```javascript
    case "window_action":
      showWindowAction(msg.state, msg.app, msg.reason);
      break;
```

- [ ] **Step 3: Add the handler**

Near the other UI helpers in `app.js`:

```javascript
// ---- Speak-to-Window status pill -------------------------------------------
// The native NSPanel is what the user actually sees mid-action — the Ultron
// window is behind whatever app they are typing into. This is the record.
const WINDOW_ACTION_LABELS = {
  recording: "LISTENING",
  processing: "TRANSFORMING",
  complete: "INJECTED ✓",
  failed: "FAILED",
};
let windowActionTimer = null;

function showWindowAction(state, app, reason) {
  const pill = document.getElementById("window-action-pill");
  const label = document.getElementById("wa-label");
  const appEl = document.getElementById("wa-app");
  if (!pill || !WINDOW_ACTION_LABELS[state]) return;

  label.textContent = WINDOW_ACTION_LABELS[state];
  appEl.textContent = reason ? `${app || ""} — ${reason}` : (app || "");
  pill.dataset.state = state;
  pill.classList.add("visible");

  clearTimeout(windowActionTimer);
  if (state === "complete" || state === "failed") {
    windowActionTimer = setTimeout(() => pill.classList.remove("visible"), 2400);
  }
}
```

- [ ] **Step 4: Add the styles**

Append to `style.css`, reusing the existing glass tokens rather than inventing colours:

```css
/* ---- Speak-to-Window status pill ---- */
.window-action-pill {
  position: fixed;
  top: 84px;
  left: var(--gutter, 18px);
  z-index: 40;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 13px;
  border-radius: 999px;
  background: rgba(8, 12, 18, 0.72);
  border: 1px solid rgba(120, 180, 220, 0.18);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  font-family: "JetBrains Mono", monospace;
  font-size: 10.5px;
  letter-spacing: 0.08em;
  opacity: 0;
  transform: translateY(-6px);
  pointer-events: none;
  transition: opacity 0.22s ease, transform 0.22s ease;
}

.window-action-pill.visible { opacity: 1; transform: translateY(0); }

.window-action-pill .wa-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: #22d3ee;
  box-shadow: 0 0 8px currentColor;
}

.window-action-pill .wa-label { color: #cfe9f5; }

.window-action-pill .wa-app {
  color: rgba(200, 220, 235, 0.5);
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.window-action-pill[data-state="recording"] .wa-dot {
  background: #22d3ee;
  animation: wa-pulse 1.1s ease-in-out infinite;
}

.window-action-pill[data-state="processing"] .wa-dot {
  background: #a78bfa;
  animation: wa-pulse 0.6s ease-in-out infinite;
}

.window-action-pill[data-state="complete"] .wa-dot { background: #4ade80; }
.window-action-pill[data-state="failed"]   .wa-dot { background: #fbbf24; }

@keyframes wa-pulse {
  0%, 100% { opacity: 1;   transform: scale(1); }
  50%      { opacity: 0.4; transform: scale(0.7); }
}
```

- [ ] **Step 5: Verify in the browser**

Start Ultron (`.venv/bin/python app_desktop.py`), then from a console:

```javascript
showWindowAction("recording", "Visual Studio Code");
showWindowAction("complete", "Visual Studio Code");
```

Expected: the pill fades in under the agent rail, cyan pulsing then green, auto-hiding after ~2.4s.

- [ ] **Step 6: Checkpoint** — leave changes in the working tree, do not commit.

---

### Task 9: Smoke harness, README, and end-to-end verification

**Files:**
- Create: `test_speak_to_window.py` (repo root — a manual harness, matching the existing `test_conn.py` convention, not a pytest file)
- Modify: `readme.md`
- Modify: `.env.template`
- Modify: `.gitignore`

- [ ] **Step 1: Keep the manual harness out of the pytest run**

`pytest.ini` already sets `testpaths = tests`, so a root-level `test_speak_to_window.py` is not collected. Confirm:

Run: `.venv/bin/python -m pytest --collect-only -q | tail -3`
Expected: only files under `tests/` listed.

- [ ] **Step 2: Write the smoke harness**

Create `test_speak_to_window.py`:

```python
"""
Manual smoke harness for Speak-to-Window.

The unit suite under tests/ covers everything that can run without a real
machine. These are the layers that cannot: Accessibility reads against live
apps, synthetic paste, and the floating panel. Run each one, focus the app named
in the prompt, and check the result with your eyes.

    .venv/bin/python test_speak_to_window.py context
    .venv/bin/python test_speak_to_window.py inject
    .venv/bin/python test_speak_to_window.py hud
    .venv/bin/python test_speak_to_window.py secure
"""

import json
import sys
import time


def countdown(message, seconds=4):
    print(f"\n{message}")
    for remaining in range(seconds, 0, -1):
        print(f"  {remaining}...", end="\r", flush=True)
        time.sleep(1)
    print("  capturing now.   ")


def check_context():
    import window_context
    countdown("Focus a text field in ANY app and select some text.")
    ctx = window_context.get_focused_window_context()
    rect = ctx.pop("caret_rect", None)
    print(json.dumps(ctx, indent=2, ensure_ascii=False)[:1500])
    print(f"caret_rect: {rect}")
    print("\nEXPECT: app_name matches the app, has_selection true, "
          "selected_text matches the highlight, caret_rect is not None.")


def check_secure():
    import window_context
    countdown("Focus a PASSWORD field (a login form, or System Settings).")
    ctx = window_context.get_focused_window_context()
    print(f"role={ctx['role']!r} blocked={ctx['blocked']} "
          f"selected={ctx['selected_text']!r} context={ctx['context_text']!r}")
    if ctx["blocked"]:
        print("\nPASS: secure field refused, no text captured.")
    else:
        print(f"\nCHECK: role was {ctx['role']!r}. If that is genuinely a secure "
              "field, add it to window_context.SECURE_ROLES.")


def check_inject():
    import text_injector
    countdown("Focus a TextEdit window and put the cursor where text should land.")
    ok, strategy = text_injector.inject_text("Ultron speak-to-window smoke test.")
    print(f"\ninjected={ok} strategy={strategy!r}")
    print("EXPECT: the sentence appears at the cursor with NO trailing newline, "
          "and Cmd+Z undoes it in one press.")
    print("EXPECT: your clipboard still holds whatever it held before this ran.")


def check_hud():
    import native_hud
    hud = native_hud.StatusHUD()
    if not hud.available():
        print("No NSApplication is running — the HUD degrades to the WS mirror. "
              "Run this from inside app_desktop.py to see the panel.")
        return
    for state in ("listening", "transforming", "injected", "failed"):
        print(f"showing {state}")
        hud.show(state, (600, 400, 2, 20))
        time.sleep(1.4)
    hud.hide()
    print("EXPECT: a pill appeared over whatever app is frontmost, "
          "cycling cyan, violet, green, amber.")


CHECKS = {"context": check_context, "secure": check_secure,
          "inject": check_inject, "hud": check_hud}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in CHECKS:
        print(__doc__)
        sys.exit(1)
    CHECKS[name]()
```

- [ ] **Step 3: Run each smoke check**

```bash
.venv/bin/python test_speak_to_window.py context
.venv/bin/python test_speak_to_window.py secure
.venv/bin/python test_speak_to_window.py inject
```

Expected, in order: a JSON context naming the focused app with the selection; `blocked=True` on a password field; the sentence landing at the cursor, undoable with one `Cmd+Z`, clipboard intact.

- [ ] **Step 4: End-to-end test in the real app**

```bash
.venv/bin/python app_desktop.py
```

Then, with Ultron running:
1. Open TextEdit, type "i went to the shops and i buyed some milk", select it.
2. Hold `Cmd+Shift+Space`, say "fix the grammar", release.
3. Expected: a cyan `LISTENING` pill by the caret → violet `TRANSFORMING` → green `INJECTED ✓`; the sentence is corrected; **Ultron does not speak**; `Cmd+Z` restores the original.
4. Open Terminal, put the cursor at the prompt, hold the hotkey, say "list files by size largest first".
5. Expected: the command appears at the prompt and **does not run** — no trailing newline.
6. Check the Ultron window shows the mirrored pill for both actions.

- [ ] **Step 5: Add the env var to `.env.template`**

```bash
cat >> .env.template <<'EOF'

# Speak-to-Window push-to-talk hotkey.
# Hold it, speak an instruction, release: the transformed text is injected at
# your cursor in whatever app is frontmost. 'fn' is supported but macOS binds it
# to dictation / the emoji picker by default.
ULTRON_PTT_HOTKEY=cmd+shift+space
EOF
```

- [ ] **Step 6: Ignore pytest's cache**

```bash
printf '\n# Test caches\n.pytest_cache/\n' >> .gitignore
```

- [ ] **Step 7: Update `readme.md`**

Four edits, matching the file's existing voice and structure:

1. **`## 🧩 Core Subsystems`** — add a numbered subsystem after §9 (Zero-Trust Remote Access):

```markdown
### 10. Speak-to-Window Native Injection (`speak_to_window.py`, `window_context.py`, `text_injector.py`, `speak_to_window_agent.py`, `hotkey_manager.py`, `native_hud.py`)

Hold `Cmd+Shift+Space`, speak an instruction, release — the text you had
selected in whatever app is frontmost is replaced by the result. No clipboard
juggling, no window switching, and Ultron stays silent throughout.

A listen-only `CGEventTap` detects the chord. On key-down the frontmost app's
Accessibility context is captured immediately (waiting until key-up would risk
reading the wrong window) and mic audio is diverted away from Gemini Live. On
key-up the WAV and the context go to `gemini-3.7-flash` in a single call, and
the reply is injected.

Injection is **paste-first**, with a direct Accessibility write as the fallback.
That ordering looks backwards — an AX write is cleaner and leaves the clipboard
alone — but a simulated `Cmd+V` lands in the target app's own undo stack, so
`Cmd+Z` recovers from a bad transform. Paste also behaves consistently in
Electron apps and terminals, where AX writes frequently no-op in silence.

Two hard rules: a focused `AXSecureTextField` is refused before any text is read,
so password fields never reach the model; and no injected text ever ends in a
newline, which in a terminal is the difference between offering a command and
running it. The buffer sent for context is windowed to ±2000 characters around
the caret rather than dumped whole.

Feedback is a borderless floating `NSPanel` pinned near the caret — the Ultron
window is behind whatever you are typing into, so an in-page pill would be
invisible exactly when it matters. The same states are mirrored into the Ultron
window over the WebSocket as a record.
```

2. **`### Core Highlights`** — add a bullet:

```markdown
- **Speak-to-Window** — hold a hotkey, speak, and have the text at your cursor rewritten in place, in any macOS app.
```

3. **`### 4. Configuration`** — document the new variable:

```markdown
# Speak-to-Window push-to-talk hotkey (default: cmd+shift+space)
ULTRON_PTT_HOTKEY=cmd+shift+space
```

4. **`## 📁 Repository Structure`** — add the six new modules, `tests/`, and `test_speak_to_window.py` with one-line descriptions matching the existing entries' style.

- [ ] **Step 8: Verify the README against the source**

Re-read each claim in the new §10 against the code as written. Every file name, the default hotkey, the model id, the ±2000 window and the secure-field role must match the implementation exactly.

- [ ] **Step 9: Final full verification**

```bash
.venv/bin/python -m pytest tests/ -v
.venv/bin/python -c "import ultron_hub, speak_to_window; print('imports ok')"
git status --short
```

Expected: all tests pass; imports clean; `git status` lists the new and modified files, **uncommitted**.

- [ ] **Step 10: Checkpoint** — leave everything in the working tree and report what changed. Do not commit.

---

## Self-Review

**Spec coverage** — every section of the design maps to a task:

| Spec section | Task |
|---|---|
| §4 `window_context.py` (AX, secure fields, caret window, caret rect) | 1 |
| §4 `text_injector.py` (paste-first, AX fallback, clipboard, no newline) | 2 |
| §4 `speak_to_window_agent.py` (one call, cleaning, refusal abort) | 3 |
| §4 `hotkey_manager.py` (Quartz tap, binding, no new dependency) | 4 |
| §4 `native_hud.py` (NSPanel, coordinate flip, headless degradation) | 5 |
| §3 Flow (capture on key-down, gate, transform, inject) | 6 |
| §4 Hub integration (PTT flag both producers, tee, wiring, log/broadcast) | 7 |
| §4 `app.js` / `style.css` mirror | 8 |
| §5 Testing (unit + smoke harness) | 1–6 (unit), 9 (smoke) |
| §6 Exclusions (no Live tool, no buffer fix, no auto-run, no custom undo) | Global Constraints |
| §7 Open defaults (±2000/4000, cmd+shift+space, 1.2s fade) | 1, 4, 6 |

**Type consistency** — checked across tasks: the context dict keys produced by `build_context` (Task 1) are exactly those read by `build_prompt` (Task 3) and `SpeakToWindow` (Task 6); `inject_text` returns `(bool, str)` in Task 2 and is unpacked as such in Task 6; `pcm_to_wav` is defined in Task 3 and called as `agent.pcm_to_wav(pcm)` in Task 6; the four WebSocket states emitted in Task 6 (`recording`, `processing`, `complete`, `failed`) are exactly the four keys of `WINDOW_ACTION_LABELS` in Task 8; `native_hud.STATES` uses the separate internal names (`listening`, `transforming`, `injected`, `failed`) and those are the only strings passed to `hud.show()`.

**Placeholder scan** — no TBD/TODO, no "add error handling", no "similar to Task N". Every code step carries the actual code.
