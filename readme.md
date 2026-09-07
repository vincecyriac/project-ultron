# Project FRIDAY

> **An Autonomous, Multimodal AI Desktop Assistant & Spatial Operating System for macOS**
> Voice-first, Gemini-only. A holographic orb you talk to, a deck of live data widgets it composes for you, an interactive 3D spatial engine, and deep native OS control — with background agents doing the heavy lifting off the audio path.

---

## 🌟 Overview

**Project FRIDAY** is a personal, multimodal desktop intelligence system for macOS. You speak; it answers in under ten words and puts the substance on screen. Detail lives in widgets — charts, metric matrices, intelligence feeds, embedded web pages, live 3D scenes — never in a wall of narration.

### Core Highlights

- 🎙️ **Real-Time Voice Streaming** — Full-duplex conversational audio over the Gemini Live WebSocket API (`gemini-3.1-flash-live-preview`), 16 kHz PCM in / 24 kHz out, with natural barge-in. Natural, articulate **Aoede** voice by default.
- 🔮 **Ambient Holographic Orb** — A Three.js plasma core that *is* the status display. Colour is bound to state and holds steady until the state changes; motion reacts to what you actually hear.
- 🧩 **Async Widget Deck** — Asking for data mounts a shimmering skeleton card *instantly*; a background generator writes the finished HTML — hero figures, inline SVG charts, metric matrices — and it hydrates in place a few seconds later. The voice never waits on layout.
- 🤖 **Tiered Background Agents** — Live stays responsive for barge-in and dispatches multi-step work to specialised models: **Gemini 3.1 Pro** for macOS automation, **Gemini 3.7 Flash** for spatial scene generation.
- 🌐 **Spatial Visualization Engine (SVE)** — Live, persistent, interactive 3D scene graphs in Three.js with object-level delta updates and local MediaPipe hand-gesture control.
- 🖥️ **Multi-Monitor Vision & OS Automation** — Quartz display enumeration, context-aware capture, and hardware-level `CGEvent` mouse/keyboard injection across every display.
- 👤 **Biometric Identity** — Local ONNX face recognition (YuNet + SFace) and MFCC voice profiling, entirely on-device.
- 📅 **Native Workspace Integration** — macOS EventKit calendars and Apple Mail via PyObjC and AppleScript.
- 🔒 **Zero-Trust Remote Access** — Reach it from your phone over a private Tailscale mesh, with one-tap approval for every shell command.

---

## 🖥️ The Interface

### Workspace tabs

The workspace is split in two, behind a tab bar carrying live counts:

- **Cards** — HTML and component widgets, stacked newest-on-top and scrollable, exactly as before.
- **3D** — every 3D surface, whether an SVE scene (`3d_spatial`) or a generated Tripo asset (`3d_asset`). **Only one is visible at a time**, filling the full workspace height. The rest stay mounted with their WebGL context and camera intact but stop rendering, so nothing is re-loaded when you switch back.

Switch by clicking a pill above the stage, or ask FRIDAY — `show_3d_view` takes `'spatial'` for the scene or a model's name, and is the only thing that moves the selection. A newly arrived model claims the slot and raises the 3D tab, the same way a new card raises Cards. Re-showing a saved model reuses its card (ids are derived from the file), so asking for the same one twice never stacks a second pill.

Hand tracking follows the visible model: switching to the scene hands gestures back to the SVE, switching to a model points them at that model.

Both 3D types share one chrome: the pills name them, so neither card carries its own header, and both use the same floating **Hands** / **Reset view** pills over a full-height stage.


The GUI is a spatial workspace with no chrome — no title bar, no tabs, no status text. Every surface owns a reserved region, so nothing ever overlaps.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 21:04:18 │ CPU 38% │ RAM 12.8/16 GB          ← ambient telemetry HUD     │
│ ◐ OS agent · Open Safari and summarise…      ← background agent chips    │
│                                                                          │
│      ╭─────────╮        ┌──────────────────────────────────────────────┐ │
│      │  ORB    │        │  ALPHABET INC.                            ×  │ │
│      │ (state) │        │  207.42  USD              ▲ +3.81 +1.87%     │ │
│      ╰─────────╯        │  ╭────────────────────────────────────────╮  │ │
│                         │  │  gradient area chart, dashed baseline  │  │ │
│    ← 260px rail         │  ╰────────────────────────────────────────╯  │ │
│                         │  OPEN 204.10   HIGH 209.94   LOW 203.44      │ │
│                         └──────────────────────────────────────────────┘ │
│  ┌───────────┐          ┌──────────────────────────────────────────────┐ │
│  │ camera PIP│          │  MARKET INTELLIGENCE                      ×  │ │
│  │ (mirrored)│          │  01 [ALERT] Antitrust ruling lands Tuesday   │ │
│  └───────────┘          └──────────────────────────────────────────────┘ │
│  ╭ Speak, or type… ╮                                   ╭─ 🎙 📷 🖥 ─╮     │
└──────────────────────────────────────────────────────────────────────────┘
```

### Layout states

Driven purely by how many widgets are mounted:

| Widgets | Layout |
|---|---|
| **0** | Orb dead-centre at full scale; workspace collapsed (`opacity: 0`, no pointer events) |
| **≥ 1** | Orb glides to the 260 px left rail at ~38 % scale; deck fills the right, newest card on top |

Transitions interpolate over `0.6s cubic-bezier(0.16, 1, 0.3, 1)`, and both WebGL renderers are re-fitted during the animation so nothing stretches.

### The orb as status

State drives colour; colour holds until the state changes. Nothing else can shift the hue — audio drives motion and brightness only.

| State | Colour | Meaning |
|---|---|---|
| Idle | `#00F2FE` calm cyan | Connected, waiting |
| Listening | `#0077FF` deep blue | Your voice is coming in |
| Thinking | `#FFB800` amber | Tool running or agent working |
| Speaking | `#00FF88` emerald | FRIDAY is talking |
| Offline | `#E5726F` ember | Hub unreachable |

Over all five, a fixed violet-to-blush accent (`#A18CD1` → `#FBC2EB`) tints the rim highlight and the outer bloom. It carries no state — it is purely FRIDAY's finish, and the state hue stays exactly as readable as before.

The "speaking" state follows the hub's authoritative turn status and the **playback timeline** — audio arrives roughly 3× faster than it plays, so the orb animates in step with what you *hear*, not with what has downloaded. Tap the orb to interrupt.

### Ambient furniture

- **Telemetry HUD** (top-left) — live clock, CPU and RAM, updated every 2 s from the hub.
- **Agent chips** — one per running background agent, with a spinner and its goal.
- **Camera PIP** (bottom-left) — mirrored; the hand-landmark overlay is mirrored with it so the skeleton stays registered.
- **Command lane** (bottom-centre) — ghosted until you hover, focus, or simply start typing.
- **Sensor dock** (bottom-right) — three icon toggles: mic, camera, screen. Active glows cyan; inactive is ghosted with a strike.

---

## 🏗️ System Architecture

```
            ┌─────────────────────────────────────────────────────────┐
            │                  USER INTERFACES                        │
            │  • Web GUI (Orb / Widget Deck / Spatial 3D / Gestures)  │
            │  • PyWebView desktop shell  • Phone via Tailscale HTTPS │
            │  • Voice In / Out (16 kHz PCM Mic / 24 kHz Speaker)     │
            └───────────────▲─────────────────────────▲───────────────┘
                            │ (WebSocket / Audio)     │ (Touch / Video)
                            ▼                         ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│                                FRIDAY ENGINE HUB (friday_hub.py)                          │
│                                                                                           │
│  ┌───────────────────────────┐  ┌───────────────────────────┐  ┌────────────────────────┐ │
│  │    Audio Pipeline         │  │   State & Event Hub       │  │  Gemini Live Session   │ │
│  │ • PyAudio 16kHz/24kHz     │  │ • Play Queue / Interrupts │  │ • Aoede voice, tools   │ │
│  │ • Voice Enrollment Buffer │  │ • Widget Deck Registry    │  │ • Per-turn receive loop│ │
│  │ • Remote Audio Routing    │  │ • Approval State Machine  │  │ • In-run resumption    │ │
│  └───────────────────────────┘  └───────────────────────────┘  └────────────────────────┘ │
└──────┬────────────────────┬───────────────────┬────────────────────────┬──────────────────┘
       │ Tool Calls         │ Dispatch          │ Scene Ops              │ Biometrics
       ▼                    ▼                   ▼                        ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
│ CAPABILITY       │ │ BACKGROUND AGENTS│ │ SPATIAL ENGINE (SVE) │ │ RECOGNITION & MEMORY │
│ SUBSYSTEMS       │ │ friday_agents.py │ │                      │ │                      │
│                  │ │                  │ │ • sentry_scene.py    │ │ • sentry_recognition │
│ • sentry_vision  │ │ • os tier        │ │   SceneGraph deltas  │ │   YuNet + SFace 128-d│
│ • sentry_action  │ │   Gemini 3.1 Pro │ │ • web_gui/sve.js     │ │   Mel MFCC voice     │
│ • sentry_exec    │ │ • spatial tier   │ │   Three.js renderer  │ │ • friday_memory.json │
│ • sentry_personal│ │ • widget writer  │ │ • web_gui/gestures.js│ │   Persistent facts   │
│ • sentry_web     │ │   Gemini 3.7 Fl. │ │   MediaPipe hands    │ │                      │
└──────────────────┘ └──────────────────┘ └──────────────────────┘ └──────────────────────┘
```

---

## 🧩 Core Subsystems

### 1. Gemini Live Hub (`friday_hub.py`)
- **Bidirectional voice** over WebSockets to `gemini-3.1-flash-live-preview`, typed `LiveConnectConfig` with the **Aoede** prebuilt voice (override with `FRIDAY_VOICE`).
- **Per-turn receive loop** — `session.receive()` yields one conversational turn and ends, so the loop re-enters it. Without that the session tears down after every reply and the reconnect replays the last turn.
- **Session lifecycle** — the resumption handle is held **in memory only**, bridging GoAway rotations and drops *within* a run. A new process is always a new conversation.
- **Persona** — `FRIDAY_SYSTEM_INSTRUCTION` holds who she is and how she sounds: perceptive, effortlessly competent, dryly witty, subtly warm. It is a standalone constant, composed with the operational rules by `build_system_instruction()`, so the voice can be retuned without touching the tool, widget and safety instructions.
- **Frontend assets are never cached stale** — the GUI server sends `Cache-Control: no-cache, must-revalidate`. Without it WKWebView applies heuristic freshness and can keep rendering an old `app.js`/`style.css` across relaunches (the desktop shell runs `private_mode=False`, so its store survives restarts). ETags are kept, so unchanged files still cost only a 304.
- **Concise by default** — spoken replies stay under ten words; when a widget is mounted, one or two sentences carrying the key takeaway. Detail belongs on screen.

### 2. Background Agent Tiers (`friday_agents.py`)
Live must stay free for barge-in, so anything multi-step is dispatched off the audio path via `dispatch_agent`. FRIDAY acknowledges in the same turn ("Working on that now.") and announces the result when it lands.

| Tier | Model | Handles |
|---|---|---|
| `os` | `gemini-3.1-pro-preview` | macOS automation, AppleScript/shell chains, GUI operation |
| `spatial` | `gemini-3.7-flash` | Building and editing 3D SVE scenes |
| widget generator | `gemini-3.7-flash` | Writing card HTML (see below) |

Agents reuse the hub's own tool implementations, run up to 12 tool round-trips, and report back a single spoken sentence. Results are **queued for a gap in the conversation** — a finished agent can never cut FRIDAY off mid-sentence.

### 3. Async Widget Deck (`friday_hub.py`, `widget_generator_agent.py`, `web_gui/app.js`)

Composing a data-dense card takes seconds. Doing that inside the voice turn would stall the conversation, so the work is split in two:

```
  user speaks
       │
       ▼
  Gemini Live ──► speaks the takeaway (1-2 sentences)
       │
       └──► create_skeleton_widget(widget_id, title, query_context)   returns in ~0ms
                    │
                    ├──► broadcast "create_skeleton"  → card appears, shimmering
                    │
                    └──► background task: gemini-3.7-flash writes the card HTML
                                  │
                                  ▼
                         broadcast "patch_content"   → skeleton fades, card hydrates
```

Measured end to end: the tool returns at **+0.00s**, the skeleton is on screen in the same frame, and content patches in at **~4–12s** depending on whether the generator needs to search for live figures.

- **The hub owns the deck** (max 8 cards). A client connecting late gets a full `sync` snapshot; a card dismissed mid-generation is never patched.
- **`query_context` is the contract.** The generator cannot see the conversation — it receives only that string, so Live is instructed to spell out every figure and section the card should carry.
- **Everything generated is sanitised before it reaches the DOM.** `<script>`, `<iframe>`, `<style>`, `<link>`, inline `on*` handlers and `javascript:` URLs are stripped hub-side. The generator is *told* not to emit them; the sanitiser is what guarantees it.
- **The generator writes no CSS.** It composes from a fixed set of `hud-*` classes defined in `style.css`, which is what keeps model-authored markup looking like the rest of the app.
- **Charts are earned, not decorative.** One is drawn only when there is a genuine series over time or a set of comparable magnitudes. A quote card gets a chart; a headlines card, a password or a weather summary does not.
- **Images are fetched, verified, then proxied.** Two failure modes are real and both were observed: a model reconstructing a plausible CDN path that answers `401`, and a genuine article image that answers `403` to a direct browser request because of hotlink protection. So the hub fetches each candidate itself with a browser user-agent and the image's own origin as `Referer`, drops whatever cannot be retrieved, and rewrites the survivors to `/img?u=…` — a local endpoint that streams the bytes from the hub. The browser only ever loads images from localhost, and a card never shows a broken frame.

### 4. Multi-Monitor Vision & Spatial Targeting (`sentry_vision.py`)
- **Quartz display enumeration** — all monitors, scaling factors, desktop arrangement.
- **Context-aware capture** — the display holding the frontmost window, a specific display, or a labelled composite of all.
- **Global coordinate tracking** — `LAST_CAPTURE_BOUNDS` maps normalized 0–1000 model coordinates back to physical pixels.

### 5. Native macOS Desktop Automation (`sentry_action.py`, `sentry_exec.py`)
- **Hardware-level input** via `CGEventPost` (`kCGHIDEventTap`), bypassing PyAutoGUI's single-monitor clamping.
- **Accessibility inspector** (`read_ui_elements`) reads the frontmost app's AX tree for exact control positions.
- **Window & Spaces enumeration** (`list_open_windows`) across the Quartz Window Server.
- **Shell and AppleScript execution** with timeouts and the remote approval gate.

### 6. Biometric Face & Voice Recognition (`sentry_recognition.py`)
- OpenCV **YuNet** ONNX detection + **SFace** 128-d embeddings, cosine-matched against `friday_profiles.json`.
- **Voice fingerprinting** — pure-NumPy MFCCs pooled by mean and variance from the rolling mic buffer.

### 7. Spatial Visualization Engine (`sentry_scene.py`, `web_gui/sve.js`, `web_gui/gestures.js`)
- **Persistent 3D workspace** — scenes live in `friday_scenes.json` and stay on stage until dismissed.
- **Incremental delta protocol** — update, rotate, recolour, highlight, hide, or explode individual objects; never a full rebuild.
- **Screen-sized labels** — annotations are sized to a constant on-screen height, depth-tested so geometry occludes them, and decluttered by screen-space overlap (12 visible at once, nearest first).
- **Markerless hand tracking** — vendored MediaPipe HandLandmarker WASM: point to hover, pinch to grab, pinch empty space to orbit, two-hand pinch to zoom.

### 8. Generated 3D Assets (`services/asset_generator.py`, `web_gui/asset_viewer.js`)
- **Text-to-3D via Tripo3D** — `generate_spatial_3d_asset` turns a description into a textured `.glb`. Requires `TRIPO_API_KEY`; without it the SVE still works and only this tool is unavailable.
- **Never on the audio path** — generation takes tens of seconds, so the tool returns instantly, mounts a card, and runs the poll loop as a background task. The card shows live progress and the model drops in when it lands, exactly like the widget generator.
- **PBR viewer** — `GLTFLoader` with a `RoomEnvironment` image-based light plus cyan key and violet rim lights, orbit controls, and auto-framing (models arrive at arbitrary scale, so each is normalised to unit size and the camera fitted to it).
- **Downloaded, not hotlinked** — the hub fetches the `.glb` server-side and saves it to `generated_assets/`, then serves it from `/assets`. The CDN sends no CORS headers and signs its URLs with an expiry, so a card pointed straight at it fails even though generation succeeded.
- **Models persist** — every asset is content-addressed (`<slug>_<sha1>.glb`) and indexed in `friday_assets.json`. `list_3d_assets` shows what exists and `show_3d_asset` re-mounts one instantly for free, so asking for the same model twice never costs a second credit. Both are git-ignored; delete the two lines in `.gitignore` if you want them committed.
- **Hand-gesture control, on by default** — a model takes gesture focus the moment it mounts and starts hand tracking itself, the way a live scene does. Pinch-drag to grab and move it, pinch empty space or open palm to orbit, two-hand pinch to zoom. The card carries the same **Hands** / **Reset view** pills as the spatial card, and fills the available height (`min(62vh, 560px)`) rather than a fixed box. `gestures.js` now resolves a *target* rather than calling `window.SVE` directly, and a focused card implements the same surface (`pickAt` / `select` / `moveSelectedTo` / `orbitCamera` / `dollyCamera` / `commitSelectedMove` / `register+unregisterInputSource`). With no card focused the target is the SVE, exactly as before. The gesture cursor and HUD move into the focused card and back on release.
- **Context-safe** — every card owns a WebGL context and browsers cap those, so dismissing a card disposes its renderer, geometry, and textures.

**This is not a replacement for the SVE.** A generated asset is one photoreal object: no ids, no labels, no edits, ~a minute, and a credit per call. Anything structural or editable — molecules, orbits, flowcharts, anatomy, data — stays an SVE scene, which is instant, free, and updatable. The persona instruction and the tool description both enforce that split.

### 9. Personal Productivity Suite (`sentry_personal.py`)
- **EventKit calendars** via PyObjC across iCloud, Google, and Exchange.
- **Apple Mail** via AppleScript — read recent mail, search sender/subject.

### 10. Zero-Trust Remote Access (`setup_remote.sh`, Tailscale)
- The hub binds only to `127.0.0.1`; remote access is tunnelled through Tailscale Serve HTTPS.
- **Human-in-the-loop approvals** — while a remote client is connected, every shell and AppleScript call suspends for one-tap approval with a 45-second auto-deny.
- **Smart sensor routing** — the phone's mic and camera become the primary senses; the unattended Mac's webcam and screen are left alone unless asked for explicitly.

---

## 🧱 Card Design Tokens

The generator is given this vocabulary and nothing else — no `<style>` blocks, no invented colours. Every class below is defined in `web_gui/style.css`:

| Class | Renders |
|---|---|
| `.hud-hero-stat` / `.hud-hero-row` / `.hud-sub` | Large monospace headline figure with a glowing accent, its label and caption |
| `.hud-badge-green` / `-red` / `-cyan` / `-amber` | Delta and status pills |
| `.hud-metric-grid` + `.hud-metric` | Three-column key/value matrix |
| `.hud-feed` + `.hud-feed-row` | Numbered rows with category tag, headline and brief |
| `.hud-svg-chart` | Wrapper for an inline `<svg viewBox="0 0 400 120">` — gradient area fill, glowing stroke, dashed reference line |
| `.hud-bar` | Linear meter |
| `.hud-note` | Closing one- or two-line summary |

Unanticipated markup still lands sensibly: tables, lists, headings, paragraphs and images inside `.hud` are given baseline styling rather than inheriting browser defaults.

## 🛠️ Tool Registry

**38 native tool functions** exposed to the model:

| Category | Tools |
|---|---|
| **OS Automation** | `computer_click`, `computer_type`, `computer_press_keys`, `computer_scroll`, `computer_drag`, `read_ui_elements`, `list_open_windows`, `execute_shell_command`, `execute_applescript_task` |
| **Vision & Sensors** | `look_at_screen`, `look_at_webcam`, `start_camera_stream`, `stop_camera_stream`, `start_screen_stream`, `stop_screen_stream` |
| **Biometrics & Memory** | `register_person`, `identify_current_user`, `save_memory_fact`, `retrieve_memory_facts` |
| **Spatial 3D Engine** | `create_3d_scene`, `update_3d_scene`, `inspect_3d_scene`, `list_3d_scenes`, `delete_3d_scene` |
| **Generated 3D Assets** | `generate_spatial_3d_asset`, `list_3d_assets`, `show_3d_asset`, `show_3d_view` |
| **Widget Deck** | `create_skeleton_widget`, `dismiss_widget`, `clear_all_widgets` |
| **Delegation** | `dispatch_agent` |
| **Productivity & Web** | `get_calendar_events`, `create_calendar_event`, `get_recent_emails`, `search_emails`, `fetch_webpage` |
| **System Lifecycle** | `shutdown_friday` |

---

## 💻 Tech Stack

- **Core Runtime** — Python 3.10+ (asyncio, websockets, PyAudio, aiohttp, psutil)
- **AI Models** — Google Gemini via `google-genai`: Live `3.1-flash-live-preview`, agents and widget generator `3.1-pro-preview` / `3.7-flash`
- **macOS Native APIs** — PyObjC (Quartz CoreGraphics, EventKit, Foundation, WebKit), AppleScript
- **Computer Vision & Biometrics** — OpenCV, ONNX (YuNet, SFace), NumPy, Pillow
- **Frontend** — Vanilla JS + CSS, custom GLSL shaders, Three.js, MediaPipe HandLandmarker (WASM)
- **Desktop Shell** — PyWebView (WKWebView) with a persistent data store
- **Networking & Security** — Tailscale Serve HTTPS reverse proxy, zero-trust execution gateway

---

## 🚀 Getting Started

### 1. Prerequisites
- macOS 12 Monterey or higher (tested on Sonoma / Sequoia)
- Python 3.10+
- A Google Gemini API key
- *(Optional)* [Tailscale](https://tailscale.com/) for secure remote access

### 2. Permissions
Grant these in **System Settings → Privacy & Security**:
- **Accessibility** — `CGEvent` mouse/keyboard automation and UI tree reads
- **Screen Recording** — Quartz display capture
- **Microphone & Camera** — voice streaming and visual perception
- **Calendars & Automation** — EventKit and AppleScript/Mail control

### 3. Installation

```bash
git clone https://github.com/vincecyriac/project_friday.git
cd project_friday

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Configuration

```bash
cp .env.template .env
```

```ini
GEMINI_API_KEY=your_gemini_api_key_here

# Gemini Live model (voice + realtime)
GEMINI_MODEL=gemini-3.1-flash-live-preview

# FRIDAY's voice: Aoede (default) or Kore — both feminine.
# Charon and Puck are masculine and will not match her persona.
FRIDAY_VOICE=Aoede

# Optional — Tripo3D text-to-3D. Without it only generate_spatial_3d_asset
# is unavailable; SVE scenes are unaffected. Key: https://platform.tripo3d.ai
TRIPO_API_KEY=your_tripo_api_key_here
```

### 5. Running FRIDAY

**Desktop app** (PyWebView window, persistent camera/mic permissions):

```bash
.venv/bin/python app_desktop.py
```

**Headless engine** (serve the GUI to a browser instead):

```bash
.venv/bin/python friday_hub.py
```

Then open 👉 **`http://127.0.0.1:8766`**
The WebSocket gateway runs alongside it on `ws://127.0.0.1:8765`.

Ctrl+C, closing the window, or saying "goodbye" all shut down gracefully — audio devices, sockets and servers are released in order.

---

## 📱 Remote Access (Phone / Tablet)

Reachable from iOS or Android over your private Tailscale network, with no public ports.

1. Install Tailscale on the Mac and the phone, signed into the same account.
2. Run the gateway setup:
   ```bash
   ./setup_remote.sh
   ```
3. Open the printed HTTPS URL on your phone and add it to the Home Screen.
4. While connected remotely:
   - Microphone and voice playback route to the phone.
   - The phone camera becomes the primary vision sensor.
   - Shell and AppleScript calls require one-tap approval on the device.

Reset sharing with `tailscale serve reset`.

---

## 📁 Repository Structure

```
project_friday/
├── friday_hub.py              # Async hub: audio, WebSocket, Live session, widget deck
├── friday_agents.py           # Background agent tiers (os / spatial) and their tool loop
├── widget_generator_agent.py  # Card HTML synthesis + output sanitiser
├── app_desktop.py             # PyWebView desktop shell + process lifecycle
├── sentry_vision.py           # Quartz multi-monitor capture & coordinate tracking
├── sentry_action.py           # CGEvent mouse/keyboard automation & click mapping
├── sentry_exec.py             # Shell & osascript execution
├── sentry_recognition.py      # Face (YuNet+SFace) & voice (MFCC) biometrics
├── sentry_scene.py            # SVE scene graph manager, validation, persistence
├── sentry_personal.py         # EventKit calendars & Apple Mail
├── sentry_web.py              # Async webpage reader
├── setup_remote.sh            # Tailscale Serve HTTPS configurator
├── requirements.txt           # Python dependencies
├── SVE.md                     # Spatial Visualization Engine specification
├── web_gui/                   # Web interface
│   ├── index.html             # Orb stage, widget deck, telemetry HUD, sensor dock
│   ├── style.css              # Glassmorphic spatial layout, widget renderers
│   ├── app.js                 # WS client, audio pipeline, widget engine, orb state
│   ├── orb.js                 # Three.js holographic orb (GLSL plasma core + glow)
│   ├── sve.js                 # Three.js 3D scene graph renderer
│   ├── gestures.js            # MediaPipe HandLandmarker gesture input
│   └── vendor/                # Vendored Three.js & MediaPipe WASM models
```

**Generated at runtime, not in the repo.** These hold personal state and are git-ignored — the app creates them on first run:

| File | Holds |
|---|---|
| `friday_memory.json` | Persistent facts FRIDAY has been asked to remember |
| `friday_profiles.json` | Face and voice embeddings for identity recognition |
| `friday_scenes.json` | Saved 3D scene graphs |
| `.session_handle.json` | Live session resumption handle |
| `friday_history.jsonl` | Local interaction log |

---

## 📜 License & Acknowledgments

Built by [Vince Cyriac](https://vincecyriac.dev).
Powered by the Google Gemini GenAI SDK, OpenCV Zoo models, Three.js, MediaPipe, and Tailscale.
