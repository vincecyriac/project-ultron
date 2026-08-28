# Speak-to-Window: OS Automation & Native Contextual Text Injection

**Date:** 2026-08-28
**Branch:** `feature/speak-to-window-automation`
**Status:** Design approved, ready for implementation planning

---

## 1. What this is

Hold a global hotkey, speak an instruction, release. Ultron reads the text you
have selected (or the text around your caret) in whatever app is frontmost,
sends it to Gemini with your spoken instruction, and replaces the selection with
the result — without you touching the clipboard, switching windows, or talking
to the assistant.

The existing Ultron surface is conversational: you talk, it answers aloud and
puts cards on screen. This is the opposite shape. It is silent, it targets
another application, and its entire output is text landing at your cursor.

Concretely: select a paragraph in Mail, hold the key, say "make this shorter and
less apologetic", release. The paragraph is replaced. Or put the caret in a
terminal, say "find every file over 100 megs under my home directory", and the
command appears at the prompt — unexecuted.

## 2. Why it is built this way

### It lives inside the hub process

The hotkey listener, context capture, transform and injection are new modules
imported by `ultron_hub.py`, not a separate daemon.

The deciding constraint is the microphone. `send_audio_task` holds a PyAudio
input stream open for the whole session. A second process would need a second
stream on the same device, which is contention we get nothing for. In-process,
the audio is already flowing — push-to-talk just tees off it.

In-process also inherits `main_loop`, the `genai` client, `broadcast_event`,
and `log_interaction`.

The alternative — a helper process talking to the hub over `:8765` — buys crash
isolation for the event tap. That is not worth a duplicated mic and a second
thing to launch, especially since the tap is `kCGEventTapOptionListenOnly` and
so cannot swallow or corrupt input.

A third option, exposing this as a Gemini Live tool instead of a hotkey, was
rejected outright. It is much less code but it is a different feature: no
deterministic push-to-talk, an audible acknowledgement cutting into your work,
and a race where the focused window may change between the utterance and the
tool call.

### Push-to-talk gets its own audio buffer

`mic_audio_buffer` looks like the obvious source. It is not usable here.

It is fed **unconditionally by two producers**: the PyAudio task at
`ultron_hub.py:541` and the browser's `audio_in` handler at `ultron_hub.py:360`.
When the GUI window is open both run, so the buffer holds two interleaved copies
of the same microphone. It is also capped at 10 seconds (`MAX_BUFFER_SIZE`),
which is shorter than a real instruction.

Push-to-talk therefore keeps a separate accumulator appended only from the
PyAudio path — one clean mono stream, no cap beyond a sanity limit.

The interleaving is a pre-existing wrinkle affecting voice enrollment
(`ultron_hub.py:462`). **It is out of scope here** and is not to be fixed as a
side effect of this work.

### Paste before Accessibility

The obvious ordering is AX-direct first (no clipboard pollution, cleanest
mechanism) with paste as fallback. This design inverts it.

A synthetic `Cmd+V` lands in the target app's **native undo stack**. An AX write
to `kAXSelectedTextAttribute` typically does not. When a transform replaces two
hundred lines with something wrong, `Cmd+Z` is the only recovery that exists,
and it has to work. Paste also behaves uniformly in Electron apps (VS Code,
Slack) and terminals, where AX writes frequently no-op in silence — so the
fallback would fire constantly anyway.

AX-direct remains the fallback for apps that block synthetic paste.

### One Gemini call, not two

The WAV goes inline to `gemini-3.7-flash` alongside the app context; the model
hears the instruction and emits the replacement in a single round trip. Two
calls (transcribe, then transform) would make failures legible — you would see
whether it misheard or misunderstood — at roughly double the latency.

Latency wins. This runs in the middle of someone's typing; a second round trip
is felt. The accepted cost is that a bad result is ambiguous.

## 3. Flow

```
key down
   ├─ capture window context NOW (before focus can shift)
   ├─ set ptt_active — mic stops feeding Gemini Live
   └─ show HUD panel at caret: LISTENING
key up
   ├─ clear ptt_active, close the audio accumulator
   ├─ HUD: TRANSFORMING
   ├─ WAV + context + app name → gemini-3.7-flash
   ├─ clean response; abort if empty or refusal-shaped
   ├─ inject (paste, then AX)
   ├─ HUD: INJECTED ✓ / FAILED, fading after ~1.2s
   └─ log_interaction + broadcast_event
```

Context capture happens on **key down**, not key up. By key-up the frontmost
application may have changed, and capturing then would read the wrong window.

## 4. Modules

### `window_context.py`

```
get_focused_window_context() -> dict
```

`NSWorkspace.frontmostApplication()` for name, pid and bundle id, then
`AXUIElementCreateApplication(pid)` → `kAXFocusedUIElementAttribute`.

Four things differ from a naive implementation:

**Constants come from `ApplicationServices`, not `Cocoa`.** Verified in this
project's venv: `Cocoa.kAXFocusedUIElementAttribute` raises `AttributeError`.
Every `kAX*` name — `kAXFocusedUIElementAttribute`, `kAXSelectedTextAttribute`,
`kAXValueAttribute`, `kAXSelectedTextRangeAttribute`, `kAXRoleAttribute`,
`kAXBoundsForRangeParameterizedAttribute` — resolves on `ApplicationServices`.

**Secure fields are refused.** If the focused element's `kAXRoleAttribute` is
`AXSecureTextField`, return `{"blocked": True}` and capture no text at all.
Password managers and login forms must never reach Gemini. This check runs
before any text is read, not after.

**The buffer is windowed, not dumped.** A focused VS Code editor's
`kAXValueAttribute` may be an entire file. Read `kAXSelectedTextRangeAttribute`
for the caret offset and take ±2000 characters around it, capped at 4000 total,
with an explicit marker showing where the caret sits. Full-buffer dumps cost
latency, tokens, and privacy for no gain.

**Caret geometry drives the HUD.** `kAXBoundsForRangeParameterizedAttribute`
over the selected range gives the caret rect. Degrade in order: caret rect →
focused element frame → current mouse position.

Returns: `app_name`, `pid`, `bundle_id`, `role`, `selected_text`,
`context_text`, `has_selection`, `caret_rect`, `blocked`.

Every AX call is individually guarded. An app that answers nothing yields a
context with `app_name` set and text fields `None` — that is a valid result
meaning "generate fresh text here", not an error.

### `text_injector.py`

```
inject_text(text, preserve_clipboard=True) -> (ok: bool, strategy: str)
```

The strategy name is returned so the HUD and the log can say which path ran.

**Strategy 1 — paste.** Snapshot the pasteboard across *all* types, not just
`NSStringPboardType`, so an image or file on the clipboard survives the round
trip. Set the string, post `Cmd+V` via `CGEventPost` on `kCGHIDEventTap` with
`kCGEventFlagMaskCommand` set on both key-down **and** key-up, restore after a
short delay.

**Strategy 2 — AX direct.** `AXUIElementSetAttributeValue(elem,
kAXSelectedTextAttribute, text)` for apps that block synthetic paste. AX writes
fail silently, so the result is read back and compared before reporting success.

Two invariants:

- **Never inject a trailing newline.** In a terminal that is the difference
  between offering a command and running it. Strip trailing newlines from every
  response on every path.
- **The clipboard is always restored**, including on exception, via
  `try/finally`.

### `speak_to_window_agent.py`

```
async process_window_voice_action(client, ctx: dict, wav_bytes: bytes) -> str
```

`gemini-3.7-flash`, `temperature=0.1`, WAV as an inline `types.Part` alongside
the assembled text prompt. Follows the `widget_generator_agent.py` shape: a
module-level system prompt, one `client.aio.models.generate_content` call, a
timeout, and cleaning on the way out.

System prompt is the one from the brief — pure output, no conversational
framing, match the target app's conventions — plus:

- Never end output with a newline.
- If the instruction is a question rather than an edit, return the answer as
  text to insert. Do not converse.
- Markdown fences are stripped unless the target is a code editor or terminal.

**Empty or refusal-shaped responses abort the injection.** Pasting "I'm sorry, I
can't help with that" into someone's document is worse than doing nothing.

### `hotkey_manager.py`

`CGEventTapCreate` at `kCGSessionEventTap` with `kCGEventTapOptionListenOnly`,
masking `keyDown`, `keyUp` and `flagsChanged`, on a daemon thread running its
own `CFRunLoop`. All symbols verified present in this venv. **No new
dependency** — `pynput` is not needed and is not installed.

Binding parsed from `ULTRON_PTT_HOTKEY`, default `cmd+shift+space`. `fn` is
supported (`kCGEventFlagMaskSecondaryFn` via `flagsChanged`) but is not the
default, because macOS binds it to dictation or the emoji picker out of the box.

Key-down and key-up call injected callbacks. The manager itself knows nothing
about audio, Gemini or injection — it translates events into two calls. Repeat
key-downs while already held are ignored.

Hands off to the event loop with
`asyncio.run_coroutine_threadsafe(..., main_loop)`. `main_loop` is already a
hub-level global set in `run_ultron`.

If `AXIsProcessTrusted()` is false the tap will not arm. Log a clear line naming
Accessibility and continue — the rest of Ultron must still boot.

### `native_hud.py`

Borderless non-activating `NSPanel` at `NSFloatingWindowLevel`, with
`collectionBehavior` set to join all spaces and be ignored by Exposé, positioned
at the caret rect. AX reports top-left origin and Cocoa expects bottom-left, so
the y coordinate is flipped against the screen containing the point.

All UI work is dispatched to the main queue — PyWebView owns the macOS main
thread and AppKit is not thread-safe.

**If no `NSApplication` is running** — the `run_headless` path in
`app_desktop.py` — the panel degrades to the WebSocket event alone. It must not
raise.

States: `LISTENING` (cyan pulse), `TRANSFORMING` (shimmer), `INJECTED ✓` (green,
auto-fade ~1.2s), `FAILED` (amber, with the reason).

### Hub integration (`ultron_hub.py`)

- A `ptt_active` flag gates mic forwarding to Live in **both** producers
  (`ultron_hub.py:546` and `ultron_hub.py:363`). Without this, Ultron hears the
  instruction and answers aloud over the top of the injection.
- `_ptt_frames`, appended only from the PyAudio path in `send_audio_task`.
- The hotkey manager starts alongside the other background workers in
  `run_ultron` (near `ultron_hub.py:1967`) and is torn down in the same `finally`
  block as `playback_task` and friends.
- Every completed action emits `log_interaction("window_action", …)` and
  `broadcast_event({"type": "window_action", "state": ..., "app": ...})`.

### `web_gui/app.js`, `web_gui/style.css`

A `window_action` case in the `handleServerMessage` switch (`app.js:306`)
mirroring the four states as a pill, reusing the existing glassmorphic tokens
rather than inventing colours. This is a **record** of what happened — the
native panel is what the user actually sees mid-action.

## 5. Testing

Unit-testable with no microphone, no permissions and no network:

- hotkey string parsing (`cmd+shift+space`, `fn`, malformed input)
- prompt assembly from a context dict
- response cleaning: fence stripping, trailing-newline removal, refusal
  detection, empty response
- caret-window extraction: caret at start, at end, mid-buffer, buffer shorter
  than the window
- clipboard save/restore round trip, including the exception path

Needing a real machine, via a `test_speak_to_window.py` smoke harness that
prints captured context and injects a fixed string so each layer is checkable
against real apps:

- AX capture against TextEdit, VS Code, Mail, Terminal
- secure-field refusal against a real password field
- paste and AX injection paths
- panel placement across displays and Spaces

## 6. Deliberately excluded

- No Gemini Live tool for this. Hotkey-triggered only.
- No fix for the `mic_audio_buffer` double-feed. Noted, separate concern.
- No command execution, ever. Terminal targets receive text with no trailing
  newline.
- No injection history or Ultron-owned undo. The app's own `Cmd+Z` is the
  recovery path, which is why paste comes first.

## 7. Open defaults

Chosen here, cheap to change, called out so they are not mistaken for
requirements:

- ±2000 character caret window, 4000 cap
- `cmd+shift+space` rather than `fn`
- ~1.2s HUD fade after success
