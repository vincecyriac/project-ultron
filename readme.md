# Project Ultron

> **An Autonomous, Multimodal AI Desktop Assistant & Spatial Operating System for macOS**  
> Powered by Google Gemini Live bidirectional voice streaming, local LLM orchestration (LM Studio), multi-monitor Quartz vision, biometric identity recognition, native OS automation, and an interactive 3D Spatial Visualization Engine (SVE).

---

## 🌟 Overview

**Project Ultron** is a personal, multimodal desktop intelligence system designed specifically for macOS. Acting as a proactive pair-programmer, digital executive assistant, and spatial visualizer, Ultron bridges the gap between state-of-the-art multimodal foundation models and deep operating system integration.

### Core Highlights

- 🎙️ **Real-Time Voice Streaming**: Sub-second full-duplex conversational audio powered by Google Gemini Live WebSocket API (`gemini-3.1-flash-live-preview`), featuring natural barge-in / interruption handling.
- 🦙 **Dual-Engine Flexibility**: Seamless dynamic switching between cloud-based Gemini Live (real-time speech + vision) and fully local LLMs running via LM Studio (OpenAI-compatible REST API).
- 🖥️ **Multi-Monitor Spatial Vision**: Multi-display awareness using macOS Quartz / CoreGraphics. Automatically detects active cursor displays, captures multi-monitor composites, and tracks global desktop coordinate spaces.
- 🖱️ **Hardware-Level OS Automation**: Quartz `CGEvent` mouse positioning, multi-display click mapping (normalized 0–1000 coordinate space), keyboard automation, drag-and-drop, and accessibility UI element tree inspection.
- 👤 **Biometric Identity Recognition**: Local ONNX-based deep face recognition (YuNet detector + SFace 128-d cosine embeddings) and Mel-filterbank MFCC voice profiling.
- 🌐 **Spatial Visualization Engine (SVE)**: A live, persistent, interactive 3D scene graph rendered with Three.js in the web GUI, with incremental object-level delta operations and local MediaPipe hand-gesture tracking.
- 📅 **Native Workspace Integrations**: Native macOS EventKit Calendar management and Apple Mail integration.
- 🔒 **Zero-Trust Remote Access & Security**: Secure remote access from smartphones via private Tailscale mesh networks with interactive human-in-the-loop remote command execution approval cards.

---

## 🏗️ System Architecture

```
                                  ┌─────────────────────────────────────────────────────────┐
                                  │                  USER INTERFACES                        │
                                  │  • Web GUI (Audio / Chat / Spatial 3D / Gestures)       │
                                  │  • Native Mobile Browser via Tailscale HTTPS Gateway    │
                                  │  • Voice In / Out (16 kHz PCM Mic / 24 kHz Speaker)     │
                                  └───────────────▲─────────────────────────▲───────────────┘
                                                  │ (WebSocket / Audio)     │ (Touch / Video)
                                                  ▼                         ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│                                ULTRON ENGINE HUB (ultron_hub.py)                          │
│                                                                                           │
│  ┌───────────────────────────┐  ┌───────────────────────────┐  ┌────────────────────────┐ │
│  │    Audio Pipeline         │  │   State & Event Hub       │  │  Model Orchestrator    │ │
│  │ • PyAudio 16kHz/24kHz     │  │ • Play Queue / Interrupts │  │ • Gemini Live (WS)     │ │
│  │ • Voice Enrollment Buffer │  │ • Client Registry / Sync  │  │ • Local LLMs (LM Studio│ │
│  │ • Remote Audio Routing    │  │ • Approval State Machine  │  │ • Dynamic Dispatcher   │ │
│  └───────────────────────────┘  └───────────────────────────┘  └────────────────────────┘ │
└───────────────┬───────────────────────────────┬─────────────────────────────┬─────────────┘
                │ Tool Calls & Perception       │ Scene Operations            │ Biometrics
                ▼                               ▼                             ▼
┌───────────────────────────────┐ ┌───────────────────────────┐ ┌───────────────────────────┐
│   CAPABILITY SUBSYSTEMS       │ │ SPATIAL VISUALIZATION (SVE│ │ RECOGNITION & MEMORY      │
│                               │ │                           │ │                           │
│ • sentry_vision.py            │ │ • sentry_scene.py         │ │ • sentry_recognition.py   │
│   Multi-display Quartz capture│ │   SceneGraph JSON ops     │ │   YuNet + SFace ONNX (128D│
│ • sentry_action.py            │ │ • web_gui/sve.js          │ │   Mel MFCC Voice Vectors  │
│   CGEvent mouse & keyboard    │ │   Three.js Render Engine  │ │ • ultron_memory.json      │
│ • sentry_exec.py              │ │ • web_gui/gestures.js     │ │   Persistent Semantic     │
│   Shell & AppleScript tasks   │ │   MediaPipe HandLandmarker│ │   Knowledge Store         │
│ • sentry_personal.py          │ └───────────────────────────┘ └───────────────────────────┘
│   EventKit & Apple Mail       │
│ • sentry_web.py               │
│   Async HTML/Text Scraper     │
└───────────────────────────────┘
```

---

## 🧩 Core Subsystems & Components

### 1. Dual-Engine Intelligence Hub (`ultron_hub.py`, `sentry_local.py`)
- **Gemini Live Stream**: Connects over bidirectional WebSockets to `gemini-3.1-flash-live-preview` using Google GenAI SDK. Supports real-time text-to-speech audio streaming and interruption detection ("barge-in").
- **Local Model Gateway**: Interfaces with LM Studio via OpenAI-compatible endpoints (`/v1/chat/completions`). Tool schemas are converted dynamically on the fly (`gemini_decls_to_openai_tools`), and screenshots/webcam frames are encoded as inline vision payloads.
- **Model Registry**: Enables zero-restart hot-swapping between cloud Gemini and local models directly from the GUI.

### 2. Multi-Monitor Vision & Spatial Targeting (`sentry_vision.py`)
- **Quartz Display Enumeration**: Detects all active monitors, scaling factors, and multi-display desktop arrangements via CoreGraphics.
- **Context-Aware Screen Capture**: Auto-detects the display where the user's cursor is currently located, or captures composite multi-display panoramas.
- **Global Coordinate Tracking**: Stores `LAST_CAPTURE_BOUNDS` for every screenshot, enabling accurate translation of normalized (0–1000) model coordinates back to physical screen pixels.

### 3. Native macOS Desktop Automation (`sentry_action.py`, `sentry_exec.py`)
- **Hardware-Level Input Injection**: Uses Quartz `CGEventPost` (`kCGHIDEventTap`) to bypass single-monitor clamping limitations found in PyAutoGUI, providing multi-monitor mouse movement, clicks, drags, and scrolling.
- **Accessibility UI Inspector (`read_ui_elements`)**: Directly queries the frontmost application's AX accessibility tree to extract UI hierarchy, control labels, and exact coordinates.
- **Window & Space Enumeration (`list_open_windows`)**: Scans macOS Quartz Window Server to identify open windows, positions, and virtual desktop (Spaces) locations.
- **System Command Execution**: Executes shell commands and AppleScript snippets with built-in timeouts and security checks.

### 4. Biometric Face & Voice Recognition (`sentry_recognition.py`)
- **Deep Face Recognition**:
  - Detection: OpenCV YuNet ONNX model for high-speed face detection.
  - Identification: OpenCV SFace ONNX deep feature extractor generating 128-dimensional embeddings, matched via cosine similarity against enrolled user profiles (`ultron_profiles.json`).
- **Voice Fingerprinting**: Extracts Mel-frequency cepstral coefficients (MFCCs) using pure NumPy from audio buffers, pooled with mean and variance statistics for speaker identification.

### 5. Spatial Visualization Engine (SVE) (`sentry_scene.py`, `web_gui/sve.js`, `web_gui/gestures.js`)
- **Persistent 3D Workspace**: Replaces flat HTML generations with live Three.js 3D scenes (astronomy, anatomy, architecture, flowcharts, data graphs) that persist in `ultron_scenes.json`.
- **Incremental Object Delta Protocol**: Models can update, rotate, recolor, highlight, hide, or explode specific objects without re-rendering the entire scene.
- **Markerless Hand Tracking**: Uses vendored local MediaPipe HandLandmarker WASM to enable point-to-hover, pinch-to-grab, pinch-to-orbit, and two-hand pinch-to-zoom spatial interaction.

### 6. Personal Productivity Suite (`sentry_personal.py`)
- **Calendar Management**: Directly talks to macOS **EventKit** via PyObjC to fetch and create events across iCloud, Google, and Microsoft Exchange calendars.
- **Apple Mail Integration**: Accesses local Mail.app accounts via AppleScript to read recent emails, search sender/subject threads, and draft messages.

### 7. Zero-Trust Remote Access & Security (`setup_remote.sh`, Tailscale)
- **Tailscale Mesh Encryption**: The hub only binds locally to `127.0.0.1:8766`. Remote mobile connections are tunneled securely via Tailscale Serve HTTPS proxy.
- **Human-in-the-Loop Command Approvals**: Whenever a remote client is active, all shell and AppleScript executions are suspended and require one-tap approval on the connected device with a 45-second auto-deny timeout.
- **Smart Audio & Camera Routing**: Automatically switches the default microphone and camera feed to the connected phone while muting laptop audio.

---

## 🛠️ Tool Registry

Ultron exposes 21+ granular native tool functions to the AI model:

| Category | Tool Name | Description |
|---|---|---|
| **OS Automation** | `computer_click` | Clicks normalized (0–1000) coordinates across any display. |
| | `computer_type` | Types text into currently focused fields with optional Enter. |
| | `computer_press_keys` | Sends key combos (`command+c`, `command+space`, etc.). |
| | `computer_scroll` | Scrolls mouse wheel up/down over targeted coordinates. |
| | `computer_drag` | Drags from coordinate A to coordinate B (sliders, windows). |
| | `read_ui_elements` | Reads the accessibility UI element tree of frontmost app. |
| | `list_open_windows` | Lists open windows and virtual Spaces across all displays. |
| | `execute_shell_command` | Executes shell commands on macOS (subject to approval). |
| | `execute_applescript_task`| Runs AppleScript code for native apps & system settings. |
| **Vision & Sensors** | `look_at_screen` | Captures active display, specific display, or composite all. |
| | `look_at_webcam` | Captures single camera frame (Mac webcam or Phone). |
| | `start_camera_stream` | Starts continuous real-time camera video stream. |
| | `stop_camera_stream` | Stops continuous camera stream. |
| | `start_screen_stream` | Starts continuous real-time screen capture stream. |
| | `stop_screen_stream` | Stops continuous screen capture stream. |
| **Biometrics & Memory**| `register_person` | Enrolls new face and voice signature profile. |
| | `identify_current_user` | Identifies active user from camera and audio buffers. |
| | `save_memory_fact` | Saves persistent key-value facts and user preferences. |
| | `retrieve_memory_facts` | Retrieves stored long-term memory facts. |
| **Spatial 3D Engine** | `create_3d_scene` | Generates a persistent 3D scene from JSON object specs. |
| | `update_3d_scene` | Executes incremental object-level modifications & animations. |
| | `inspect_3d_scene` | Reads full JSON state and hierarchy of an active scene. |
| | `list_3d_scenes` | Lists active 3D scenes and user object selections. |
| | `delete_3d_scene` | Closes and removes a scene from the workspace. |
| **Productivity & Web** | `get_calendar_events` | Reads calendar events via macOS EventKit. |
| | `create_calendar_event` | Creates new calendar events with title, time, notes. |
| | `get_recent_emails` | Reads recent emails from Apple Mail inbox. |
| | `search_emails` | Searches emails by query/sender. |
| | `fetch_webpage` | Asynchronously scrapes URL content to clean text. |
| **System Lifecycle** | `shutdown_ultron` | Gracefully closes all services and terminates the hub. |

---

## 💻 Tech Stack

- **Core Runtime**: Python 3.10+ (Asyncio, WebSockets, PyAudio, aiohttp)
- **AI Models**: Google Gemini Live API (`google-genai` SDK), LM Studio (Local LLMs)
- **macOS Native APIs**: PyObjC (Quartz CoreGraphics, EventKit, Foundation), AppleScript (`osascript`)
- **Computer Vision & Biometrics**: OpenCV (`cv2`), ONNX Runtime (YuNet, SFace), NumPy, Pillow
- **Frontend & Visualization**: Modern Vanilla JavaScript / CSS, Three.js, MediaPipe HandLandmarker (WASM)
- **Networking & Security**: Tailscale Serve (HTTPS Reverse Proxy), SSL/TLS, Zero-Trust Execution Gateway

---

## 🚀 Getting Started

### 1. Prerequisites
- macOS 12 Monterey or higher (tested on macOS 14 Sonoma / macOS 15 Sequoia)
- Python 3.10+
- Google Gemini API Key
- *(Optional)* [LM Studio](https://lmstudio.ai/) for offline local models
- *(Optional)* [Tailscale](https://tailscale.com/) for secure remote access

### 2. Permissions Required
Grant permissions in **macOS System Settings -> Privacy & Security**:
- **Accessibility**: Required for `CGEvent` mouse/keyboard automation.
- **Screen Recording**: Required for `Quartz` display capture.
- **Microphone & Camera**: Required for voice streaming and visual perception.
- **Calendars & Automation**: Required for EventKit and AppleScript / Mail control.

### 3. Installation

```bash
# Clone the repository
git clone https://github.com/vincecyriac/project_ultron.git
cd project_ultron

# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 4. Configuration

Copy `.env.template` to `.env` and fill in your API credentials:

```bash
cp .env.template .env
```

```ini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.1-flash-live-preview

# Optional: Local LLMs via LM Studio
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
LOCAL_MODELS=qwen/qwen3-8b,google/gemma-3-4b
```

### 5. Running Ultron

```bash
.venv/bin/python ultron_hub.py
```

Open the web interface in your browser:
👉 **`http://127.0.0.1:8766`**

---

## 📱 Remote Access Setup (Phone / Tablet)

Ultron can be securely accessed from iOS or Android over your private Tailscale network without exposing any public ports.

1. Install Tailscale on your Mac and mobile device, signed in with the same account.
2. Run the remote gateway setup script:
   ```bash
   ./setup_remote.sh
   ```
3. Open the printed HTTPS URL on your phone (e.g. `https://vinces-macbook-pro.tailnet.ts.net/`) and add it to your Home Screen.
4. When accessing remotely:
   - Microphone and audio route directly to your phone.
   - The phone camera becomes the primary vision sensor.
   - Shell commands prompt for interactive one-tap approval.

To reset remote sharing:
```bash
tailscale serve reset
```

---

## 📁 Repository Structure

```
project_ultron/
├── ultron_hub.py              # Main async hub: audio, WebSocket, model orchestration
├── sentry_vision.py           # Quartz multi-monitor capture & coordinate tracking
├── sentry_action.py           # CGEvent mouse/keyboard automation & click mapping
├── sentry_exec.py             # Subprocess shell & osascript AppleScript execution
├── sentry_recognition.py      # Face (YuNet+SFace) & voice (MFCC) biometric matching
├── sentry_scene.py            # SVE Scene Graph manager, validation, persistence
├── sentry_personal.py         # macOS EventKit Calendar & Apple Mail integration
├── sentry_web.py              # Asynchronous webpage reader & scraper
├── sentry_local.py            # LM Studio OpenAI-compatible adapter & schema converter
├── setup_remote.sh            # Tailscale Serve HTTPS reverse proxy configurator
├── requirements.txt           # Python dependencies
├── SVE.md                     # Spatial Visualization Engine technical specification
├── web_gui/                   # Web interface
│   ├── index.html             # GUI markup (Audio, Chat, Spatial 3D, Settings)
│   ├── style.css              # Dark-mode styling, glassmorphism design
│   ├── app.js                 # WebSocket client, audio streaming, approvals
│   ├── sve.js                 # Three.js 3D scene graph rendering engine
│   ├── gestures.js            # MediaPipe HandLandmarker gesture input manager
│   └── vendor/                # Vendored Three.js & MediaPipe WASM models
├── ultron_memory.json         # Persistent key-value user memory facts
├── ultron_profiles.json       # Biometric face/voice embedding profiles
└── ultron_scenes.json         # Persistent 3D SVE scene graph states
```

---

## 📜 License & Acknowledgments

Built by [Vince Cyriac](https://github.com/vincecyriac).  
Powered by Google Gemini GenAI SDK, OpenCV Zoo models, Three.js, and Tailscale.
