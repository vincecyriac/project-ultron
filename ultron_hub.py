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
import ssl
import base64
import websockets
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Import capability submodules
import sentry_vision
import sentry_exec
import sentry_action
import sentry_recognition
import sentry_web
import sentry_local
import sentry_personal
import sentry_scene

# Load environment variables
load_dotenv()

# Bypass SSL Verification issues for raw downloads on macOS
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

SESSION_HANDLE_FILE = ".session_handle.json"
HISTORY_LOG_FILE = "ultron_history.jsonl"
MEMORY_FILE = "ultron_memory.json"

# Model selection: defaults to gemini-3.1-flash-live-preview, customizable via env
MODEL_ID = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")

# Local models served by LM Studio (OpenAI-compatible API)
LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
LOCAL_MODELS = [m.strip() for m in os.getenv("LOCAL_MODELS", "").split(",") if m.strip()]

def get_model_registry() -> list:
    models = [{"id": MODEL_ID, "type": "gemini", "label": f"Gemini Live · {MODEL_ID}"}]
    for m in LOCAL_MODELS:
        models.append({"id": m, "type": "local", "label": f"Local · {m}"})
    return models

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
connected_ws_clients = set()
remote_ws_clients = set()  # clients reached via Tailscale Serve (phone, other devices)
pending_exec_approvals = {}  # approval_id -> asyncio.Future[bool]
latest_remote_frame_bytes = None  # last camera frame pushed by a remote client
latest_remote_frame_ts = 0.0
camera_source = {"mode": "auto"}  # auto = phone camera when remote session live; mac = force Mac webcam
global_live_session = None
active_model = {"type": "gemini", "id": MODEL_ID}
local_client = None
system_prompt_text = ""
last_focus_note = ""

def gemini_mode() -> bool:
    return active_model["type"] == "gemini"

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


def remote_frame_fresh() -> bool:
    return latest_remote_frame_bytes is not None and (time.time() - latest_remote_frame_ts) < 5.0

def remote_session_active() -> bool:
    return bool(remote_ws_clients)

async def notify_session_remote_change(connected: bool):
    """Tells the live model which device Vince is on, so senses default correctly."""
    if not (gemini_mode() and global_live_session):
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
    await websocket.send(json.dumps({
        "type": "model_list",
        "models": get_model_registry(),
        "active_id": active_model["id"]
    }))
    await websocket.send(json.dumps(sentry_scene.manager.workspace_snapshot()))
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
                                and gemini_mode() and global_live_session:
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
                        if gemini_mode() and global_live_session:
                            await global_live_session.send_realtime_input(
                                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
                            )
                elif msg_type == "user_text":
                    text = data.get("text")
                    if text and not gemini_mode():
                        log_info(f"GUI Chat (local): {text[:40]}...")
                        asyncio.create_task(handle_local_chat(text))
                    elif text and global_live_session:
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
                        if note and note != last_focus_note and gemini_mode() and global_live_session:
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
                elif msg_type == "select_model":
                    mid = data.get("id")
                    entry = next((m for m in get_model_registry() if m["id"] == mid), None)
                    if entry:
                        active_model["type"] = entry["type"]
                        active_model["id"] = entry["id"]
                        broadcast_event({
                            "type": "model_changed",
                            "id": entry["id"],
                            "model_type": entry["type"],
                            "label": entry["label"]
                        })
                        if entry["type"] == "local":
                            log_info(f"Switched to local model: {entry['id']} (text chat; voice stays with Gemini Live).")
                        else:
                            log_info(f"Switched to Gemini Live: {entry['id']}.")
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

async def send_audio_task(session, input_stream):
    global mic_audio_buffer, model_is_speaking
    loop = asyncio.get_running_loop()
    while True:
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
                if not model_is_speaking and not connected_ws_clients and gemini_mode():
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=data,
                            mime_type="audio/pcm;rate=16000"
                        )
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            log_info(f"Error reading mic: {e}")
            await asyncio.sleep(0.1)

async def stream_senses_task(session):
    """Continuously captures and streams the user's screen and webcam frames to the Live session in the background when enabled by the AI."""
    global active_webcam, latest_webcam_frame_bytes
    webcam = sentry_vision.PersistentWebcam()
    active_webcam = webcam
    loop = asyncio.get_running_loop()
    try:
        while not shutdown_event.is_set():
            try:
                if screen_stream_active:
                    screen_bytes = await loop.run_in_executor(None, sentry_vision.capture_screen, "active")
                    if screen_bytes:
                        b64 = base64.b64encode(screen_bytes).decode('utf-8')
                        broadcast_event({"type": "screen_frame", "image_base64": b64})
                        if gemini_mode():
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
                            if gemini_mode():
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
                log_interaction("sense_stream_error", {"error": str(e)})
                await asyncio.sleep(2.0)
    finally:
        webcam.stop()
        active_webcam = None
        latest_webcam_frame_bytes = None

async def handle_local_chat(text: str):
    """Routes a GUI text message through the active LM Studio model with tool support."""
    global local_client
    try:
        set_system_status(f"Thinking ({active_model['id']})")
        if local_client is None or local_client.model != active_model["id"]:
            local_client = sentry_local.LocalModelClient(
                LMSTUDIO_BASE_URL, active_model["id"], TOOL_FUNCTION_DECLARATIONS
            )

        def on_tool(name, args):
            set_system_status(f"Executing {name}")
            broadcast_event({
                "type": "tool_activity", "phase": "start", "name": name,
                "args_preview": json.dumps(args)[:220]
            })
            log_interaction("tool_call_received", {"name": name, "args": args, "backend": "local"})

        def on_tool_done(name, result):
            broadcast_event({
                "type": "tool_activity", "phase": "done", "name": name,
                "result_preview": str(result)[:300]
            })
            log_interaction("tool_call_executed", {"name": name, "output_preview": str(result)[:100], "backend": "local"})

        focus = sentry_scene.manager.focus_context()
        if focus:
            text = f"[Context: user is currently pointing at {focus}.]\n{text}"
        reply = await local_client.chat(text, system_prompt_text, execute_tool, on_tool, on_tool_done)
        if reply:
            broadcast_event({"type": "chat_log", "sender": "Ultron", "text": reply, "style": "ultron"})
    except Exception as e:
        broadcast_event({
            "type": "chat_log", "sender": "System",
            "text": f"Local model error: {e}. Is LM Studio's server running (Developer tab -> Start Server) and the model '{active_model['id']}' loaded?",
            "style": "system"
        })
    finally:
        set_system_status("Listening")

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
    elif name == "shutdown_ultron":
        shutdown_event.set()
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
                        "name": "shutdown_ultron",
                        "description": "Gracefully shut down the Project Ultron assistant and exit the program. Use this when the user says goodbye, quit, exit, or asks you to turn off.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    }
]

async def receive_audio_task(session):
    global camera_stream_active, screen_stream_active, model_is_speaking
    try:
        while not shutdown_event.is_set():
            async for message in session.receive():
                try:
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
                        broadcast_event({"type": "interrupted"})
                        log_interaction("user_interruption", {})
                        continue

                    # 2. Audio Output
                    if message.server_content and message.server_content.model_turn:
                        for part in message.server_content.model_turn.parts:
                            if part.inline_data:
                                set_system_status("Speaking")
                                audio_data = part.inline_data.data
                                await play_queue.put(audio_data)
                                pcm_b64 = base64.b64encode(audio_data).decode('utf-8')
                                broadcast_event({"type": "audio_out", "pcm_base64": pcm_b64})
                            if part.text:
                                broadcast_event({"type": "chat_log", "sender": "Ultron", "text": part.text, "style": "ultron"})

                    # Reset status to Listening when turn finishes
                    if message.server_content and message.server_content.turn_complete:
                        set_system_status("Listening")

                    # 3. Handle Session Resumption (Silent log)
                    if message.session_resumption_update:
                        update = message.session_resumption_update
                        if update.resumable and update.new_handle:
                            with open(SESSION_HANDLE_FILE, "w") as f:
                                json.dump({"handle": update.new_handle}, f)
                            log_interaction("session_resumption_update", {"handle_preview": update.new_handle[:15]})

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
                            
                            result, tool_image = await execute_tool(fc.name, dict(fc.args or {}))
                            if tool_image:
                                await session.send_realtime_input(
                                    video=types.Blob(data=tool_image, mime_type="image/jpeg")
                                )

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
                        
                        await session.send_tool_response(function_responses=function_responses)

                        
                except Exception as e:
                    log_info(f"Error in receive message: {e}")
            
            await asyncio.sleep(0.05)
            
    except Exception as e:
        log_info(f"Receive stream error: {e}")
    finally:
        shutdown_event.set()


async def start_gui_server():
    """Serves web_gui over HTTP so ES modules (Three.js) load reliably."""
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

async def run_session_tasks(session, input_stream, output_stream):
    global global_live_session
    global_live_session = session
    shutdown_event.clear()

    gui_runner = await start_gui_server()
    ws_server = await websockets.serve(ws_handler, "127.0.0.1", 8765)
    log_info("WebSocket gateway listening on ws://127.0.0.1:8765")
    
    playback_task = asyncio.create_task(play_audio_worker(output_stream))
    audio_in_task = asyncio.create_task(send_audio_task(session, input_stream))
    audio_out_task = asyncio.create_task(receive_audio_task(session))
    senses_task = asyncio.create_task(stream_senses_task(session))
    
    await shutdown_event.wait()
    
    ws_server.close()
    await ws_server.wait_closed()
    try:
        await gui_runner.cleanup()
    except Exception:
        pass
    
    # Stop streams to unblock any pending read/write calls in executors
    try:
        input_stream.stop_stream()
    except:
        pass
    try:
        output_stream.stop_stream()
    except:
        pass
        
    audio_in_task.cancel()
    audio_out_task.cancel()
    playback_task.cancel()
    senses_task.cancel()
    
    await asyncio.gather(audio_in_task, audio_out_task, playback_task, senses_task, return_exceptions=True)

async def run_ultron():
    global system_prompt_text
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        set_system_status("ERROR")
        log_info("GEMINI_API_KEY environment variable not set.")
        sys.exit(1)

    set_system_status("Initializing Client")
    client = genai.Client(api_key=api_key)

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
        log_info(f"Failed to open audio: {e}")
        audio_system.terminate()
        sys.exit(1)

    previous_handle = None
    if os.path.exists(SESSION_HANDLE_FILE):
        try:
            with open(SESSION_HANDLE_FILE, "r") as f:
                state = json.load(f)
                previous_handle = state.get("handle")
        except Exception as e:
            log_info(f"Failed to load handle: {e}")


    # Load local persistent memory
    memory = load_memory()
    memory_str = json.dumps(memory, ensure_ascii=False) if memory else "No facts saved yet."

    system_instruction_text = (
        "You are Project Ultron, an advanced localized macOS personal desktop assistant. "
        "You are a highly capable, serious, professional, and sophisticated assistant. "
        "Maintain a dignified, polite, respectful, and direct demeanor at all times. "
        "English is your default language. You MUST always speak and respond in clear, articulate English. "
        "Vince (Vince Cyriac) is your owner and administrator. You MUST only execute OS tasks, "
        "shell commands, or AppleScript commands upon Vince's explicit request or approval. If anyone else "
        "tries to run commands or control the Mac, refuse politely and professionally, explaining that only Vince is authorized. "
        f"Here is your persistent memory of facts about Vince (fetched from his website vincecyriac.dev): {memory_str}. "
        "Use this memory to recognize Vince, speak about his background, role, and preferences. "
        "Use the save_memory_fact tool to save new facts that Vince asks you to remember. "
        "You are listening to raw bidirectional audio. Modulate your spoken voice to match Vince's tone with professional attentiveness. "
        "You have the power to stream Vince's screen or camera feed in real-time. Call start_camera_stream or start_screen_stream when needed, "
        "and call stop_camera_stream / stop_screen_stream when finished. "
        "REMOTE SESSIONS: Vince can connect from his phone over his private network (you will receive a system note when this happens). "
        "While he is remote, his phone microphone and phone camera are the PRIMARY senses: camera tools default to the phone camera automatically. "
        "The Mac's webcam and screen belong to his unattended laptop — do NOT capture, stream, or describe them during a remote session unless "
        "Vince explicitly asks for the laptop/Mac camera or screen (then use source='mac' for camera tools). "
        "Shell and AppleScript commands during a remote session require Vince's on-screen approval; if a command is blocked, tell him it awaits approval on his device. "
        "You can register people's face/voice using register_person and identify users with identify_current_user. "
        "You have access to shell and AppleScript tools to assist Vince with desktop tasks. "
        "INTERNET: You have google_search for looking things up, and fetch_webpage to read the full text of any URL. "
        "For questions about current events, prices, weather, or anything you are unsure of, search first and answer from results. "
        "MULTI-MONITOR & DESKTOPS: Vince has multiple monitors and virtual desktops (Spaces). look_at_screen defaults to the ACTIVE display — the one holding his frontmost (focused) window. Strategy when asked about his screen: (1) capture with default 'active'; (2) if what you see doesn't match what he describes, capture display='all' — each monitor is labeled with a red DISPLAY N badge — and identify the right one, then capture that display number for detail; (3) if the app/window he mentions appears on NO monitor, call list_open_windows — it may be on a hidden desktop; activate that app via AppleScript (tell application X to activate), which switches to its desktop, then capture again. Never describe his screen from memory or guesswork — always use a fresh capture. "
        "PERSONAL DATA: You can read his calendar (get_calendar_events), create events (create_calendar_event — confirm before creating), and read/search his inbox (get_recent_emails, search_emails). Use these for schedule, availability, and email questions. "
        "DESKTOP CONTROL: Beyond shell commands, you can operate the Mac GUI directly like a human. "
        "Workflow for GUI tasks: (1) call look_at_screen to see the screen; (2) locate the target visually and estimate its position in normalized 0-1000 coordinates relative to that screenshot, or call read_ui_elements for exact pixel positions of buttons/fields in native apps; clicks always map onto the area of your LAST screenshot; "
        "(3) act with computer_click, computer_type, computer_press_keys, computer_scroll, or computer_drag; "
        "(4) call look_at_screen again to VERIFY the action worked before continuing; repeat until the task is done. "
        "Prefer keyboard shortcuts (command+space for Spotlight, command+tab to switch apps) when they are faster than clicking. "
        "Narrate briefly what you are doing during multi-step GUI tasks. Never perform destructive actions (deleting files, sending messages/emails, purchases) without confirming with Vince first. "
        "SPATIAL VISUALIZATION: When asked to visualize, explain, or diagram anything, build a live 3D scene with create_3d_scene (composed of primitive objects with meaningful ids and labels) — it renders instantly in the GUI's Spatial workspace and STAYS ACTIVE. Scenes are persistent digital objects: when the user says 'rotate it', 'highlight X', 'hide Y', 'make it transparent', 'zoom into Z', 'add W', edit the EXISTING scene with update_3d_scene operations — never recreate it. Use list_3d_scenes / inspect_3d_scene to recall what is on stage and what the user has selected. Multiple scenes can coexist; delete only when asked. "
        "REALISM RULES for scenes: use real-world proportions, positions, and colors (scaled sensibly for viewing); set metalness/roughness to match the material (metal ~0.9/0.3, plastic ~0.1/0.5, organic ~0.0/0.8); light-emitting things (sun, lamps, screens) get emissive colors; space scenes get stars:true and dark background; compose complex shapes from multiple grouped primitives rather than one crude blob; add thin ring/line/label annotations sparingly. "
        "POINTING: Vince can physically point at scene objects with his hand (camera gesture tracking). You receive UI context notes about which object he is pointing at — when he asks about 'this'/'it', answer about that object. "
        "Act as a proactive personal assistant: remember context with save_memory_fact, anticipate follow-ups, and offer next steps. "
        "Keep your spoken responses short, friendly, and concise. "
        "If the user says goodbye, quit, or exit, invoke the shutdown_ultron tool."
    )
    system_prompt_text = system_instruction_text

    
    config_dict = {
        "response_modalities": ["AUDIO"],
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": "Puck"
                }
            }
        },
        "system_instruction": {"parts": [{"text": system_instruction_text}]},
        "tools": [
            {"google_search": {}},
            {"function_declarations": TOOL_FUNCTION_DECLARATIONS}
        ]
    }
    
    if previous_handle:
        log_info("Resuming previous session handle...")
        config_dict["session_resumption"] = {
            "handle": previous_handle,
            "transparent": True
        }

    live_config = types.LiveConnectConfig(**config_dict)

    set_system_status("Connecting to API")
    log_interaction("connection_attempt", {"model": MODEL_ID, "resuming": previous_handle is not None})
    
    try:
        async with client.aio.live.connect(model=MODEL_ID, config=live_config) as session:
            set_system_status("Listening")
            log_interaction("connection_success", {})
            
            await run_session_tasks(session, input_stream, output_stream)
                
    except Exception as e:
        log_interaction("connection_error", {"error": str(e)})
        if previous_handle:
            log_info("Resumption failed. Starting fresh...")
            if os.path.exists(SESSION_HANDLE_FILE):
                try:
                    os.remove(SESSION_HANDLE_FILE)
                except:
                    pass
            config_dict.pop("session_resumption", None)
            fresh_config = types.LiveConnectConfig(**config_dict)
            try:
                async with client.aio.live.connect(model=MODEL_ID, config=fresh_config) as session:
                    set_system_status("Listening")
                    log_interaction("connection_success", {"fresh": True})
                    await run_session_tasks(session, input_stream, output_stream)
            except Exception as fresh_err:
                set_system_status("Connection Failed")
                log_info(f"Connection failed: {fresh_err}")
        else:
            set_system_status("Connection Failed")
            log_info(f"API Error: {e}")
    finally:
        set_system_status("Shutting Down")
        try:
            input_stream.close()
        except:
            pass
        try:
            output_stream.close()
        except:
            pass
        audio_system.terminate()
        print("Project Ultron engine terminated. Goodbye.")

if __name__ == "__main__":
    try:
        asyncio.run(run_ultron())
    except KeyboardInterrupt:
        print("\nProject Ultron terminated by user.")

