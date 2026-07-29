import os
os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
import sys
import json
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
global_live_session = None

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


async def ws_handler(websocket):
    global camera_stream_active, screen_stream_active, model_is_speaking, mic_audio_buffer
    connected_ws_clients.add(websocket)
    await websocket.send(json.dumps({
        "type": "sense_update",
        "camera_active": camera_stream_active,
        "screen_active": screen_stream_active
    }))
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_type = data.get("type")
                if msg_type == "audio_in":
                    pcm_b64 = data.get("pcm_base64")
                    if pcm_b64 and global_live_session:
                        pcm_bytes = base64.b64decode(pcm_b64)
                        mic_audio_buffer.extend(pcm_bytes)
                        if len(mic_audio_buffer) > MAX_BUFFER_SIZE:
                            mic_audio_buffer = mic_audio_buffer[-MAX_BUFFER_SIZE:]
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
        connected_ws_clients.remove(websocket)


def _current_webcam_frame() -> bytes:
    """Best available webcam frame: live stream frame if streaming, else one-shot capture."""
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
            log_info(f"Error reading mic: {e}")
            await asyncio.sleep(0.1)

async def stream_senses_task(session):
    """Continuously captures and streams the user's screen and webcam frames to the Live session in the background when enabled by the AI."""
    global active_webcam, latest_webcam_frame_bytes
    webcam = sentry_vision.PersistentWebcam()
    active_webcam = webcam
    try:
        while not shutdown_event.is_set():
            try:
                if screen_stream_active:
                    screen_bytes = sentry_vision.capture_screen()
                    if screen_bytes:
                        b64 = base64.b64encode(screen_bytes).decode('utf-8')
                        broadcast_event({"type": "screen_frame", "image_base64": b64})
                        await session.send_realtime_input(
                            video=types.Blob(data=screen_bytes, mime_type="image/jpeg")
                        )
                    await asyncio.sleep(0.8)
                
                if camera_stream_active:
                    webcam.start()
                    webcam_bytes = webcam.read_frame()
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
                log_interaction("sense_stream_error", {"error": str(e)})
                await asyncio.sleep(2.0)
    finally:
        webcam.stop()
        active_webcam = None
        latest_webcam_frame_bytes = None

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
                            
                            result = ""
                            if fc.name == "execute_shell_command":
                                cmd = fc.args.get("command")
                                result = sentry_exec.execute_shell(cmd)
                            elif fc.name == "execute_applescript_task":
                                script = fc.args.get("script")
                                result = sentry_exec.execute_applescript(script)
                            elif fc.name == "look_at_screen":
                                display_sel = fc.args.get("display", "active")
                                screen_bytes = await asyncio.get_running_loop().run_in_executor(
                                    None, sentry_vision.capture_screen, display_sel
                                )
                                if screen_bytes:
                                    await session.send_realtime_input(
                                        video=types.Blob(data=screen_bytes, mime_type="image/jpeg")
                                    )
                                    result = (
                                        f"Screen captured (mode: {display_sel}) and loaded into your visual sensor. "
                                        f"{sentry_vision.describe_displays()} "
                                        "Click coordinates (0-1000) now map onto exactly this captured area. "
                                        "Tell the user what is on the screen."
                                    )
                                else:
                                    result = "Error: Failed to capture screen. Verify Screen Recording permissions."
                            elif fc.name == "look_at_webcam":
                                webcam_bytes = sentry_vision.capture_webcam()
                                if webcam_bytes:
                                    await session.send_realtime_input(
                                        video=types.Blob(data=webcam_bytes, mime_type="image/jpeg")
                                    )
                                    result = "Webcam frame captured successfully and loaded into your visual sensor. Tell the user what you see."
                                else:
                                    result = "Error: Failed to capture webcam. Verify camera access permissions."
                            elif fc.name == "start_camera_stream":
                                camera_stream_active = True
                                broadcast_event({"type": "sense_update", "camera_active": True, "screen_active": screen_stream_active})
                                reason = fc.args.get("reason", "")
                                result = f"Webcam continuous streaming started. Reason: '{reason}'."
                            elif fc.name == "stop_camera_stream":
                                camera_stream_active = False
                                broadcast_event({"type": "sense_update", "camera_active": False, "screen_active": screen_stream_active})
                                result = "Webcam continuous streaming stopped."
                            elif fc.name == "start_screen_stream":
                                screen_stream_active = True
                                broadcast_event({"type": "sense_update", "camera_active": camera_stream_active, "screen_active": True})
                                reason = fc.args.get("reason", "")
                                result = f"Screen continuous capture started. Reason: '{reason}'."
                            elif fc.name == "stop_screen_stream":
                                screen_stream_active = False
                                broadcast_event({"type": "sense_update", "camera_active": camera_stream_active, "screen_active": False})
                                result = "Screen continuous capture stopped."
                            elif fc.name == "register_person":
                                name = fc.args.get("name")
                                result = register_person(name)
                            elif fc.name == "identify_current_user":
                                result = identify_current_user()
                            elif fc.name == "save_memory_fact":
                                key = fc.args.get("key")
                                val = fc.args.get("value")
                                memory = load_memory()
                                memory[key] = val
                                save_memory(memory)
                                result = f"Fact saved: '{key}' is now remembered as '{val}'."
                            elif fc.name == "retrieve_memory_facts":
                                memory = load_memory()
                                result = json.dumps(memory, ensure_ascii=False)
                            elif fc.name == "visualize_concept":
                                concept_name = fc.args.get("concept_name")
                                html_content = fc.args.get("html_content")
                                try:
                                    filename = "ultron_visualization.html"
                                    with open(filename, "w", encoding="utf-8") as f:
                                        f.write(html_content)
                                    broadcast_event({
                                        "type": "visualization",
                                        "concept_name": concept_name,
                                        "html_content": html_content
                                    })
                                    result = f"Successfully generated HTML visualization for '{concept_name}' and rendered inside the Desktop App GUI."
                                except Exception as e:
                                    result = f"Error generating HTML visualization: {str(e)}"
                            elif fc.name == "fetch_webpage":
                                url = fc.args.get("url")
                                result = await sentry_web.fetch_webpage(url)
                            elif fc.name == "computer_click":
                                result = await asyncio.get_running_loop().run_in_executor(
                                    None, sentry_action.click,
                                    int(fc.args.get("x", 500)), int(fc.args.get("y", 500)),
                                    fc.args.get("button", "left"), int(fc.args.get("clicks", 1))
                                )
                            elif fc.name == "computer_type":
                                result = await asyncio.get_running_loop().run_in_executor(
                                    None, sentry_action.type_text,
                                    fc.args.get("text", ""), bool(fc.args.get("press_enter", False))
                                )
                            elif fc.name == "computer_press_keys":
                                result = await asyncio.get_running_loop().run_in_executor(
                                    None, sentry_action.press_keys, list(fc.args.get("keys", []))
                                )
                            elif fc.name == "computer_scroll":
                                x = fc.args.get("x")
                                y = fc.args.get("y")
                                result = await asyncio.get_running_loop().run_in_executor(
                                    None, sentry_action.scroll,
                                    int(fc.args.get("amount", -5)),
                                    int(x) if x is not None else None,
                                    int(y) if y is not None else None
                                )
                            elif fc.name == "computer_drag":
                                result = await asyncio.get_running_loop().run_in_executor(
                                    None, sentry_action.drag,
                                    int(fc.args.get("x1", 0)), int(fc.args.get("y1", 0)),
                                    int(fc.args.get("x2", 0)), int(fc.args.get("y2", 0))
                                )
                            elif fc.name == "read_ui_elements":
                                result = await asyncio.get_running_loop().run_in_executor(
                                    None, sentry_action.read_ui_elements
                                )
                            elif fc.name == "shutdown_ultron":
                                shutdown_event.set()
                                result = "Shutting down the Project Ultron system. Goodbye!"
                            else:
                                result = f"Unknown function: {fc.name}"
                                
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


async def run_session_tasks(session, input_stream, output_stream):
    global global_live_session
    global_live_session = session
    shutdown_event.clear()
    
    ws_server = await websockets.serve(ws_handler, "127.0.0.1", 8765)
    log_info("WebSocket gateway listening on ws://127.0.0.1:8765")
    
    playback_task = asyncio.create_task(play_audio_worker(output_stream))
    audio_in_task = asyncio.create_task(send_audio_task(session, input_stream))
    audio_out_task = asyncio.create_task(receive_audio_task(session))
    senses_task = asyncio.create_task(stream_senses_task(session))
    
    await shutdown_event.wait()
    
    ws_server.close()
    await ws_server.wait_closed()
    
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
        "You can register people's face/voice using register_person and identify users with identify_current_user. "
        "You have access to shell and AppleScript tools to assist Vince with desktop tasks. "
        "INTERNET: You have google_search for looking things up, and fetch_webpage to read the full text of any URL. "
        "For questions about current events, prices, weather, or anything you are unsure of, search first and answer from results. "
        "MULTI-MONITOR: Vince has multiple monitors and desktops (Spaces). look_at_screen defaults to the ACTIVE display (where his mouse is) — this is usually the right one. If what you see doesn't match what he's describing, call look_at_screen with display='all' to see everything, then focus the right display number. Never claim to see his screen without a fresh capture; if unsure, capture again rather than guessing. "
        "DESKTOP CONTROL: Beyond shell commands, you can operate the Mac GUI directly like a human. "
        "Workflow for GUI tasks: (1) call look_at_screen to see the screen; (2) locate the target visually and estimate its position in normalized 0-1000 coordinates relative to that screenshot, or call read_ui_elements for exact pixel positions of buttons/fields in native apps; clicks always map onto the area of your LAST screenshot; "
        "(3) act with computer_click, computer_type, computer_press_keys, computer_scroll, or computer_drag; "
        "(4) call look_at_screen again to VERIFY the action worked before continuing; repeat until the task is done. "
        "Prefer keyboard shortcuts (command+space for Spotlight, command+tab to switch apps) when they are faster than clicking. "
        "Narrate briefly what you are doing during multi-step GUI tasks. Never perform destructive actions (deleting files, sending messages/emails, purchases) without confirming with Vince first. "
        "When asked to explain complex concepts or visualize diagrams/charts, use the visualize_concept tool to generate interactive HTML content. "
        "Act as a proactive personal assistant: remember context with save_memory_fact, anticipate follow-ups, and offer next steps. "
        "Keep your spoken responses short, friendly, and concise. "
        "If the user says goodbye, quit, or exit, invoke the shutdown_ultron tool."
    )

    
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
            {
                "function_declarations": [
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
                        "description": "Capture a single frame from the user's default webcam and load it into your visual sensor. Use this when the user asks you to look at them, check the camera feed, or see their physical surroundings.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "start_camera_stream",
                        "description": "Start continuous real-time streaming of the webcam feed. Use this when you decide you need to watch the user, check their movements, recognize their face, or see what they are doing in real-time.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "reason": {
                                    "type": "STRING",
                                    "description": "The reason why you need to enable the camera feed."
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
                        "name": "visualize_concept",
                        "description": "Visualize a concept by generating an interactive, beautiful HTML/CSS/JS page containing diagrams and explanatory graphics, and opening it in the user's default browser.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "concept_name": {
                                    "type": "STRING",
                                    "description": "The title or name of the concept (e.g. 'How Neural Networks Work')."
                                },
                                "html_content": {
                                    "type": "STRING",
                                    "description": "The complete, standalone HTML source code with inline CSS/JS containing visual diagrams, flowcharts, SVGs, or interactive elements."
                                }
                            },
                            "required": ["concept_name", "html_content"]
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
                        "name": "shutdown_ultron",
                        "description": "Gracefully shut down the Project Ultron assistant and exit the program. Use this when the user says goodbye, quit, exit, or asks you to turn off.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    }
                ]
            }
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

