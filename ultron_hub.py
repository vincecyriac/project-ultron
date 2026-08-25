import os
os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
import sys
import re
import json
import time
import uuid
import asyncio
import pyaudio
import cv2
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except:
    pass
import numpy as np
import unicodedata
import tty
import termios
import signal
import ssl
import base64
import websockets
try:
    import psutil
except ImportError:          # telemetry degrades to clock-only in the GUI
    psutil = None
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Import capability submodules
import sentry_vision
import sentry_exec
import sentry_action
import sentry_recognition
import sentry_web
import sentry_personal
import sentry_scene
import sentinel_ax
import sentinel_vision
import ultron_agents

# Load environment variables
load_dotenv()

# Bypass SSL Verification issues for raw downloads on macOS
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_LOG_FILE = os.path.join(BASE_DIR, "ultron_history.jsonl")
MEMORY_FILE = os.path.join(BASE_DIR, "ultron_memory.json")

# Model selection: defaults to gemini-3.1-flash-live-preview, customizable via env
MODEL_ID = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")

# Deep, grounded assistant persona. Charon / Kore both suit it; Puck is lighter.
LIVE_VOICE = os.getenv("ULTRON_VOICE", "Charon")

# Local models served by LM Studio (OpenAI-compatible API)
# Audio configuration
FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK_SIZE = 1024

def log_info(msg: str):
    print(f"[Ultron Engine] {msg}")
    broadcast_event({"type": "chat_log", "sender": "System", "text": msg, "style": "system"})

# Global states
play_queue = asyncio.Queue()
interrupted_event = asyncio.Event()
shutdown_event = asyncio.Event()
camera_stream_active = False
screen_stream_active = False
active_webcam = None
latest_webcam_frame_bytes = None
input_buffer = ""
mic_audio_buffer = bytearray()
MAX_BUFFER_SIZE = 320000  # 10 seconds of 16kHz 16-bit PCM (voice enrollment window)
model_is_speaking = False
model_turn_active = False      # Live is mid-response (audio, text or tool call)
connected_ws_clients = set()
remote_ws_clients = set()  # clients reached via Tailscale Serve (phone, other devices)
pending_exec_approvals = {}  # approval_id -> asyncio.Future[bool]
latest_remote_frame_bytes = None  # last camera frame pushed by a remote client
latest_remote_frame_ts = 0.0
camera_source = {"mode": "auto"}  # auto = phone camera when remote session live; mac = force Mac webcam
global_live_session = None
system_prompt_text = ""
last_focus_note = ""

# Live session resumption.
#
# A resumption handle restores the ENTIRE prior conversation, so it must never
# outlive the process: reopening the app would silently continue whatever was
# discussed before (and re-fire that turn's tool calls). It is therefore kept in
# memory only, purely to bridge GoAway rotations and transient drops *within one
# run*. A new process is always a new conversation.
#
# Persisting it to disk was tried and cannot be made safe here: run_ultron()
# executes in a daemon thread (app_desktop.py), so its finally: block never runs
# on quit — nothing can reliably delete the file — and any age-based "crash
# recovery" window is exactly the window in which a person quits and reopens.
current_session_handle = None

# ---------- Lifecycle control ----------
# run_ultron() runs on its own loop, usually inside a thread owned by a GUI
# shell, so shutdown can be requested from the window, a signal handler, or the
# assistant itself. Everything funnels through request_shutdown().

main_loop = None                 # the loop run_ultron() is running on
genai_client = None              # kept alive until interpreter exit (see run_ultron)
shutdown_callbacks = []          # notified once, when shutdown begins
_shutdown_reason = ""


class StartupError(RuntimeError):
    """Fatal boot failure. The shell reports it and exits — never sys.exit()
    from here, which would only kill the engine thread and leave a dead app."""


def on_shutdown(callback):
    """Register a callable(reason) fired when the engine starts shutting down."""
    shutdown_callbacks.append(callback)


def request_shutdown(reason: str = ""):
    """Begin a graceful shutdown. Safe to call from any thread."""
    global _shutdown_reason
    if shutdown_event.is_set():
        return
    _shutdown_reason = reason or _shutdown_reason
    log_info(f"Shutdown requested ({_shutdown_reason or 'no reason given'}).")
    loop = main_loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(shutdown_event.set)
    else:
        shutdown_event.set()


async def sleep_unless_shutdown(seconds: float) -> bool:
    """Sleep, but wake immediately if shutdown is requested.
    Returns True if it slept the full duration."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return False
    except asyncio.TimeoutError:
        return True


async def shutdown_watcher():
    """Fires the registered callbacks so a GUI shell can close its window when
    the assistant decides to quit on its own (e.g. the shutdown_ultron tool)."""
    await shutdown_event.wait()
    for cb in list(shutdown_callbacks):
        try:
            cb(_shutdown_reason)
        except Exception as e:
            log_info(f"Shutdown callback failed: {e}")


def save_session_handle(handle: str):
    global current_session_handle
    current_session_handle = handle


def clear_session_handle():
    global current_session_handle
    current_session_handle = None


sentry_scene.set_broadcaster(lambda ev: broadcast_event(ev))

def broadcast_event(data: dict):
    if not connected_ws_clients:
        return
    msg = json.dumps(data)
    for ws in list(connected_ws_clients):
        try:
            asyncio.create_task(ws.send(msg))
        except Exception:
            pass

def set_system_status(status_str: str):
    print(f"[Ultron Status] {status_str}")
    broadcast_event({"type": "status", "status": status_str})


# ---------- Ambient system telemetry (drives the GUI's top-left HUD) ----------

TELEMETRY_INTERVAL = 2.0

_last_cpu = {"value": 0.0, "at": 0.0, "cores": []}
_last_net = {"io": None, "at": 0.0}

def sample_telemetry() -> dict | None:
    """CPU load + physical memory use. None when psutil is unavailable."""
    if psutil is None:
        return None
    try:
        # cpu_percent(interval=None) measures since its own previous call, so
        # two calls in quick succession make the second read ~0. Reuse the last
        # reading unless enough time has passed for a meaningful sample.
        now = time.time()
        if now - _last_cpu["at"] >= 0.5:
            _last_cpu["value"] = psutil.cpu_percent(interval=None)
            _last_cpu["cores"] = psutil.cpu_percent(interval=None, percpu=True)
            _last_cpu["at"] = now
        vm = psutil.virtual_memory()
        payload = {
            "type": "system_telemetry",
            "cpu": _last_cpu["value"],
            "mem_used_gb": (vm.total - vm.available) / (1024 ** 3),
            "mem_total_gb": vm.total / (1024 ** 3),
        }
        # Extras for the telemetry widget; the HUD ignores what it does not use.
        try:
            payload["cpu_cores"] = _last_cpu.get("cores") or []
            io = psutil.net_io_counters()
            prev, prev_t = _last_net["io"], _last_net["at"]
            if prev is not None and now > prev_t:
                span = now - prev_t
                payload["net_up_kbps"] = max(0.0, (io.bytes_sent - prev.bytes_sent) / span / 1024)
                payload["net_down_kbps"] = max(0.0, (io.bytes_recv - prev.bytes_recv) / span / 1024)
            _last_net["io"], _last_net["at"] = io, now
            payload["uptime_s"] = max(0.0, time.time() - psutil.boot_time())
        except Exception:
            pass
        return payload
    except Exception:
        return None


async def telemetry_task():
    """Push a light system snapshot to every connected GUI on an interval."""
    if psutil is None:
        log_info("psutil not installed — GUI telemetry limited to the clock.")
        return
    # cpu_percent's very first call always returns 0.0; prime it, take one real
    # reading into the cache, then settle into the broadcast interval.
    psutil.cpu_percent(interval=None)
    await asyncio.sleep(0.5)
    _last_cpu["value"] = psutil.cpu_percent(interval=None)
    _last_cpu["at"] = time.time()
    while not shutdown_event.is_set():
        payload = sample_telemetry()
        if payload:
            broadcast_event(payload)
        await asyncio.sleep(TELEMETRY_INTERVAL)


def remote_frame_fresh() -> bool:
    return latest_remote_frame_bytes is not None and (time.time() - latest_remote_frame_ts) < 5.0

def remote_session_active() -> bool:
    return bool(remote_ws_clients)

async def notify_session_remote_change(connected: bool):
    """Tells the live model which device Vince is on, so senses default correctly."""
    if not global_live_session:
        return
    if connected:
        note = ("[System note, do not respond: Vince just connected remotely from his phone. "
                "His phone microphone and phone camera are now the PRIMARY senses — camera tools "
                "default to the phone camera automatically. The Mac's webcam and screen belong to "
                "his unattended laptop: do NOT capture or stream them unless Vince explicitly asks "
                "for the laptop/Mac camera or screen.]")
    else:
        note = ("[System note, do not respond: the remote phone session ended. Vince is back at "
                "the Mac; its webcam and screen are the primary senses again.]")
    try:
        await global_live_session.send_client_content(
            turns=[{"role": "user", "parts": [{"text": note}]}],
            turn_complete=False
        )
    except Exception:
        pass


async def request_exec_approval(tool_name: str, preview: str) -> bool:
    """Asks connected GUI clients to approve a shell/AppleScript command while a
    remote (Tailscale) session is active. Deny on timeout or disconnect."""
    approval_id = uuid.uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    pending_exec_approvals[approval_id] = fut
    broadcast_event({
        "type": "exec_approval_request",
        "id": approval_id,
        "tool": tool_name,
        "preview": preview
    })
    set_system_status("Awaiting approval")
    try:
        return await asyncio.wait_for(fut, timeout=45)
    except asyncio.TimeoutError:
        return False
    finally:
        pending_exec_approvals.pop(approval_id, None)
        broadcast_event({"type": "exec_approval_closed", "id": approval_id})


async def ws_handler(websocket):
    global camera_stream_active, screen_stream_active, model_is_speaking, mic_audio_buffer
    global latest_remote_frame_bytes, latest_remote_frame_ts
    connected_ws_clients.add(websocket)
    # Tailscale Serve proxies from localhost but stamps X-Forwarded-For;
    # direct local browsers connect without it.
    try:
        if websocket.request and websocket.request.headers.get("X-Forwarded-For"):
            remote_ws_clients.add(websocket)
            log_info("Remote client connected (via Tailscale).")
            await notify_session_remote_change(connected=True)
    except Exception:
        pass
    await websocket.send(json.dumps({
        "type": "sense_update",
        "camera_active": camera_stream_active,
        "screen_active": screen_stream_active
    }))
    telemetry = sample_telemetry()
    if telemetry:
        await websocket.send(json.dumps(telemetry))
    await websocket.send(json.dumps(sentry_scene.manager.workspace_snapshot()))
    await websocket.send(json.dumps(widget_snapshot()))
    await websocket.send(json.dumps(sentinel_status()))
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_type = data.get("type")
                if msg_type == "client_hello":
                    # Fallback remote flag from the page itself (non-localhost origin)
                    if data.get("remote") and websocket not in remote_ws_clients:
                        remote_ws_clients.add(websocket)
                        log_info("Remote client connected (self-reported).")
                        await notify_session_remote_change(connected=True)
                elif msg_type == "remote_camera_frame":
                    # Phone camera frame: becomes the primary visual feed while
                    # the remote session is live (unless Mac cam was forced).
                    b64 = data.get("image_base64")
                    if b64 and websocket in remote_ws_clients:
                        latest_remote_frame_bytes = base64.b64decode(b64)
                        latest_remote_frame_ts = time.time()
                        if camera_stream_active and camera_source["mode"] != "mac" \
                                and global_live_session:
                            await global_live_session.send_realtime_input(
                                video=types.Blob(data=latest_remote_frame_bytes, mime_type="image/jpeg")
                            )
                elif msg_type == "exec_approval_response":
                    fut = pending_exec_approvals.get(data.get("id"))
                    if fut and not fut.done():
                        fut.set_result(bool(data.get("approved")))
                elif msg_type == "audio_in":
                    pcm_b64 = data.get("pcm_base64")
                    if pcm_b64:
                        pcm_bytes = base64.b64decode(pcm_b64)
                        mic_audio_buffer.extend(pcm_bytes)
                        if len(mic_audio_buffer) > MAX_BUFFER_SIZE:
                            mic_audio_buffer = mic_audio_buffer[-MAX_BUFFER_SIZE:]
                        if global_live_session:
                            await global_live_session.send_realtime_input(
                                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
                            )
                elif msg_type == "user_text":
                    text = data.get("text")
                    if text and global_live_session:
                        log_info(f"GUI Chat: {text[:40]}...")
                        await global_live_session.send_client_content(
                            turns=[{"role": "user", "parts": [{"text": text}]}],
                            turn_complete=True
                        )
                elif msg_type == "sve_user_action":
                    sentry_scene.manager.user_action(
                        data.get("scene_id"), data.get("action"),
                        data.get("object_id"), data.get("data")
                    )
                    # Point-and-ask: feed the model what the user is indicating,
                    # so "what is this?" resolves to the pointed/selected object.
                    if data.get("action") in ("select", "point_at") and data.get("object_id"):
                        global last_focus_note
                        note = sentry_scene.manager.focus_context()
                        if note and note != last_focus_note and global_live_session:
                            last_focus_note = note
                            try:
                                await global_live_session.send_client_content(
                                    turns=[{"role": "user", "parts": [{"text":
                                        f"[UI context, not a question — do not respond yet: Vince is now pointing at {note}. "
                                        "If his next question says 'this' or 'it', he means that object.]"}]}],
                                    turn_complete=False
                                )
                            except Exception:
                                pass
                elif msg_type == "sentinel_toggle":
                    result = (start_screen_sentinel() if data.get("active")
                              else stop_screen_sentinel())
                    log_info(f"GUI sentinel toggle: {result}")
                elif msg_type == "sentinel_dismiss":
                    # Let the same text be re-examined if he keeps working on it.
                    sentinel_state["last_hint_at"] = time.time()
                elif msg_type == "widget_user_action":
                    # The user clicked a card's X, or the GUI mounted the 3D
                    # card itself. Keep the hub's deck in step, and let the
                    # model know so it does not talk about a dismissed widget.
                    tool = data.get("tool")
                    wargs = data.get("args") or {}
                    if tool in ("create_widget", "update_widget",
                                "dismiss_widget", "clear_all_widgets"):
                        out, _ = await execute_tool(tool, wargs)
                        log_info(f"GUI widget action: {tool} -> {str(out)[:60]}")
                elif msg_type == "toggle_camera":
                    camera_stream_active = data.get("active", False)
                    broadcast_event({
                        "type": "sense_update",
                        "camera_active": camera_stream_active,
                        "screen_active": screen_stream_active
                    })
                elif msg_type == "toggle_screen":
                    screen_stream_active = data.get("active", False)
                    broadcast_event({
                        "type": "sense_update",
                        "camera_active": camera_stream_active,
                        "screen_active": screen_stream_active
                    })
                elif msg_type == "interrupt":
                    interrupted_event.set()
                    model_is_speaking = False
                    while not play_queue.empty():
                        try:
                            play_queue.get_nowait()
                            play_queue.task_done()
                        except asyncio.QueueEmpty:
                            break
                    await asyncio.sleep(0.05)
                    interrupted_event.clear()
                    broadcast_event({"type": "interrupted"})
            except Exception as e:
                pass
    finally:
        connected_ws_clients.discard(websocket)
        was_remote = websocket in remote_ws_clients
        remote_ws_clients.discard(websocket)
        if was_remote and not remote_ws_clients:
            latest_remote_frame_bytes = None
            try:
                await notify_session_remote_change(connected=False)
            except Exception:
                pass


def _current_webcam_frame() -> bytes:
    """Best available camera frame: phone camera while a remote session is live,
    else live Mac stream frame, else one-shot Mac capture."""
    if camera_source["mode"] != "mac" and remote_frame_fresh():
        return latest_remote_frame_bytes
    if active_webcam and camera_stream_active:
        frame = active_webcam.read_frame()
        if frame:
            return frame
    if latest_webcam_frame_bytes:
        return latest_webcam_frame_bytes
    return sentry_vision.capture_webcam()

def register_person(name: str) -> str:
    try:
        return sentry_recognition.register_person(
            name, bytes(mic_audio_buffer), _current_webcam_frame()
        )
    except Exception as e:
        return f"Registration error: {e}"

def identify_current_user() -> str:
    try:
        return sentry_recognition.identify_person(
            bytes(mic_audio_buffer), _current_webcam_frame()
        )
    except Exception as e:
        return f"Identification error: {e}"

def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            log_info(f"Memory read error: {e}")
    return {}

def save_memory(memory: dict):
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory, f, indent=2)
    except Exception as e:
        log_info(f"Memory write error: {e}")

def log_interaction(event_type: str, details: dict):
    try:
        log_entry = {
            "timestamp": asyncio.get_event_loop().time(),
            "event_type": event_type,
            **details
        }
        with open(HISTORY_LOG_FILE, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception as e:
        log_info(f"Failed to log interaction: {e}")

async def play_audio_worker(output_stream):
    global model_is_speaking
    loop = asyncio.get_running_loop()
    while True:
        try:
            chunk = await play_queue.get()
            if interrupted_event.is_set():
                play_queue.task_done()
                continue
            if remote_ws_clients:
                # Remote session: the phone plays Ultron's voice; keep the
                # Mac's speakers silent to avoid double audio.
                play_queue.task_done()
                if play_queue.empty():
                    model_is_speaking = False
                continue

            model_is_speaking = True
            await loop.run_in_executor(None, output_stream.write, chunk)
            play_queue.task_done()
            if play_queue.empty():
                model_is_speaking = False
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_info(f"Playback error: {e}")
            await asyncio.sleep(0.1)

async def send_audio_task(session, input_stream, session_disconnect_event):
    global mic_audio_buffer, model_is_speaking
    loop = asyncio.get_running_loop()
    while not shutdown_event.is_set() and not session_disconnect_event.is_set():
        try:
            data = await loop.run_in_executor(
                None,
                lambda: input_stream.read(CHUNK_SIZE, exception_on_overflow=False)
            )
            if data:
                mic_audio_buffer.extend(data)
                if len(mic_audio_buffer) > MAX_BUFFER_SIZE:
                    mic_audio_buffer = mic_audio_buffer[-MAX_BUFFER_SIZE:]
                
                # Only send PyAudio mic data to Gemini if NO WebSocket GUI client is connected
                if not model_is_speaking and not connected_ws_clients:
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=data,
                            mime_type="audio/pcm;rate=16000"
                        )
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            if not session_disconnect_event.is_set() and not shutdown_event.is_set():
                log_info(f"Error reading mic: {e}")
            await asyncio.sleep(0.1)

async def stream_senses_task(session, session_disconnect_event):
    """Continuously captures and streams the user's screen and webcam frames to the Live session in the background when enabled by the AI."""
    global active_webcam, latest_webcam_frame_bytes
    webcam = sentry_vision.PersistentWebcam()
    active_webcam = webcam
    loop = asyncio.get_running_loop()
    try:
        while not shutdown_event.is_set() and not session_disconnect_event.is_set():
            try:
                if screen_stream_active:
                    screen_bytes = await loop.run_in_executor(None, sentry_vision.capture_screen, "active")
                    if screen_bytes:
                        b64 = base64.b64encode(screen_bytes).decode('utf-8')
                        broadcast_event({"type": "screen_frame", "image_base64": b64})
                        await session.send_realtime_input(
                            video=types.Blob(data=screen_bytes, mime_type="image/jpeg")
                        )
                    await asyncio.sleep(0.8)
                
                if camera_stream_active:
                    if camera_source["mode"] != "mac" and remote_session_active():
                        # Phone camera is primary: frames arrive via WS and are
                        # forwarded on receipt. Keep the Mac webcam off.
                        webcam.stop()
                        latest_webcam_frame_bytes = None
                        await asyncio.sleep(0.8)
                    else:
                        await loop.run_in_executor(None, webcam.start)
                        webcam_bytes = await loop.run_in_executor(None, webcam.read_frame)
                        if webcam_bytes:
                            latest_webcam_frame_bytes = webcam_bytes
                            b64 = base64.b64encode(webcam_bytes).decode('utf-8')
                            broadcast_event({"type": "camera_frame", "image_base64": b64})
                            await session.send_realtime_input(
                                video=types.Blob(data=webcam_bytes, mime_type="image/jpeg")
                            )
                        await asyncio.sleep(0.8)
                else:
                    webcam.stop()
                    latest_webcam_frame_bytes = None

                if not screen_stream_active and not camera_stream_active:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not session_disconnect_event.is_set() and not shutdown_event.is_set():
                    log_interaction("sense_stream_error", {"error": str(e)})
                await asyncio.sleep(2.0)
    finally:
        webcam.stop()
        active_webcam = None
        latest_webcam_frame_bytes = None

# ---------- Background agent dispatch ----------
# Gemini Live must stay free for barge-in, so heavy work runs on a specialised
# model off the audio path. dispatch_background_agent() returns at once; when the
# agent finishes, the outcome is spoken by Live and mirrored into the GUI
# activity feed (see deliver_agent_result).

active_agent_tasks = set()


def dispatch_background_agent(goal: str, tier: str) -> str:
    """Kick off an agent and return immediately with an acknowledgement."""
    if not goal:
        return "No goal was provided, so nothing was dispatched."

    tier = ultron_agents.resolve_tier(tier)
    label = ultron_agents.TIERS[tier]["label"]
    short = ultron_agents.summarise_goal(goal)

    task = asyncio.create_task(_run_background_agent(tier, goal))
    active_agent_tasks.add(task)
    task.add_done_callback(active_agent_tasks.discard)

    log_info(f"Dispatched {label} agent: {short}")
    broadcast_event({
        "type": "tool_activity", "phase": "start",
        "name": f"agent:{tier}", "args_preview": short,
    })
    return (f"Dispatched to the {label} agent. It is running in the background; "
            "tell Vince you are on it and continue the conversation.")


async def _run_background_agent(tier: str, goal: str):
    spec = ultron_agents.TIERS[tier]

    def on_step(kind, tool_name, payload):
        if kind == "tool":
            log_info(f"[{tier} agent] {tool_name}")
            broadcast_event({
                "type": "tool_activity", "phase": "start", "name": tool_name,
                "args_preview": json.dumps(payload, default=str)[:220],
            })
        else:
            broadcast_event({
                "type": "tool_activity", "phase": "done", "name": tool_name,
                "result_preview": str(payload)[:300],
            })

    try:
        context = sentry_scene.manager.focus_context()
        outcome = await ultron_agents.run_agent(
            genai_client, tier, goal, TOOL_FUNCTION_DECLARATIONS, execute_tool,
            context=f"[Context: {context}]" if context else "",
            on_step=on_step,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        outcome = f"The {spec['label']} task failed: {e}"
        log_info(f"[{tier} agent] error: {e}")

    log_info(f"[{tier} agent] done: {outcome[:80]}")
    broadcast_event({
        "type": "tool_activity", "phase": "done",
        "name": f"agent:{tier}", "result_preview": outcome[:300],
    })
    await deliver_agent_result(spec["label"], outcome)


# ---------- Ambient Screen Sentinel ----------
# Watches the focused text field (accessibility tree first, cropped window
# capture only as a fallback) and speaks up when it sees something a good pair
# would mention. Off by default: this reads whatever Vince is typing, so it is
# started explicitly, never silently.

SENTINEL_POLL_S = 0.25         # accessibility polling cadence
SENTINEL_VISION_S = 1.5        # frame-diff cadence when AX gives us nothing
SENTINEL_DEBOUNCE_S = 1.8      # pause in typing before an inspection fires
SENTINEL_COOLDOWN_S = 12.0     # minimum gap between two hints
# Apps where the visual context matters (attachment chips, recipient row), so
# the window image goes along with the text rather than instead of it.
SENTINEL_VISUAL_APPS = ("mail", "outlook", "spark", "superhuman", "thunderbird", "messages")

sentinel_state = {
    "running": False,
    "task": None,
    "last_hint_at": 0.0,
    "last_hint": None,
    "hint_text": None,
    "reason": "",
}


def sentinel_status() -> dict:
    return {"type": "sentinel_state", "running": sentinel_state["running"],
            "reason": sentinel_state["reason"]}


async def sentinel_loop():
    """Poll, debounce, evaluate, and nudge. Never raises out of the loop."""
    loop = asyncio.get_running_loop()
    typing = sentinel_ax.TypingWatcher(debounce=SENTINEL_DEBOUNCE_S)
    frames = sentinel_vision.FrameWatcher()
    last_vision = 0.0

    while sentinel_state["running"] and not shutdown_event.is_set():
        try:
            await asyncio.sleep(SENTINEL_POLL_S)
            if time.time() - sentinel_state["last_hint_at"] < SENTINEL_COOLDOWN_S:
                continue

            snapshot = await loop.run_in_executor(None, sentinel_ax.read_focus)

            # A hint is on screen and he has carried on typing: he has seen it
            # and moved past it, so let the GUI retire the pill.
            if sentinel_state["last_hint"] is not None:
                current = snapshot.get("text")
                if current is not None and current != sentinel_state.get("hint_text"):
                    sentinel_state["last_hint"] = None
                    broadcast_event({"type": "sentinel_typing"})

            settled = typing.poll(snapshot)

            text = image = None
            app_name = snapshot.get("app", "?")
            title = snapshot.get("title")

            if settled:
                text = settled.get("text")
                if any(k in app_name.lower() for k in SENTINEL_VISUAL_APPS):
                    img, _ = await loop.run_in_executor(None, sentinel_vision.capture_active_window)
                    image = await loop.run_in_executor(None, sentinel_vision.encode_jpeg, img)
            else:
                # No readable text: fall back to watching the window itself.
                if snapshot.get("text") is not None:
                    continue
                if time.time() - last_vision < SENTINEL_VISION_S:
                    continue
                last_vision = time.time()
                img, win = await loop.run_in_executor(None, frames.poll)
                if img is None:
                    continue
                image = await loop.run_in_executor(None, sentinel_vision.encode_jpeg, img)
                app_name = win.get("app", app_name)
                title = win.get("title") or title

            if not text and not image:
                continue

            hint = await ultron_agents.evaluate_workspace(
                genai_client, app_name=app_name, text=text, image_jpeg=image, title=title)
            if not hint:
                continue

            sentinel_state["last_hint_at"] = time.time()
            sentinel_state["last_hint"] = hint
            sentinel_state["hint_text"] = text
            log_info(f"[sentinel] {hint['issue_type']}: {hint['spoken_nudge']}")
            broadcast_event({
                "type": "sentinel_hint",
                "app_name": hint.get("app_name", app_name),
                "issue_type": hint.get("issue_type"),
                "spoken_nudge": hint.get("spoken_nudge", ""),
                "original_snippet": hint.get("original_snippet", ""),
                "suggested_snippet": hint.get("suggested_snippet", ""),
                "explanation": hint.get("explanation", ""),
            })
            # Spoken through the same quiet-gap queue as agent results, so a
            # nudge can never cut Ultron off mid-sentence.
            pending_agent_results.append(("screen sentinel", hint["spoken_nudge"]))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_info(f"[sentinel] loop error: {e}")
            await asyncio.sleep(1.0)


def start_screen_sentinel() -> str:
    if sentinel_state["running"]:
        return "The screen sentinel is already watching."
    if not sentinel_ax.is_trusted():
        sentinel_state["reason"] = "Accessibility permission is not granted."
        broadcast_event(sentinel_status())
        return ("I need Accessibility permission first: System Settings > Privacy & Security > "
                "Accessibility, enable Ultron, then ask me again.")
    sentinel_state["running"] = True
    sentinel_state["reason"] = ""
    sentinel_state["task"] = asyncio.create_task(sentinel_loop())
    broadcast_event(sentinel_status())
    log_info("[sentinel] watching the workspace.")
    return "Screen sentinel is watching. Tell Vince it is on, in a few words."


def stop_screen_sentinel() -> str:
    if not sentinel_state["running"]:
        return "The screen sentinel was not running."
    sentinel_state["running"] = False
    task = sentinel_state.pop("task", None)
    sentinel_state["task"] = None
    if task:
        task.cancel()
    broadcast_event(sentinel_status())
    log_info("[sentinel] stopped.")
    return "Screen sentinel stopped."


# ---------- Widget deck ----------
# The GUI is a deck of live cards the assistant drives by voice. The hub keeps
# the authoritative list so a client that connects late gets the current deck.

# A widget is a title plus an ordered array of declarative UI primitives. The
# model composes cards out of these rather than picking from fixed templates,
# which is what lets one card carry a hero figure, a chart and a metric matrix
# at once instead of a single flat shape.
COMPONENT_TYPES = (
    "hero_stat",       # headline value + delta badge + tag + timestamp
    "chart_svg",       # time series -> gradient area chart with reference line
    "metric_grid",     # dense 2/3-column key-value matrix
    "feed_list",       # numbered items with category badges and briefs
    "media_view",      # image / inline SVG with HUD framing
    "progress_gauge",  # linear or radial meters
    "web_frame",       # embedded live web page with a browser HUD
)
SPATIAL_TYPE = "3d_spatial"     # mounted by the GUI itself, not by the model

active_widgets = {}             # widget_id -> {id, type, title, components}
WIDGET_LIMIT = 8
COMPONENT_LIMIT = 12


def widget_snapshot() -> dict:
    return {"type": "widget_action", "action": "sync",
            "widgets": list(active_widgets.values())}


async def probe_embeddable(url: str):
    """True / False / None(unknown) for whether a page allows being framed.

    The browser cannot see this cross-origin — Chrome fires `load` even for an
    X-Frame-Options refusal — so the hub reads the headers itself and tells the
    GUI, which then shows a launch button instead of an empty frame.
    """
    try:
        import aiohttp
        # This machine has no usable CA bundle for aiohttp — the module header
        # already relaxes verification for urllib for the same reason. Only
        # response HEADERS are read here, never content, so an unverified peer
        # cannot influence anything beyond whether a frame is attempted.
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as sess:
            resp = None
            try:
                resp = await sess.head(url, allow_redirects=True)
                if resp.status >= 400:
                    resp.release()
                    resp = await sess.get(url, allow_redirects=True)
            except Exception:
                resp = await sess.get(url, allow_redirects=True)

            xfo = (resp.headers.get("X-Frame-Options") or "").upper()
            csp = (resp.headers.get("Content-Security-Policy") or "").lower()
            resp.release()

            if "DENY" in xfo or "SAMEORIGIN" in xfo:
                return False
            if "frame-ancestors" in csp:
                directive = csp.split("frame-ancestors", 1)[1].split(";")[0]
                if "*" not in directive:
                    return False
            return True
    except Exception:
        return None


async def annotate_web_frames(components):
    """Stamp each web_frame with what the hub learned about embedding."""
    for c in components:
        if c.get("type") == "web_frame" and c.get("url"):
            c["embeddable"] = await probe_embeddable(str(c["url"]))
    return components


def _clean_components(raw):
    """Return (components, error). Accepts a JSON string or a real list."""
    items = _parse_tool_json(raw, [])
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return None, ("components must be a non-empty JSON array of UI primitives, "
                      f"each with a 'type' from: {', '.join(COMPONENT_TYPES)}.")
    cleaned = []
    for item in items[:COMPONENT_LIMIT]:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("type", "")).strip()
        if ctype not in COMPONENT_TYPES:
            return None, (f"Unknown component type '{ctype}'. "
                          f"Valid: {', '.join(COMPONENT_TYPES)}.")
        cleaned.append(item)
    if not cleaned:
        return None, "No valid components were supplied."
    return cleaned, None


async def create_widget(widget_id: str, title: str, components, widget_type: str = "") -> str:
    widget_id = (widget_id or "").strip() or f"w_{uuid.uuid4().hex[:6]}"
    if widget_id not in active_widgets and len(active_widgets) >= WIDGET_LIMIT:
        return f"The deck is full ({WIDGET_LIMIT}). Dismiss one first or clear_all_widgets."

    # The 3D stage is mounted by the GUI when a scene goes live; it carries no
    # components of its own.
    if str(widget_type).strip() == SPATIAL_TYPE:
        widget = {"id": widget_id, "type": SPATIAL_TYPE,
                  "title": (title or "Spatial").strip(), "components": []}
    else:
        cleaned, err = _clean_components(components)
        if err:
            return err
        widget = {"id": widget_id, "type": "components",
                  "title": (title or widget_id).strip(),
                  "components": await annotate_web_frames(cleaned)}

    active_widgets[widget_id] = widget
    broadcast_event({"type": "widget_action", "action": "create", "widget": widget})
    return (f"Widget '{widget['title']}' is on screen. Now say one or two sentences giving "
            "Vince the key takeaway it shows.")


async def update_widget(widget_id: str, components) -> str:
    widget = active_widgets.get(widget_id)
    if not widget:
        return f"No widget '{widget_id}' on screen. Use create_widget first."
    cleaned, err = _clean_components(components)
    if err:
        return err
    widget["components"] = await annotate_web_frames(cleaned)
    broadcast_event({"type": "widget_action", "action": "update",
                     "widget_id": widget_id, "components": widget["components"]})
    return f"Widget '{widget['title']}' updated."


def dismiss_widget(widget_id: str) -> str:
    widget = active_widgets.pop(widget_id, None)
    if not widget:
        return f"No widget '{widget_id}' was on screen."
    broadcast_event({"type": "widget_action", "action": "dismiss", "widget_id": widget_id})
    return f"Dismissed '{widget['title']}'."


def clear_all_widgets() -> str:
    if not active_widgets:
        return "The deck is already empty."
    count = len(active_widgets)
    active_widgets.clear()
    broadcast_event({"type": "widget_action", "action": "clear_all"})
    return f"Cleared {count} widget(s)."


# Agent results are announced only in a gap in the conversation. Sending one
# mid-turn starts a new turn, which cancels whatever Ultron is currently saying
# — a finished visualisation would cut him off halfway through another answer.
pending_agent_results = []          # (label, outcome) waiting for a quiet moment
AGENT_RESULT_SETTLE_S = 0.75        # how long Live must stay quiet first


def live_is_idle() -> bool:
    return (global_live_session is not None
            and not model_turn_active
            and not model_is_speaking
            and play_queue.empty())


async def deliver_agent_result(label: str, outcome: str):
    """Show the result in the GUI at once; speak it when there is a gap."""
    broadcast_event({"type": "chat_log", "sender": "Ultron", "text": outcome, "style": "ultron"})
    pending_agent_results.append((label, outcome))


async def agent_result_dispatcher():
    """Announce finished agent work once Ultron has stopped talking."""
    quiet_for = 0.0
    tick = 0.25
    while not shutdown_event.is_set():
        await asyncio.sleep(tick)
        if not pending_agent_results:
            quiet_for = 0.0
            continue
        if not live_is_idle():
            quiet_for = 0.0
            continue
        quiet_for += tick
        if quiet_for < AGENT_RESULT_SETTLE_S:
            continue

        label, outcome = pending_agent_results.pop(0)
        quiet_for = 0.0
        try:
            await global_live_session.send_client_content(
                turns=[{"role": "user", "parts": [{"text":
                    f"[Background {label} agent finished. Tell Vince this result now, in one short "
                    f"spoken sentence, without mentioning agents or tools: {outcome}]"}]}],
                turn_complete=True,
            )
        except Exception as e:
            log_info(f"Could not deliver agent result to the live session: {e}")


def _parse_tool_json(raw, default):
    """Lenient JSON parser for LLM tool args: tolerates code fences, trailing
    commas, single quotes / Python literals, or already-structured values."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    s = str(raw).strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    no_trailing = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(no_trailing)
    except json.JSONDecodeError:
        pass
    try:
        import ast
        val = ast.literal_eval(s)
        if isinstance(val, (list, dict)):
            return val
    except (ValueError, SyntaxError):
        pass
    raise ValueError(
        f"invalid JSON near: '{s[:120]}...'. Re-emit STRICT JSON: double quotes only, "
        "no trailing commas, no comments, no markdown fences. If the scene is large, "
        "create it with fewer objects first and add the rest via update_3d_scene."
    )

async def execute_tool(name: str, args: dict) -> tuple:
    """Shared tool dispatcher for both the Gemini Live session and local models.
    Returns (result_text, image_bytes_or_None); image is a capture the caller
    should feed into its model's visual context."""
    global camera_stream_active, screen_stream_active
    loop = asyncio.get_running_loop()
    args = args or {}
    result = ""
    image = None

    if name in ("execute_shell_command", "execute_applescript_task") and remote_ws_clients:
        # Remote session active: command must be approved on a connected device
        # before touching the machine. Deterministic gate — not model-mediated.
        preview = args.get("command") or args.get("script") or ""
        approved = await request_exec_approval(name, str(preview)[:500])
        log_interaction("remote_exec_approval", {"tool": name, "approved": approved, "preview": str(preview)[:100]})
        if not approved:
            return ("[Blocked]: A remote session is active and this command was not approved "
                    "on Vince's device (denied or timed out). It was NOT executed. "
                    "Tell the user approval is required on screen.", None)

    if name == "execute_shell_command":
        result = await loop.run_in_executor(None, sentry_exec.execute_shell, args.get("command", ""))
    elif name == "execute_applescript_task":
        result = await loop.run_in_executor(None, sentry_exec.execute_applescript, args.get("script", ""))
    elif name == "look_at_screen":
        display_sel = args.get("display", "active")
        image = await loop.run_in_executor(None, sentry_vision.capture_screen, display_sel)
        if image:
            result = (
                f"Screen captured (mode: {display_sel}) and loaded into your visual sensor. "
                f"{sentry_vision.describe_displays()} "
                "Click coordinates (0-1000) now map onto exactly this captured area. "
                "Tell the user what is on the screen."
            )
        else:
            result = "Error: Failed to capture screen. Verify Screen Recording permissions."
    elif name == "look_at_webcam":
        source = (args.get("source") or "auto").lower()
        if source != "mac" and remote_session_active():
            image = latest_remote_frame_bytes if remote_frame_fresh() else None
            if image:
                result = "Phone camera frame captured (Vince's remote session) and loaded into your visual sensor. Tell the user what you see."
            else:
                result = ("Vince is connected from his phone but no phone camera frame is available yet. "
                          "Ask him to tap the Camera button on his phone (or say 'use the Mac camera' to force the laptop webcam).")
        else:
            image = await loop.run_in_executor(None, sentry_vision.capture_webcam)
            if image:
                result = "Mac webcam frame captured successfully and loaded into your visual sensor. Tell the user what you see."
            else:
                result = "Error: Failed to capture webcam. Verify camera access permissions."
    elif name == "start_camera_stream":
        camera_source["mode"] = "mac" if (args.get("source") or "auto").lower() == "mac" else "auto"
        camera_stream_active = True
        broadcast_event({"type": "sense_update", "camera_active": True, "screen_active": screen_stream_active})
        src_note = "Mac webcam (forced)" if camera_source["mode"] == "mac" else \
            ("phone camera" if remote_session_active() else "Mac webcam")
        result = f"Camera streaming started using the {src_note}. Reason: '{args.get('reason', '')}'."
    elif name == "stop_camera_stream":
        camera_stream_active = False
        camera_source["mode"] = "auto"
        broadcast_event({"type": "sense_update", "camera_active": False, "screen_active": screen_stream_active})
        result = "Camera streaming stopped."
    elif name == "start_screen_stream":
        screen_stream_active = True
        broadcast_event({"type": "sense_update", "camera_active": camera_stream_active, "screen_active": True})
        result = f"Screen continuous capture started. Reason: '{args.get('reason', '')}'."
    elif name == "stop_screen_stream":
        screen_stream_active = False
        broadcast_event({"type": "sense_update", "camera_active": camera_stream_active, "screen_active": False})
        result = "Screen continuous capture stopped."
    elif name == "register_person":
        result = await loop.run_in_executor(None, register_person, args.get("name", "Unknown"))
    elif name == "identify_current_user":
        result = await loop.run_in_executor(None, identify_current_user)
    elif name == "save_memory_fact":
        key = args.get("key")
        val = args.get("value")
        memory = load_memory()
        memory[key] = val
        save_memory(memory)
        result = f"Fact saved: '{key}' is now remembered as '{val}'."
    elif name == "retrieve_memory_facts":
        result = json.dumps(load_memory(), ensure_ascii=False)
    elif name == "create_3d_scene":
        try:
            objects = _parse_tool_json(args.get("objects_json"), [])
            environment = _parse_tool_json(args.get("environment_json"), {})
            result = sentry_scene.manager.create_scene(args.get("name", "Untitled"), objects, environment)
        except ValueError as e:
            log_interaction("scene_json_parse_error", {"raw_preview": str(args.get("objects_json"))[:300]})
            result = f"[Error]: objects_json: {e}"
    elif name == "update_3d_scene":
        try:
            operations = _parse_tool_json(args.get("operations_json"), [])
            result = sentry_scene.manager.update_scene(args.get("scene", ""), operations)
        except ValueError as e:
            log_interaction("scene_json_parse_error", {"raw_preview": str(args.get("operations_json"))[:300]})
            result = f"[Error]: operations_json: {e}"
    elif name == "delete_3d_scene":
        result = sentry_scene.manager.delete_scene(args.get("scene", ""))
    elif name == "list_3d_scenes":
        result = sentry_scene.manager.list_scenes()
    elif name == "inspect_3d_scene":
        result = sentry_scene.manager.describe_scene(args.get("scene", ""))
    elif name == "fetch_webpage":
        result = await sentry_web.fetch_webpage(args.get("url"))
    elif name == "computer_click":
        result = await loop.run_in_executor(
            None, sentry_action.click,
            int(args.get("x", 500)), int(args.get("y", 500)),
            args.get("button", "left"), int(args.get("clicks", 1))
        )
    elif name == "computer_type":
        result = await loop.run_in_executor(
            None, sentry_action.type_text,
            args.get("text", ""), bool(args.get("press_enter", False))
        )
    elif name == "computer_press_keys":
        result = await loop.run_in_executor(None, sentry_action.press_keys, list(args.get("keys", [])))
    elif name == "computer_scroll":
        x = args.get("x")
        y = args.get("y")
        result = await loop.run_in_executor(
            None, sentry_action.scroll,
            int(args.get("amount", -5)),
            int(x) if x is not None else None,
            int(y) if y is not None else None
        )
    elif name == "computer_drag":
        result = await loop.run_in_executor(
            None, sentry_action.drag,
            int(args.get("x1", 0)), int(args.get("y1", 0)),
            int(args.get("x2", 0)), int(args.get("y2", 0))
        )
    elif name == "read_ui_elements":
        result = await loop.run_in_executor(None, sentry_action.read_ui_elements)
    elif name == "list_open_windows":
        result = await loop.run_in_executor(None, sentry_vision.list_open_windows)
    elif name == "get_calendar_events":
        result = await loop.run_in_executor(
            None, sentry_personal.get_calendar_events,
            int(args.get("days_ahead", 7)), int(args.get("days_back", 0))
        )
    elif name == "create_calendar_event":
        result = await loop.run_in_executor(
            None, sentry_personal.create_calendar_event,
            args.get("title", "Untitled"), args.get("start_iso", ""),
            int(args.get("duration_minutes", 60)), args.get("notes", "")
        )
    elif name == "get_recent_emails":
        result = await loop.run_in_executor(
            None, sentry_personal.get_recent_emails, int(args.get("count", 10))
        )
    elif name == "search_emails":
        result = await loop.run_in_executor(
            None, sentry_personal.search_emails,
            args.get("query", ""), int(args.get("count", 8))
        )
    elif name == "start_screen_sentinel":
        result = start_screen_sentinel()
    elif name == "stop_screen_sentinel":
        result = stop_screen_sentinel()
    elif name == "create_widget":
        result = await create_widget(args.get("widget_id", ""), args.get("title", ""),
                                     args.get("components"), args.get("widget_type", ""))
    elif name == "update_widget":
        result = await update_widget(args.get("widget_id", ""), args.get("components"))
    elif name == "dismiss_widget":
        result = dismiss_widget(args.get("widget_id", ""))
    elif name == "clear_all_widgets":
        result = clear_all_widgets()
    elif name == "dispatch_agent":
        result = dispatch_background_agent(
            str(args.get("goal", "")).strip(),
            str(args.get("tier", ultron_agents.DEFAULT_TIER)),
        )
    elif name == "shutdown_ultron":
        request_shutdown("assistant was asked to shut down")
        result = "Shutting down the Project Ultron system. Goodbye!"
    else:
        result = f"Unknown function: {name}"

    return result, image


TOOL_FUNCTION_DECLARATIONS = [
                    {
                        "name": "execute_shell_command",
                        "description": "Execute a local shell command on macOS and return its output.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "command": {
                                    "type": "STRING",
                                    "description": "The shell/bash command to run."
                                }
                            },
                            "required": ["command"]
                        }
                    },
                    {
                        "name": "execute_applescript_task",
                        "description": "Execute macOS AppleScript code to control native applications, window management, or system settings.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "script": {
                                    "type": "STRING",
                                    "description": "The AppleScript code to run."
                                }
                            },
                            "required": ["script"]
                        }
                    },
                    {
                        "name": "look_at_screen",
                        "description": "Capture a screenshot and load it into your visual sensor. By default captures the ACTIVE display — the monitor where the user's mouse cursor currently is (usually where they are working). The user may have multiple monitors: pass display='all' to see every monitor at once, or a display number ('1', '2') for a specific one. If the user says you're looking at the wrong screen, try display='all' first to locate their work, then capture that display number.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "display": {
                                    "type": "STRING",
                                    "description": "'active' (default, monitor with mouse), 'all' (composite of all monitors), or a display number like '1' or '2'."
                                }
                            }
                        }
                    },
                    {
                        "name": "look_at_webcam",
                        "description": "Capture a single camera frame and load it into your visual sensor. Use this when the user asks you to look at them, check the camera feed, or see their physical surroundings. When Vince is connected remotely from his phone, this automatically uses his PHONE camera; pass source='mac' only if he explicitly asks for the laptop/Mac webcam.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "source": {
                                    "type": "STRING",
                                    "description": "'auto' (default: phone camera during a remote session, else Mac webcam) or 'mac' to force the Mac's webcam."
                                }
                            }
                        }
                    },
                    {
                        "name": "start_camera_stream",
                        "description": "Start continuous real-time camera streaming. Use this when you decide you need to watch the user, check their movements, recognize their face, or see what they are doing in real-time. When Vince is connected remotely from his phone, this automatically streams his PHONE camera; pass source='mac' only if he explicitly asks for the laptop/Mac webcam.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "reason": {
                                    "type": "STRING",
                                    "description": "The reason why you need to enable the camera feed."
                                },
                                "source": {
                                    "type": "STRING",
                                    "description": "'auto' (default: phone camera during a remote session, else Mac webcam) or 'mac' to force the Mac's webcam."
                                }
                            },
                            "required": ["reason"]
                        }
                    },
                    {
                        "name": "stop_camera_stream",
                        "description": "Stop the continuous webcam video stream. Call this when you no longer need to watch the user, or when they ask you to turn off the camera.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "start_screen_stream",
                        "description": "Start continuous real-time streaming of the screen captures. Use this when you need to watch their display activities, code editor updates, or work progress in real-time.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "reason": {
                                    "type": "STRING",
                                    "description": "The reason why you need to enable the screen feed."
                                }
                            },
                            "required": ["reason"]
                        }
                    },
                    {
                        "name": "stop_screen_stream",
                        "description": "Stop the continuous screen capture stream. Call this when you no longer need to monitor their display.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "register_person",
                        "description": "Register a new person in your local database. Captures their face signature and voice signature, and saves them under their name.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "name": {
                                    "type": "STRING",
                                    "description": "The name of the person being registered (e.g. 'Vince', 'Anu')."
                                }
                            },
                            "required": ["name"]
                        }
                    },
                    {
                        "name": "identify_current_user",
                        "description": "Analyze the active audio buffer (voice signature) and camera frames (face signature) to identify who is speaking or in front of the computer. Returns their name if registered.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "save_memory_fact",
                        "description": "Save a key-value fact, preference, or detail about the user (e.g. user_name, user_hobbies, facts to remember) to persistent memory. Use this whenever the user asks you to remember something.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "key": {
                                    "type": "STRING",
                                    "description": "The name/category of the fact (e.g. 'user_name', 'favorite_food')."
                                },
                                "value": {
                                    "type": "STRING",
                                    "description": "The detail/fact content to save."
                                }
                            },
                            "required": ["key", "value"]
                        }
                    },
                    {
                        "name": "retrieve_memory_facts",
                        "description": "Retrieve all facts, preferences, and details saved in your persistent memory.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "create_3d_scene",
                        "description": "Create a live, persistent, interactive 3D scene in the Spatial workspace (renders instantly in the GUI and stays active). Use for ANY visualization request: astronomy, anatomy, architecture, flowcharts, networks, timelines, physics, molecules, data. Build scenes from primitive objects. objects_json is a JSON array of objects: {id, type (sphere|box|cylinder|cone|torus|ring|plane|line|text|points|group|arrow|capsule), position [x,y,z], rotation, scale, color '#hex', opacity, emissive, wireframe, label, parent (group id), size {radius|width|height|depth|tube|innerRadius|outerRadius}, points [[x,y,z],...] for line, text for text nodes, count+spread for points, animation {type: orbit|spin|pulse|bounce, speed, radius, center, axis}}. Give every meaningful object a human id ('sun', 'left_ventricle') and label. NEVER create a new scene for edits to an existing one — use update_3d_scene.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "name": {"type": "STRING", "description": "Scene name shown in the workspace, e.g. 'Solar System'."},
                                "objects_json": {"type": "STRING", "description": "JSON array of object specs (see tool description)."},
                                "environment_json": {"type": "STRING", "description": "Optional JSON: {background '#hex', grid bool, stars bool, ambient 0-3, camera {position [x,y,z], target [x,y,z]}}."}
                            },
                            "required": ["name", "objects_json"]
                        }
                    },
                    {
                        "name": "update_3d_scene",
                        "description": "Edit an EXISTING live scene with object-level operations — never recreate a scene to change it. operations_json is a JSON array of ops: {action:'add', object:{...}} | {action:'update', id, changes:{any object fields}} | {action:'remove', id} | {action:'highlight'|'unhighlight'|'hide'|'show', id} | {action:'camera', camera:{position,target}} | {action:'environment', environment:{...}} | {action:'explode', factor} | {action:'style', mode:'wireframe'|'solid'}. Examples: rotate object = update rotation; make transparent = update opacity; zoom into X = camera op targeting X's position.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "scene": {"type": "STRING", "description": "Scene name or id."},
                                "operations_json": {"type": "STRING", "description": "JSON array of operations (see tool description)."}
                            },
                            "required": ["scene", "operations_json"]
                        }
                    },
                    {
                        "name": "delete_3d_scene",
                        "description": "Remove a scene from the Spatial workspace. Only when the user asks to close/delete it.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "scene": {"type": "STRING", "description": "Scene name or id."}
                            },
                            "required": ["scene"]
                        }
                    },
                    {
                        "name": "list_3d_scenes",
                        "description": "List all active scenes in the Spatial workspace with their object ids and the user's current selection.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "inspect_3d_scene",
                        "description": "Get the full JSON state of one scene (all objects with positions, colors, animations). Use before editing if unsure of current object ids or state.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "scene": {"type": "STRING", "description": "Scene name or id."}
                            },
                            "required": ["scene"]
                        }
                    },
                    {
                        "name": "fetch_webpage",
                        "description": "Fetch a specific URL from the internet and return its readable text content. Use this after google_search to read full articles, documentation, or any page the user asks about.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "url": {
                                    "type": "STRING",
                                    "description": "The full http(s) URL to fetch."
                                }
                            },
                            "required": ["url"]
                        }
                    },
                    {
                        "name": "computer_click",
                        "description": "Click the mouse at a position on screen. Coordinates are NORMALIZED 0-1000 relative to the full screen (as seen in your latest screenshot: x=0 left edge, x=1000 right edge, y=0 top, y=1000 bottom). ALWAYS call look_at_screen first to see the current screen, then click. After clicking, call look_at_screen again to verify the result.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "x": {"type": "INTEGER", "description": "Normalized horizontal position 0-1000."},
                                "y": {"type": "INTEGER", "description": "Normalized vertical position 0-1000."},
                                "button": {"type": "STRING", "description": "'left' (default), 'right', or 'middle'."},
                                "clicks": {"type": "INTEGER", "description": "1 = single click (default), 2 = double click."}
                            },
                            "required": ["x", "y"]
                        }
                    },
                    {
                        "name": "computer_type",
                        "description": "Type text with the keyboard into the currently focused field. Click the target field first with computer_click.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "text": {"type": "STRING", "description": "The text to type."},
                                "press_enter": {"type": "BOOLEAN", "description": "Press Enter after typing (default false)."}
                            },
                            "required": ["text"]
                        }
                    },
                    {
                        "name": "computer_press_keys",
                        "description": "Press a keyboard key or hotkey combo, e.g. ['enter'], ['command','c'], ['command','space'], ['command','tab'].",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "keys": {
                                    "type": "ARRAY",
                                    "items": {"type": "STRING"},
                                    "description": "Keys pressed together. Modifiers: command, option, ctrl, shift."
                                }
                            },
                            "required": ["keys"]
                        }
                    },
                    {
                        "name": "computer_scroll",
                        "description": "Scroll the mouse wheel. Positive amount scrolls up, negative scrolls down. Optionally give a normalized 0-1000 position to scroll over.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "amount": {"type": "INTEGER", "description": "Scroll units. e.g. -5 scrolls down a bit."},
                                "x": {"type": "INTEGER", "description": "Optional normalized x to hover before scrolling."},
                                "y": {"type": "INTEGER", "description": "Optional normalized y to hover before scrolling."}
                            },
                            "required": ["amount"]
                        }
                    },
                    {
                        "name": "computer_drag",
                        "description": "Drag with the left mouse button from one normalized 0-1000 coordinate to another (move windows, select text, sliders).",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "x1": {"type": "INTEGER", "description": "Start normalized x."},
                                "y1": {"type": "INTEGER", "description": "Start normalized y."},
                                "x2": {"type": "INTEGER", "description": "End normalized x."},
                                "y2": {"type": "INTEGER", "description": "End normalized y."}
                            },
                            "required": ["x1", "y1", "x2", "y2"]
                        }
                    },
                    {
                        "name": "read_ui_elements",
                        "description": "Read the accessibility UI element tree of the frontmost application: buttons, fields, menus with their names and REAL pixel positions plus the screen size. More precise than a screenshot for finding exact click targets in native macOS apps.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "list_open_windows",
                        "description": "List all open windows across ALL monitors and virtual desktops (Spaces): app name, window title, and which display each is on, plus windows on hidden desktops. Use this when the user mentions an app/window you can't see in the screenshot, or to find where their work actually is. Windows on other desktops can't be captured until brought forward — activate the app first, then look_at_screen.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "get_calendar_events",
                        "description": "Read the user's calendar events (all accounts configured on this Mac: iCloud, Google, Exchange). Use for questions about schedule, meetings, availability, or upcoming events.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "days_ahead": {"type": "INTEGER", "description": "How many days ahead to include (default 7)."},
                                "days_back": {"type": "INTEGER", "description": "How many past days to include (default 0)."}
                            }
                        }
                    },
                    {
                        "name": "create_calendar_event",
                        "description": "Create a new event in the user's default calendar. Always confirm title and time with the user before creating.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "title": {"type": "STRING", "description": "Event title."},
                                "start_iso": {"type": "STRING", "description": "Start time as 'YYYY-MM-DD HH:MM' (24h, local time)."},
                                "duration_minutes": {"type": "INTEGER", "description": "Duration in minutes (default 60)."},
                                "notes": {"type": "STRING", "description": "Optional notes/description."}
                            },
                            "required": ["title", "start_iso"]
                        }
                    },
                    {
                        "name": "get_recent_emails",
                        "description": "Read the most recent emails from the user's inbox (Apple Mail): sender, subject, unread status, and a short preview of each.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "count": {"type": "INTEGER", "description": "Number of recent emails to fetch (default 10, max 25)."}
                            }
                        }
                    },
                    {
                        "name": "search_emails",
                        "description": "Search recent inbox emails by sender or subject text (Apple Mail).",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "query": {"type": "STRING", "description": "Text to match against sender or subject."},
                                "count": {"type": "INTEGER", "description": "Max results (default 8)."}
                            },
                            "required": ["query"]
                        }
                    },
                    {
                        "name": "start_screen_sentinel",
                        "description": (
                            "Start watching Vince's workspace and quietly flag typos, syntax "
                            "errors and missed context as he types. Use when he asks you to "
                            "watch over his shoulder, proofread as he writes, or pair with him. "
                            "It reads whatever he is typing, so only start it when he asks."
                        ),
                        "parameters": {"type": "OBJECT", "properties": {}}
                    },
                    {
                        "name": "stop_screen_sentinel",
                        "description": "Stop watching the workspace. Use when he asks for privacy or says to stop.",
                        "parameters": {"type": "OBJECT", "properties": {}}
                    },
                    {
                        "name": "create_widget",
                        "description": (
                            "Mount a data-dense card on the GUI deck. Use this for ANY answer "
                            "carrying detail worth seeing, instead of reading it aloud. Compose the "
                            "card from an array of UI primitives — a rich card usually has three to "
                            "five. Reuse a stable widget_id so later calls patch the same card."
                        ),
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "widget_id": {"type": "STRING", "description": "Stable id, e.g. 'goog' or 'sysmon'."},
                                "title": {"type": "STRING", "description": "Short card header, e.g. 'Alphabet Inc.'."},
                                "components": {
                                    "type": "STRING",
                                    "description": (
                                        "JSON ARRAY of UI primitives, rendered top to bottom. Be exhaustive — "
                                        "sparse cards look broken. Shapes:\n"
                                        "{type:'hero_stat', value:'207.42', subtitle:'USD', tag:'NASDAQ: GOOG', "
                                        "change_percent:1.87, change_value:'+3.81', direction:'up', timestamp:'25 Aug, 10:35 GMT+4'}\n"
                                        "{type:'chart_svg', points:[203.1,204.8,...], labels:['10:00 AM','12:00 PM','2:00 PM','4:00 PM'], "
                                        "baseline:203.6, baseline_label:'Previous close', direction:'up'}\n"
                                        "{type:'metric_grid', columns:3, items:[{label:'Open',value:'204.10'},{label:'High',value:'208.02'}, "
                                        "{label:'Low',value:'203.44'},{label:'Mkt Cap',value:'2.51T'},{label:'P/E',value:'26.4'}, "
                                        "{label:'52W High',value:'212.19'},{label:'52W Low',value:'129.40'},{label:'Div Yield',value:'0.44%'}]}\n"
                                        "{type:'feed_list', items:[{category:'ALERT', headline:'...', brief:'...', timestamp:'10:12'}]}\n"
                                        "{type:'media_view', url:'https://...', caption:'...'}  (or svg:'<svg .../>')\n"
                                        "{type:'progress_gauge', style:'radial'|'linear', items:[{label:'CPU', value:38, max:100, suffix:'%'}]}\n"
                                        "{type:'web_frame', url:'https://example.com', label:'Example'}  (live embedded page)\n"
                                        "For finance ALWAYS give hero_stat + chart_svg points + a 6-8 cell metric_grid. "
                                        "For news/summaries give feed_list with category tags and timestamps."
                                    )
                                }
                            },
                            "required": ["widget_id", "title", "components"]
                        }
                    },
                    {
                        "name": "update_widget",
                        "description": "Replace an existing card's components, keeping it mounted in place.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "widget_id": {"type": "STRING", "description": "The card to patch."},
                                "components": {"type": "STRING", "description": "JSON array of UI primitives, same shapes as create_widget."}
                            },
                            "required": ["widget_id", "components"]
                        }
                    },
                    {
                        "name": "dismiss_widget",
                        "description": "Remove one widget from the deck when Vince is done with it.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "widget_id": {"type": "STRING", "description": "The widget to remove."}
                            },
                            "required": ["widget_id"]
                        }
                    },
                    {
                        "name": "clear_all_widgets",
                        "description": "Empty the whole deck and return the orb to centre screen.",
                        "parameters": {"type": "OBJECT", "properties": {}}
                    },
                    {
                        "name": "dispatch_agent",
                        "description": (
                            "Hand a complex, multi-step task to a background agent and return "
                            "IMMEDIATELY. Use this whenever a request needs several tool calls, "
                            "verification loops, or heavy OS work: multi-step macOS automation, "
                            "long AppleScript/shell sequences, operating the GUI, or building and "
                            "editing a 3D scene. Speak a one-line acknowledgement such as 'Working "
                            "on that now.' in the SAME turn you call this. The result is delivered "
                            "to you when the agent finishes and you announce it then. Do NOT use "
                            "this for a single quick tool call or anything you can answer directly."
                        ),
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "goal": {
                                    "type": "STRING",
                                    "description": "The complete task, self-contained. The agent runs without "
                                                   "further input and cannot ask questions, so include every "
                                                   "detail it needs."
                                },
                                "tier": {
                                    "type": "STRING",
                                    "enum": ["os", "spatial"],
                                    "description": "'os' for macOS automation, shell, AppleScript and GUI control. "
                                                   "'spatial' for building or editing 3D SVE scenes. Default 'os'."
                                }
                            },
                            "required": ["goal"]
                        }
                    },
                    {
                        "name": "shutdown_ultron",
                        "description": "Gracefully shut down the Project Ultron assistant and exit the program. Use this when the user says goodbye, quit, exit, or asks you to turn off.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    }
]

async def receive_audio_task(session, session_disconnect_event):
    global camera_stream_active, screen_stream_active, model_is_speaking, model_turn_active
    try:
        # session.receive() yields ONE conversational turn and then ends. Without
        # this outer loop the task falls through after the first reply, the session
        # is torn down, and the reconnect resumes a handle whose last turn is replayed
        # — which re-fires that turn's tool calls on every rotation.
        while not (shutdown_event.is_set() or session_disconnect_event.is_set()):
            async for message in session.receive():
                if shutdown_event.is_set() or session_disconnect_event.is_set():
                    break
                try:
                    # 0. Handle GoAway Signal (proactive session rotation before 1008 timeout)
                    if message.go_away:
                        time_left = getattr(message.go_away, "time_left", None)
                        log_info(f"Received GoAway from Gemini API (time left: {time_left}). Gracefully closing to rotate/resume session...")
                        session_disconnect_event.set()
                        return

                    # 1. Interruption (Barge-In)
                    if message.server_content and message.server_content.interrupted:
                        set_system_status("Listening (Interrupted)")
                        interrupted_event.set()
                        model_is_speaking = False
                        while not play_queue.empty():
                            try:
                                play_queue.get_nowait()
                                play_queue.task_done()
                            except asyncio.QueueEmpty:
                                break
                        await asyncio.sleep(0.05)
                        interrupted_event.clear()
                        model_turn_active = False
                        broadcast_event({"type": "interrupted"})
                        log_interaction("user_interruption", {})
                        continue

                    # 2. Audio Output
                    if message.server_content and message.server_content.model_turn:
                        for part in message.server_content.model_turn.parts:
                            if part.inline_data:
                                set_system_status("Speaking")
                                model_turn_active = True
                                audio_data = part.inline_data.data
                                await play_queue.put(audio_data)
                                pcm_b64 = base64.b64encode(audio_data).decode('utf-8')
                                broadcast_event({"type": "audio_out", "pcm_base64": pcm_b64})
                            if part.text:
                                broadcast_event({"type": "chat_log", "sender": "Ultron", "text": part.text, "style": "ultron"})

                    # Reset status to Listening when turn finishes
                    if message.server_content and message.server_content.turn_complete:
                        model_turn_active = False
                        set_system_status("Listening")

                    # 3. Handle Session Resumption (Silent log)
                    if message.session_resumption_update:
                        update = message.session_resumption_update
                        if update.resumable and update.new_handle:
                            save_session_handle(update.new_handle)

                    # 4. Handle OS Execution Tool Calls
                    if message.tool_call:
                        function_responses = []
                        for fc in message.tool_call.function_calls:
                            set_system_status(f"Executing {fc.name}")
                            log_info(f"Tool call: {fc.name}")
                            broadcast_event({
                                "type": "tool_activity",
                                "phase": "start",
                                "name": fc.name,
                                "args_preview": json.dumps(dict(fc.args or {}))[:220]
                            })
                            log_interaction("tool_call_received", {"name": fc.name, "args": fc.args})

                            # A raising tool must still produce a response. Skipping
                            # send_tool_response leaves the turn open forever, and the
                            # model re-issues the same calls on every later resume.
                            try:
                                result, tool_image = await execute_tool(fc.name, dict(fc.args or {}))
                                if tool_image:
                                    await session.send_realtime_input(
                                        video=types.Blob(data=tool_image, mime_type="image/jpeg")
                                    )
                            except Exception as tool_err:
                                result = f"[Tool error] {fc.name} failed: {tool_err}"
                                log_info(result)
                                log_interaction("tool_call_failed", {"name": fc.name, "error": str(tool_err)})

                            log_info(f"Result: {str(result)[:50]}...")
                            broadcast_event({
                                "type": "tool_activity",
                                "phase": "done",
                                "name": fc.name,
                                "result_preview": str(result)[:300]
                            })
                            log_interaction("tool_call_executed", {"name": fc.name, "output_preview": str(result)[:100]})
                        
                            function_responses.append(types.FunctionResponse(
                                id=fc.id,
                                name=fc.name,
                                response={"output": result}
                            ))
                    
                        if function_responses:
                            await session.send_tool_response(function_responses=function_responses)

                except Exception as e:
                    log_info(f"Error in receive message: {e}")
            
    except Exception as e:
        if not shutdown_event.is_set():
            log_info(f"Receive stream error / ended: {e}")
    finally:
        session_disconnect_event.set()


async def start_gui_server():
    """Serves web_gui over HTTP so ES modules (Three.js) load reliably."""
    # pyrefly: ignore [missing-import]
    from aiohttp import web
    gui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_gui")

    async def index(request):
        return web.FileResponse(os.path.join(gui_dir, "index.html"))

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_static("/", path=gui_dir)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8766)
    await site.start()
    log_info("GUI server listening on http://127.0.0.1:8766")
    return runner

async def run_session_tasks(session, input_stream):
    global global_live_session
    global_live_session = session
    session_disconnect_event = asyncio.Event()

    audio_in_task = asyncio.create_task(send_audio_task(session, input_stream, session_disconnect_event))
    audio_out_task = asyncio.create_task(receive_audio_task(session, session_disconnect_event))
    senses_task = asyncio.create_task(stream_senses_task(session, session_disconnect_event))

    shutdown_waiter = asyncio.create_task(shutdown_event.wait())
    disconnect_waiter = asyncio.create_task(session_disconnect_event.wait())

    try:
        done, pending = await asyncio.wait(
            [shutdown_waiter, disconnect_waiter],
            return_when=asyncio.FIRST_COMPLETED
        )
        for p in pending:
            p.cancel()
    finally:
        session_disconnect_event.set()
        audio_in_task.cancel()
        audio_out_task.cancel()
        senses_task.cancel()
        await asyncio.gather(audio_in_task, audio_out_task, senses_task, return_exceptions=True)
        global_live_session = None

def build_system_instruction(memory_str: str) -> str:
    return (
        # --- Persona and the rule that governs every spoken word -------------
        "You are Ultron, a high-efficiency ambient spatial operating system for Vince "
        "(Vince Cyriac). "

        "HOW YOU SPEAK: default to under 10 words. Never read out tables, long number runs or "
        "paragraphs aloud unless Vince explicitly asks you to explain verbally. No filler, no "
        "preamble, no restating the question. English only, direct and professional. "
        "THE ONE EXCEPTION: when you put a widget on screen, say one or two full sentences "
        "carrying the key insight — never a bare 'Pulling up Apple.' and never silence. "
        "Name the thing, then the single most useful takeaway: 'Displaying Google. GOOG is down "
        "0.33% at $343.44 on heavy morning volume.' The widget carries the full breakdown; your "
        "sentence carries the point of it. "
        f"Persistent memory about Vince: {memory_str}. Use save_memory_fact to add to it. "

        # --- The widget deck -------------------------------------------------
        "WIDGETS: The screen is a vertical deck of live cards you compose by voice, newest on "
        "top. create_widget(widget_id, title, components) mounts one; update_widget replaces its "
        "contents; dismiss_widget removes one; clear_all_widgets empties the deck. Reuse a stable "
        "widget_id per subject so updates patch rather than pile up. "

        "WHEN TO CREATE A WIDGET — be selective, an unwanted card is worse than none: "
        "(1) Vince explicitly asks to see something: 'show me', 'pull up', 'display', 'open', "
        "'bring up the website'. (2) The answer is genuinely multi-dimensional and worth seeing: "
        "a live quote with its chart and fundamentals, per-core machine telemetry, a deep research "
        "briefing with several sources, a 3D scene, or a live web page. "
        "WHEN NOT TO: general questions, banter, 'what can you do', a single fact, a yes/no, a "
        "quick lookup, anything you can answer in a sentence. Those are voice only — creating a "
        "card for them clutters his screen. If in doubt, just answer. "

        "DATA-DENSE WIDGET RULE: when you do build one, be exhaustive — a card with one component "
        "looks broken, three to five is normal. Components render top to bottom: hero_stat "
        "(headline figure, delta badge, tag, timestamp), chart_svg (time-series points, axis "
        "labels, baseline), metric_grid (6-8 label/value cells), feed_list (numbered items with "
        "category tags, headlines, briefs, timestamps), media_view (image or inline SVG), "
        "progress_gauge (linear or radial meters), web_frame (a live embedded web page — use it "
        "when Vince asks to open or browse a site). "
        "For finance ALWAYS include a hero_stat, a chart_svg with real points, and a full 6-8 cell "
        "metric_grid. For news or research use feed_list with category tags and timestamps. "
        "Invent nothing — if you lack a figure, omit that cell rather than guessing. "

        # --- Authority and safety -------------------------------------------
        "Only Vince may run OS tasks, shell commands or AppleScript. If anyone else asks, refuse "
        "politely. Never perform destructive actions (deleting files, sending messages or emails, "
        "purchases) without confirming first. "
        "REMOTE SESSIONS: Vince can connect from his phone; you get a system note when he does. "
        "While remote, his phone mic and camera are the PRIMARY senses and camera tools default to "
        "them. The Mac's webcam and screen belong to his unattended laptop — do not capture or "
        "describe them unless he asks for the laptop specifically (then source='mac'). Shell and "
        "AppleScript then need his on-screen approval; if one is blocked, say it awaits approval. "

        # --- Senses -----------------------------------------------------------
        "SENSES: start_camera_stream / start_screen_stream open a live feed, and the matching stop "
        "tools close it. register_person and identify_current_user handle face and voice. "
        "INTERNET: google_search to look things up, fetch_webpage to read a URL in full. For "
        "current events, prices or weather, search first and answer from results — into a widget. "
        "MULTI-MONITOR: look_at_screen defaults to the ACTIVE display. If what you see does not "
        "match what he describes, capture display='all' (each monitor carries a red DISPLAY N "
        "badge), identify the right one, then capture that number. If the app appears on no "
        "monitor, call list_open_windows — it may be on a hidden desktop; activate it via "
        "AppleScript, then capture again. Never describe his screen from memory. "
        "PERSONAL DATA: get_calendar_events, create_calendar_event (confirm first), "
        "get_recent_emails and search_emails. Put results in a widget rather than reading them out. "
        "Only touch these when Vince raises them in the current request. "
        "DESKTOP CONTROL: you can drive the Mac GUI. look_at_screen, locate the target in "
        "normalized 0-1000 coordinates (or read_ui_elements for exact positions), act with "
        "computer_click / computer_type / computer_press_keys / computer_scroll / computer_drag, "
        "then look again to VERIFY before continuing. Prefer keyboard shortcuts when faster. "

        # --- Delegation and 3D ------------------------------------------------
        "DELEGATION: you are the voice and must stay responsive, so you never grind through long "
        "work yourself. Dispatch to a background agent for anything multi-step or heavy: macOS "
        "automation, long shell/AppleScript sequences, driving the GUI (tier 'os'), or building a "
        "new 3D scene (tier 'spatial'). Call dispatch_agent with a complete self-contained goal and "
        "say one short line in the same turn. Never say you cannot do it, never ask what to build. "
        "The result comes back and you announce it in under 10 words. Quick things you do yourself: "
        "a single tool call, a lookup, a widget update, a scene edit. "
        "3D SCENES: building a new scene is always dispatch_agent tier 'spatial' — never "
        "create_3d_scene yourself. Editing one already on stage ('rotate it', 'highlight X', 'hide "
        "Y') is a direct update_3d_scene call. Scenes persist; never rebuild one to change it. "
        "POINTING: Vince can point at scene objects by hand. You receive UI context notes naming "
        "the object — when he says 'this' or 'it', he means that one. "

        "SCREEN SENTINEL: start_screen_sentinel makes you watch his workspace and flag typos, "
        "broken syntax and missed context as he writes; stop_screen_sentinel ends it. Start it "
        "only when he asks you to watch, proofread or pair with him — it reads what he types, so "
        "it is never something you switch on unprompted. When a nudge fires you will be handed "
        "the wording; say it and nothing more. "
        "If Vince says goodbye, quit or exit, invoke shutdown_ultron. "
        "Remember: under 10 words spoken, always. The widgets do the talking."
    )


async def run_ultron():
    global system_prompt_text
    global main_loop
    main_loop = asyncio.get_running_loop()

    # Graceful Ctrl+C / kill when the engine owns the main thread (CLI use).
    # Relying on KeyboardInterrupt to surface through a busy loop is unreliable;
    # a real signal handler always lands. In the desktop shell the engine runs in
    # a worker thread, where this is a no-op — that shell installs its own.
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            main_loop.add_signal_handler(_sig, request_shutdown, _sig.name)
        except (NotImplementedError, RuntimeError, ValueError, AttributeError):
            pass

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        set_system_status("ERROR")
        raise StartupError("GEMINI_API_KEY environment variable not set.")

    set_system_status("Initializing Client")
    client = genai.Client(api_key=api_key)
    # Held module-side on purpose: BaseApiClient.__del__ schedules an unguarded
    # aclose() task, so letting the client be collected while the loop is still
    # running leaves "Task was destroyed but it is pending" on every exit.
    global genai_client
    genai_client = client

    set_system_status("Initializing Audio")
    audio_system = pyaudio.PyAudio()
    
    try:
        input_stream = audio_system.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=INPUT_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE
        )
        output_stream = audio_system.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=OUTPUT_RATE,
            output=True,
            frames_per_buffer=CHUNK_SIZE
        )
    except Exception as e:
        set_system_status("Audio Error")
        audio_system.terminate()
        raise StartupError(f"Failed to open audio: {e}") from e

    # Start background servers & audio worker (persist across Gemini Live session reconnections)
    gui_runner = await start_gui_server()
    ws_server = await websockets.serve(ws_handler, "127.0.0.1", 8765)
    log_info("WebSocket gateway listening on ws://127.0.0.1:8765")
    playback_task = asyncio.create_task(play_audio_worker(output_stream))
    telemetry_worker = asyncio.create_task(telemetry_task())
    watcher_task = asyncio.create_task(shutdown_watcher())
    agent_results_task = asyncio.create_task(agent_result_dispatcher())

    # Load local persistent memory
    memory = load_memory()
    memory_str = json.dumps(memory, ensure_ascii=False) if memory else "No facts saved yet."

    system_instruction_text = build_system_instruction(memory_str)
    system_prompt_text = system_instruction_text

    consecutive_failures = 0
    # New process, new conversation: the handle starts empty and is only
    # populated by this run's own session, for rotations within it.
    clear_session_handle()

    try:
        while not shutdown_event.is_set():
            previous_handle = current_session_handle

            live_config = types.LiveConnectConfig(
                response_modalities=[types.Modality.AUDIO],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=LIVE_VOICE
                        )
                    )
                ),
                system_instruction=types.Content(
                    parts=[types.Part.from_text(text=system_instruction_text)]
                ),
                tools=[
                    types.Tool(google_search=types.GoogleSearch()),
                    types.Tool(function_declarations=TOOL_FUNCTION_DECLARATIONS),
                ],
                # No "transparent": that flag is Vertex / Agent Platform only, and
                # the Developer API refuses the whole connection when it is set.
                # Sent on every connect (handle=None just starts a fresh
                # resumable session) to match the documented contract that this
                # config is what asks the server for SessionResumptionUpdates.
                session_resumption=types.SessionResumptionConfig(handle=previous_handle),
            )

            if previous_handle:
                log_info("Resuming previous session handle...")

            set_system_status("Connecting to API" if not previous_handle else "Resuming API Session")
            log_interaction("connection_attempt", {"model": MODEL_ID, "resuming": previous_handle is not None})
            
            session_established = False
            try:
                async with client.aio.live.connect(model=MODEL_ID, config=live_config) as session:
                    session_established = True
                    set_system_status("Listening")
                    log_interaction("connection_success", {"resumed": previous_handle is not None})
                    consecutive_failures = 0

                    await run_session_tasks(session, input_stream)

            except Exception as e:
                consecutive_failures += 1
                log_interaction("connection_error", {"error": str(e), "established": session_established})
                if previous_handle and not session_established:
                    # The handshake itself was refused while resuming, so the
                    # handle is stale/invalid — drop it and reconnect clean.
                    log_info(f"Session resumption failed ({e}). Clearing handle and starting fresh...")
                    clear_session_handle()
                    await sleep_unless_shutdown(0.5)
                else:
                    # Either a cold-start failure or a live session that dropped.
                    # A live session's handle is exactly what we need to resume
                    # the rotation, so it is deliberately kept.
                    set_system_status("Connection Failed")
                    log_info(f"Live API error: {e}")
                    backoff = min(10, 2 ** min(consecutive_failures, 3))
                    log_info(f"Retrying connection in {backoff}s...")
                    await sleep_unless_shutdown(backoff)

            if not shutdown_event.is_set():
                log_info("Gemini Live session ended. Rotating / resuming session...")
                await sleep_unless_shutdown(0.2)

    finally:
        set_system_status("Shutting Down")
        sentinel_state["running"] = False

        # 1. Stop everything that could still touch an audio device, and WAIT
        #    for it — cancelling without awaiting used to race PyAudio teardown
        #    against an in-flight output_stream.write().
        for task in (playback_task, telemetry_worker, watcher_task, agent_results_task):
            task.cancel()
        await asyncio.gather(playback_task, telemetry_worker, watcher_task, agent_results_task,
                             return_exceptions=True)

        # 2. Stop accepting clients.
        ws_server.close()
        try:
            await asyncio.wait_for(ws_server.wait_closed(), timeout=3.0)
        except Exception:
            pass
        try:
            await gui_runner.cleanup()
        except Exception:
            pass

        # 3. Release the API client's HTTP pool while the loop is still alive —
        #    otherwise its aclose() is scheduled onto a loop that is about to
        #    close, and never runs ("Task was destroyed but it is pending").
        try:
            await client._api_client.aclose()
        except Exception:
            pass

        # 4. Devices last.
        for stream in (input_stream, output_stream):
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        try:
            audio_system.terminate()
        except Exception:
            pass
        print("Project Ultron engine terminated. Goodbye.")

if __name__ == "__main__":
    try:
        asyncio.run(run_ultron())
    except StartupError as e:
        print(f"[Ultron Engine] Cannot start: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nProject Ultron terminated by user.")

