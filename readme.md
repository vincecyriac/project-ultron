# Project Ultron

> **An Autonomous, Multimodal AI Desktop Assistant & Spatial Operating System for macOS**
> Voice-first, Gemini-only. A holographic orb you talk to, a deck of live data widgets it composes for you, an interactive 3D spatial engine, and deep native OS control — with background agents doing the heavy lifting off the audio path.

---

## 🌟 Overview

**Project Ultron** is a personal, multimodal desktop intelligence system for macOS. You speak; it answers in under ten words and puts the substance on screen. Detail lives in widgets — charts, metric matrices, intelligence feeds, embedded web pages, live 3D scenes — never in a wall of narration.

### Core Highlights

- 🎙️ **Real-Time Voice Streaming** — Full-duplex conversational audio over the Gemini Live WebSocket API (`gemini-3.1-flash-live-preview`), 16 kHz PCM in / 24 kHz out, with natural barge-in. Deep, grounded **Charon** voice by default.
- 🔮 **Ambient Holographic Orb** — A Three.js plasma core that *is* the status display. Colour is bound to state and holds steady until the state changes; motion reacts to what you actually hear.
- 🧩 **Composable Widget Deck** — The assistant builds cards out of declarative UI primitives (`hero_stat`, `chart_svg`, `metric_grid`, `feed_list`, `media_view`, `progress_gauge`, `web_frame`) and stacks them newest-first beside the orb.
- 🤖 **Tiered Background Agents** — Live stays responsive for barge-in and dispatches multi-step work to specialised models: **Gemini 3.1 Pro** for macOS automation, **Gemini 3.7 Flash** for spatial scene generation.
- 🌐 **Spatial Visualization Engine (SVE)** — Live, persistent, interactive 3D scene graphs in Three.js with object-level delta updates and local MediaPipe hand-gesture control.
- 🖥️ **Multi-Monitor Vision & OS Automation** — Quartz display enumeration, context-aware capture, and hardware-level `CGEvent` mouse/keyboard injection across every display.
- 👤 **Biometric Identity** — Local ONNX face recognition (YuNet + SFace) and MFCC voice profiling, entirely on-device.
- 📅 **Native Workspace Integration** — macOS EventKit calendars and Apple Mail via PyObjC and AppleScript.
- 🔒 **Zero-Trust Remote Access** — Reach it from your phone over a private Tailscale mesh, with one-tap approval for every shell command.

---

## 🖥️ The Interface

The GUI is a spatial workspace with no chrome — no title bar, no tabs, no status text. Every surface owns a reserved region, so nothing ever overlaps.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 21:04:18 │ CPU 38% │ RAM 12.8/16 GB          ← ambient telemetry HUD     │
│ ◐ OS agent · Open Safari and summarise…      ← background agent chips    │
│                                                                          │
│      ╭─────────╮        ┌──────────────────────────────────────────────┐ │
│      │  ORB    │        │  ALPHABET INC.                            ×  │ │
│      │ (state) │        │  207.42  USD              ▲ +3.81 +1.87%    │ │
│      ╰─────────╯        │  ╭────────────────────────────────────────╮  │ │
│                         │  │  gradient area chart, dashed baseline  │  │ │
│    ← 260px rail         │  ╰────────────────────────────────────────╯  │ │
│                         │  OPEN 204.10   HIGH 209.94   LOW 203.44     │ │
│                         └──────────────────────────────────────────────┘ │
│  ┌───────────┐          ┌──────────────────────────────────────────────┐ │
│  │ camera PIP│          │  MARKET INTELLIGENCE                      ×  │ │
│  │ (mirrored)│          │  01 [ALERT] Antitrust ruling lands Tuesday   │ │
│  └───────────┘          └──────────────────────────────────────────────┘ │
│  ╭ Speak, or type… ╮                                   ╭─ 🎙 📷 🖥 ─╮   │
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
| Speaking | `#00FF88` emerald | Ultron is talking |
| Offline | `#E5726F` ember | Hub unreachable |

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
│                                ULTRON ENGINE HUB (ultron_hub.py)                          │
│                                                                                           │
│  ┌───────────────────────────┐  ┌───────────────────────────┐  ┌────────────────────────┐ │
│  │    Audio Pipeline         │  │   State & Event Hub       │  │  Gemini Live Session   │ │
│  │ • PyAudio 16kHz/24kHz     │  │ • Play Queue / Interrupts │  │ • Charon voice, tools  │ │
│  │ • Voice Enrollment Buffer │  │ • Widget Deck Registry    │  │ • Per-turn receive loop│ │
│  │ • Remote Audio Routing    │  │ • Approval State Machine  │  │ • In-run resumption    │ │
│  └───────────────────────────┘  └───────────────────────────┘  └────────────────────────┘ │
└──────┬────────────────────┬───────────────────┬────────────────────────┬──────────────────┘
       │ Tool Calls         │ Dispatch          │ Scene Ops              │ Biometrics
       ▼                    ▼                   ▼                        ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
│ CAPABILITY       │ │ BACKGROUND AGENTS│ │ SPATIAL ENGINE (SVE) │ │ RECOGNITION & MEMORY │
│ SUBSYSTEMS       │ │ ultron_agents.py │ │                      │ │                      │
│                  │ │                  │ │ • sentry_scene.py    │ │ • sentry_recognition │
│ • sentry_vision  │ │ • os tier        │ │   SceneGraph deltas  │ │   YuNet + SFace 128-d│
│ • sentry_action  │ │   Gemini 3.1 Pro │ │ • web_gui/sve.js     │ │   Mel MFCC voice     │
│ • sentry_exec    │ │ • spatial tier   │ │   Three.js renderer  │ │ • ultron_memory.json │
│ • sentry_personal│ │   Gemini 3.7 Fl. │ │ • web_gui/gestures.js│ │   Persistent facts   │
│ • sentry_web     │ │                  │ │   MediaPipe hands    │ │                      │
└──────────────────┘ └──────────────────┘ └──────────────────────┘ └──────────────────────┘
```

---

## 🧩 Core Subsystems

### 1. Gemini Live Hub (`ultron_hub.py`)
- **Bidirectional voice** over WebSockets to `gemini-3.1-flash-live-preview`, typed `LiveConnectConfig` with the **Charon** prebuilt voice (override with `ULTRON_VOICE`).
- **Per-turn receive loop** — `session.receive()` yields one conversational turn and ends, so the loop re-enters it. Without that the session tears down after every reply and the reconnect replays the last turn.
- **Session lifecycle** — the resumption handle is held **in memory only**, bridging GoAway rotations and drops *within* a run. A new process is always a new conversation.
- **Concise persona** — spoken replies stay under ten words; when a widget is mounted, one or two sentences carrying the key takeaway. Detail belongs on screen.

### 2. Background Agent Tiers (`ultron_agents.py`)
Live must stay free for barge-in, so anything multi-step is dispatched off the audio path via `dispatch_agent`. Ultron acknowledges in the same turn ("Working on that now.") and announces the result when it lands.

| Tier | Model | Handles |
|---|---|---|
| `os` | `gemini-3.1-pro-preview` | macOS automation, AppleScript/shell chains, GUI operation |
| `spatial` | `gemini-3.7-flash` | Building and editing 3D SVE scenes |

Agents reuse the hub's own tool implementations, run up to 12 tool round-trips, and report back a single spoken sentence. Results are **queued for a gap in the conversation** — a finished agent can never cut Ultron off mid-sentence.

### 3. Widget Deck (`ultron_hub.py`, `web_gui/app.js`)
The hub owns the authoritative deck (max 8 cards, 12 components each) and broadcasts `widget_action` events; a client connecting late receives a full `sync` snapshot. Cards are keyed by a stable `widget_id`, so repeat calls patch in place rather than piling up.

Clicks flow back: dismissing a card tells the hub, so the model's view never drifts from yours.

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
- OpenCV **YuNet** ONNX detection + **SFace** 128-d embeddings, cosine-matched against `ultron_profiles.json`.
- **Voice fingerprinting** — pure-NumPy MFCCs pooled by mean and variance from the rolling mic buffer.

### 7. Spatial Visualization Engine (`sentry_scene.py`, `web_gui/sve.js`, `web_gui/gestures.js`)
- **Persistent 3D workspace** — scenes live in `ultron_scenes.json` and stay on stage until dismissed.
- **Incremental delta protocol** — update, rotate, recolour, highlight, hide, or explode individual objects; never a full rebuild.
- **Screen-sized labels** — annotations are sized to a constant on-screen height, depth-tested so geometry occludes them, and decluttered by screen-space overlap (12 visible at once, nearest first).
- **Markerless hand tracking** — vendored MediaPipe HandLandmarker WASM: point to hover, pinch to grab, pinch empty space to orbit, two-hand pinch to zoom.

### 8. Personal Productivity Suite (`sentry_personal.py`)
- **EventKit calendars** via PyObjC across iCloud, Google, and Exchange.
- **Apple Mail** via AppleScript — read recent mail, search sender/subject.

### 9. Zero-Trust Remote Access (`setup_remote.sh`, Tailscale)
- The hub binds only to `127.0.0.1`; remote access is tunnelled through Tailscale Serve HTTPS.
- **Human-in-the-loop approvals** — while a remote client is connected, every shell and AppleScript call suspends for one-tap approval with a 45-second auto-deny.
- **Smart sensor routing** — the phone's mic and camera become the primary senses; the unattended Mac's webcam and screen are left alone unless asked for explicitly.

---

## 🧱 Widget Component Reference

A card is a title plus an ordered array of primitives, rendered top to bottom.

| Component | Renders |
|---|---|
| `hero_stat` | Headline figure, subtitle, market tag, colour-coded delta badge, timestamp |
| `chart_svg` | Auto-scaling gradient area chart, glowing stroke, dashed baseline reference, axis bounds and time markers |
| `metric_grid` | Dense 2–4 column label/value matrix (Open, High, Low, Mkt Cap, P/E, 52W range, Div Yield…) |
| `feed_list` | Numbered intelligence items with category badges, bold headlines, briefs, timestamps |
| `media_view` | Image URL or inline SVG inside corner-HUD framing, with a graceful failure state |
| `progress_gauge` | Linear meters or radial dials with warm/hot thresholds |
| `web_frame` | Live embedded page with a browser HUD (back / forward / reload / URL pill / open externally) |

`web_frame` is header-aware: the hub probes `X-Frame-Options` and `Content-Security-Policy: frame-ancestors` server-side — the browser cannot see a framing refusal cross-origin — and a site that refuses embedding shows a launch button instead of a blank frame.

---

## 🛠️ Tool Registry

**35 native tool functions** exposed to the model:

| Category | Tools |
|---|---|
| **OS Automation** | `computer_click`, `computer_type`, `computer_press_keys`, `computer_scroll`, `computer_drag`, `read_ui_elements`, `list_open_windows`, `execute_shell_command`, `execute_applescript_task` |
| **Vision & Sensors** | `look_at_screen`, `look_at_webcam`, `start_camera_stream`, `stop_camera_stream`, `start_screen_stream`, `stop_screen_stream` |
| **Biometrics & Memory** | `register_person`, `identify_current_user`, `save_memory_fact`, `retrieve_memory_facts` |
| **Spatial 3D Engine** | `create_3d_scene`, `update_3d_scene`, `inspect_3d_scene`, `list_3d_scenes`, `delete_3d_scene` |
| **Widget Deck** | `create_widget`, `update_widget`, `dismiss_widget`, `clear_all_widgets` |
| **Delegation** | `dispatch_agent` |
| **Productivity & Web** | `get_calendar_events`, `create_calendar_event`, `get_recent_emails`, `search_emails`, `fetch_webpage` |
| **System Lifecycle** | `shutdown_ultron` |

---

## 💻 Tech Stack

- **Core Runtime** — Python 3.10+ (asyncio, websockets, PyAudio, aiohttp, psutil)
- **AI Models** — Google Gemini via `google-genai`: Live `3.1-flash-live-preview`, agents `3.1-pro-preview` and `3.7-flash`
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
git clone https://github.com/vincecyriac/project_ultron.git
cd project_ultron

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

# Assistant voice: Charon (default), Kore, Puck
ULTRON_VOICE=Charon
```

### 5. Running Ultron

**Desktop app** (PyWebView window, persistent camera/mic permissions):

```bash
.venv/bin/python app_desktop.py
```

**Headless engine** (serve the GUI to a browser instead):

```bash
.venv/bin/python ultron_hub.py
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
project_ultron/
├── ultron_hub.py              # Async hub: audio, WebSocket, Live session, widget deck
├── ultron_agents.py           # Background agent tiers (os / spatial) and their tool loop
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
| `ultron_memory.json` | Persistent facts Ultron has been asked to remember |
| `ultron_profiles.json` | Face and voice embeddings for identity recognition |
| `ultron_scenes.json` | Saved 3D scene graphs |
| `.session_handle.json` | Live session resumption handle |
| `ultron_history.jsonl` | Local interaction log |

---

## 📜 License & Acknowledgments

Built by [Vince Cyriac](https://vincecyriac.dev).
Powered by the Google Gemini GenAI SDK, OpenCV Zoo models, Three.js, MediaPipe, and Tailscale.
