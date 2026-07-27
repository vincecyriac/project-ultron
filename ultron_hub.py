import os
import sys
import json
import asyncio
import contextlib
import datetime
try:
    import psutil
except ImportError:
    psutil = None

@contextlib.contextmanager
def suppress_c_stdout_stderr():
    """A context manager that redirects standard output and standard error at the OS level to suppress C-level warnings."""
    try:
        null_fd = os.open(os.devnull, os.O_RDWR)
        save_stdout = os.dup(1)
        save_stderr = os.dup(2)
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
        yield
    except Exception:
        yield
    finally:
        try:
            os.dup2(save_stdout, 1)
            os.dup2(save_stderr, 2)
            os.close(save_stdout)
            os.close(save_stderr)
            os.close(null_fd)
        except:
            pass

os.environ["OPENCV_LOG_LEVEL"] = "ERROR"
with suppress_c_stdout_stderr():
    import pyaudio
    import cv2
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except:
        pass
import numpy as np
import unicodedata
import ssl
import threading
import websockets
from Foundation import NSObject, NSURL
from AppKit import (
    NSApplication, NSWindow, NSBackingStoreBuffered,
    NSRect, NSPoint, NSSize, NSTitledWindowMask, NSClosableWindowMask,
    NSMiniaturizableWindowMask, NSResizableWindowMask, NSColor,
    NSVisualEffectView, NSVisualEffectMaterialDark, NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive, NSLayoutConstraint
)
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Import capability submodules
import sentry_vision
import sentry_exec

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
CURRENT_MODEL_ID = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
SUPPORTED_MODELS = [
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-live-preview",
    "gemini-2.0-flash-exp",
    "gemini-2.0-flash-realtime-exp"
]

def switch_model_session(new_model: str) -> str:
    global CURRENT_MODEL_ID, model_switch_event
    if new_model not in SUPPORTED_MODELS:
        SUPPORTED_MODELS.append(new_model)
    CURRENT_MODEL_ID = new_model
    if gui:
        gui.add_log(f"Model switch requested -> {new_model}")
        gui.broadcast({"type": "model_update", "value": new_model, "models": SUPPORTED_MODELS})
    model_switch_event.set()
    return f"Model successfully switched to '{new_model}'. Reconnecting live session..."

# Voice configuration
CURRENT_VOICE = "Puck"
SUPPORTED_VOICES = ["Puck", "Charon", "Kore", "Fenrir", "Aoede"]

mic_muted = False

def switch_voice_session(new_voice: str) -> str:
    global CURRENT_VOICE, model_switch_event
    if new_voice not in SUPPORTED_VOICES:
        SUPPORTED_VOICES.append(new_voice)
    CURRENT_VOICE = new_voice
    if gui:
        gui.add_log(f"Voice switch requested -> {new_voice}")
        gui.broadcast({"type": "voice_update", "value": new_voice, "voices": SUPPORTED_VOICES})
    model_switch_event.set()
    return f"Voice successfully switched to '{new_voice}'. Reconnecting live session..."

def toggle_mic_mute() -> bool:
    global mic_muted
    mic_muted = not mic_muted
    if gui:
        gui.add_log(f"Microphone {'MUTED' if mic_muted else 'UNMUTED'}")
        gui.broadcast({"type": "mute_update", "value": mic_muted})
    return mic_muted

def export_session_history(format_type: str = "markdown") -> str:
    """Export ultron_history.jsonl into a formatted Markdown report ultron_history_export.md."""
    if not os.path.exists(HISTORY_LOG_FILE):
        return "No history log file found."
    
    export_filename = "ultron_history_export.md"
    try:
        entries = []
        with open(HISTORY_LOG_FILE, "r") as f:
            for line in f:
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except:
                        pass
        
        md_lines = [
            "# Project Ultron Session History Export\n",
            f"**Export Time**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"**Total Events**: {len(entries)}\n\n---\n"
        ]
        for idx, entry in enumerate(entries, 1):
            event_type = entry.get("type", "event")
            timestamp = entry.get("timestamp", "")
            data = entry.get("data", {})
            md_lines.append(f"### {idx}. [{timestamp}] `{event_type}`\n")
            md_lines.append(f"```json\n{json.dumps(data, indent=2)}\n```\n\n")
            
        with open(export_filename, "w") as ef:
            ef.write("\n".join(md_lines))
            
        if gui:
            gui.add_log(f"Exported session history to {export_filename}")
        return f"Successfully exported {len(entries)} events to '{export_filename}'."
    except Exception as e:
        return f"Error exporting session history: {e}"

def delete_memory_fact(key: str) -> str:
    """Delete a memory fact by key from ultron_memory.json."""
    memory = load_memory()
    if key in memory:
        del memory[key]
        save_memory(memory)
        if gui:
            gui.broadcast({"type": "memory_update", "value": memory})
        return f"Fact '{key}' deleted from memory."
    return f"Key '{key}' not found in memory."


# Audio configuration
FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK_SIZE = 1024

def get_visual_width(s: str) -> int:
    """Calculates the visual column width of a string, accounting for wide characters and emojis."""
    width = 0
    for char in s:
        east_asian = unicodedata.east_asian_width(char)
        if east_asian in ('W', 'F'):
            width += 2
        elif char in "🤖🎤🖥️📷":
            width += 2
        else:
            width += 1
    return width

def visual_ljust(text: str, width: int) -> str:
    """Pads a string to a visual column width, taking wide characters into account."""
    vis_w = get_visual_width(text)
    if vis_w >= width:
        res = ""
        curr_w = 0
        for char in text:
            char_w = 2 if unicodedata.east_asian_width(char) in ('W', 'F') or char in "🤖🎤🖥️📷" else 1
            if curr_w + char_w > width:
                break
            res += char
            curr_w += char_w
        return res + " " * (width - curr_w)
    else:
        return text + " " * (width - vis_w)



ui_clients = set()
ws_input_queue = asyncio.Queue()

async def ws_handler(websocket):
    global ui_clients, ws_input_queue
    ui_clients.add(websocket)
    try:
        if gui:
            await websocket.send(json.dumps({"type": "status", "value": gui.status}))
            for log in gui.logs:
                await websocket.send(json.dumps({"type": "log", "value": log}))
            await websocket.send(json.dumps({
                "type": "senses",
                "screen": "STREAMING" if screen_stream_active else "READY",
                "webcam": "STREAMING" if camera_stream_active else "READY"
            }))
            await websocket.send(json.dumps({
                "type": "model_update",
                "value": CURRENT_MODEL_ID,
                "models": SUPPORTED_MODELS
            }))
            await websocket.send(json.dumps({
                "type": "voice_update",
                "value": CURRENT_VOICE,
                "voices": SUPPORTED_VOICES
            }))
            await websocket.send(json.dumps({
                "type": "mute_update",
                "value": mic_muted
            }))
            await websocket.send(json.dumps({
                "type": "memory_update",
                "value": load_memory()
            }))
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "input":
                    val = data.get("value")
                    await ws_input_queue.put(val)
                elif data.get("type") == "switch_model":
                    new_model = data.get("value")
                    if new_model:
                        switch_model_session(new_model)
                elif data.get("type") == "switch_voice":
                    new_voice = data.get("value")
                    if new_voice:
                        switch_voice_session(new_voice)
                elif data.get("type") == "toggle_mute":
                    toggle_mic_mute()
                elif data.get("type") == "export_history":
                    export_session_history()
                elif data.get("type") == "delete_memory":
                    key = data.get("key")
                    if key:
                        delete_memory_fact(key)
            except Exception as e:
                if gui:
                    gui.add_log(f"WS message error: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        ui_clients.remove(websocket)

class UltronDesktopGUI:
    def __init__(self):
        self.status = "Initializing"
        self.logs = []

    def set_status(self, status):
        self.status = status
        self.broadcast({"type": "status", "value": status})

    def add_log(self, log_msg):
        self.logs.append(log_msg)
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]
        self.broadcast({"type": "log", "value": log_msg})

    def tick_wave(self):
        pass

    def broadcast(self, data):
        global event_loop
        if event_loop is None or not event_loop.is_running():
            return
        async def do_broadcast():
            if ui_clients:
                msg = json.dumps(data)
                await asyncio.gather(*[client.send(msg) for client in ui_clients], return_exceptions=True)
        asyncio.run_coroutine_threadsafe(do_broadcast(), event_loop)

# Global states
play_queue = asyncio.Queue()
interrupted_event = asyncio.Event()
shutdown_event = asyncio.Event()
model_switch_event = asyncio.Event()
gui = None
camera_stream_active = True
screen_stream_active = False
active_webcam = None
latest_webcam_frame_bytes = None
input_buffer = ""
mic_audio_buffer = bytearray()
MAX_BUFFER_SIZE = 96000  # 3 seconds of 16kHz 16-bit PCM
model_is_speaking = False
current_speaker_rms = 0.0

PROFILES_FILE = "ultron_profiles.json"
YUNET_MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_MODEL_PATH = "face_detection_yunet.onnx"

def download_yunet():
    # If the file is smaller than 10KB, it's likely a Git LFS pointer text and not the binary
    if os.path.exists(YUNET_MODEL_PATH) and os.path.getsize(YUNET_MODEL_PATH) < 10000:
        try:
            os.remove(YUNET_MODEL_PATH)
        except:
            pass
            
    if not os.path.exists(YUNET_MODEL_PATH):
        try:
            import urllib.request
            if gui:
                gui.add_log("Downloading face detection model (YuNet)...")
            urllib.request.urlretrieve(YUNET_MODEL_URL, YUNET_MODEL_PATH)
            if gui:
                gui.add_log("Face detection model loaded successfully.")
        except Exception as e:
            if gui:
                gui.add_log(f"Model load failed: {e}")

def load_profiles() -> dict:
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            if gui:
                gui.add_log(f"Profiles read error: {e}")
    return {}

def save_profiles(profiles: dict):
    try:
        with open(PROFILES_FILE, "w") as f:
            json.dump(profiles, f, indent=2)
    except Exception as e:
        if gui:
            gui.add_log(f"Profiles write error: {e}")

def get_multiple_face_signatures() -> list:
    """Detects all faces in the frame and returns a list of (face_signature, bounding_box) tuples."""
    global active_webcam
    try:
        download_yunet()
        webcam_bytes = None
        if active_webcam and camera_stream_active:
            webcam_bytes = active_webcam.read_frame()
        else:
            webcam_bytes = sentry_vision.capture_webcam()
            
        if not webcam_bytes:
            return []
            
        nparr = np.frombuffer(webcam_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return []
            
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        signatures = []
        
        if os.path.exists(YUNET_MODEL_PATH):
            try:
                detector = cv2.FaceDetectorYN.create(YUNET_MODEL_PATH, "", (w, h))
                _, faces = detector.detect(img)
                if faces is not None:
                    for face in faces:
                        fx, fy, fw, fh = int(face[0]), int(face[1]), int(face[2]), int(face[3])
                        fx = max(0, fx)
                        fy = max(0, fy)
                        fw = min(w - fx, fw)
                        fh = min(h - fy, fh)
                        if fw > 10 and fh > 10:
                            face_crop = gray[fy:fy+fh, fx:fx+fw]
                            resized = cv2.resize(face_crop, (64, 64))
                            equalized = cv2.equalizeHist(resized)
                            flat = equalized.flatten().astype(np.float32)
                            flat -= np.mean(flat)
                            norm = np.linalg.norm(flat)
                            if norm > 1e-6:
                                flat /= norm
                            signatures.append((flat.tolist(), (fx, fy, fw, fh)))
                    return signatures
            except Exception as e:
                if gui:
                    gui.add_log(f"YuNet detection error: {e}")
                    
        # Fallback to center crop
        cy, cx = h // 2, w // 2
        dy, dx = int(h * 0.3), int(w * 0.3)
        face_crop = gray[cy-dy:cy+dy, cx-dx:cx+dx]
        resized = cv2.resize(face_crop, (64, 64))
        equalized = cv2.equalizeHist(resized)
        flat = equalized.flatten().astype(np.float32)
        flat -= np.mean(flat)
        norm = np.linalg.norm(flat)
        if norm > 1e-6:
            flat /= norm
        signatures.append((flat.tolist(), (cx-dx, cy-dy, dx*2, dy*2)))
        return signatures
    except Exception as e:
        if gui:
            gui.add_log(f"Face extraction error: {e}")
        return []

def get_face_signature() -> list:
    """Compatibility helper returning the first detected face signature."""
    sigs = get_multiple_face_signatures()
    return sigs[0][0] if sigs else None

def extract_audio_signature(audio_data: bytes) -> list:
    """Extracts a frequency-domain voice signature from raw 16kHz PCM audio bytes."""
    try:
        samples = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < 512:
            return None
            
        n_fft = 512
        hop_length = 256
        
        stft = []
        for i in range(0, len(samples) - n_fft, hop_length):
            chunk = samples[i:i+n_fft]
            windowed = chunk * np.hanning(n_fft)
            fft_res = np.abs(np.fft.rfft(windowed))
            stft.append(fft_res)
            
        if not stft:
            return None
            
        spectrogram = np.array(stft)
        mean_spectrum = np.mean(spectrogram, axis=0)
        
        bin_count = len(mean_spectrum)
        mel_bands = 20
        mel_indices = np.linspace(0, bin_count - 1, mel_bands + 2, dtype=np.int32)
        
        features = []
        for i in range(mel_bands):
            start = mel_indices[i]
            end = mel_indices[i+2]
            band_energy = np.sum(mean_spectrum[start:end])
            features.append(band_energy)
            
        features = np.array(features, dtype=np.float32)
        features = np.log(features + 1e-6)
        features -= np.mean(features)
        norm = np.linalg.norm(features)
        if norm > 1e-6:
            features /= norm
            
        return features.tolist()
    except Exception as e:
        if gui:
            gui.add_log(f"Voice sig extract error: {e}")
        return None

def extract_multiple_voice_signatures(audio_data: bytes) -> list:
    """Divides audio into overlapping 1.5s windows to extract multiple speaker voice prints."""
    if len(audio_data) < 16000:
        return []
    window_size = 48000  # 1.5 seconds in bytes (16kHz 16-bit PCM)
    hop_size = 24000     # 0.75 seconds overlap
    
    signatures = []
    if len(audio_data) < window_size:
        sig = extract_audio_signature(audio_data)
        if sig:
            signatures.append(sig)
        return signatures
        
    for i in range(0, len(audio_data) - window_size + 1, hop_size):
        window_bytes = audio_data[i : i + window_size]
        sig = extract_audio_signature(window_bytes)
        if sig:
            signatures.append(sig)
    return signatures

def register_person(name: str) -> str:
    global mic_audio_buffer
    try:
        audio_bytes = bytes(mic_audio_buffer)
        voice_sig = extract_audio_signature(audio_bytes)
        if not voice_sig:
            return "Error: No voice audio captured. Please speak something first."
            
        face_sigs = get_multiple_face_signatures()
        face_sig = face_sigs[0][0] if face_sigs else None
        
        profiles = load_profiles()
        profiles[name] = {
            "voice_signature": voice_sig,
            "face_signature": face_sig
        }
        save_profiles(profiles)
        
        status_str = f"Successfully registered '{name}'."
        if face_sig:
            status_str += " Captured both face and voice prints."
        else:
            status_str += " Captured voice print (no face detected; look at the camera to register face)."
        return status_str
    except Exception as e:
        return f"Registration error: {e}"

def identify_current_user() -> str:
    global mic_audio_buffer
    try:
        profiles = load_profiles()
        if not profiles:
            return "No registered profiles found in the database. Please register someone first."
            
        # 1. Identify voices
        audio_bytes = bytes(mic_audio_buffer)
        voice_sigs = extract_multiple_voice_signatures(audio_bytes)
        
        identified_voices = set()
        for voice_sig in voice_sigs:
            curr_voice_arr = np.array(voice_sig)
            best_match = None
            min_dist = float("inf")
            for name, sigs in profiles.items():
                db_voice_sig = sigs.get("voice_signature")
                if db_voice_sig:
                    db_voice_arr = np.array(db_voice_sig)
                    dist = np.linalg.norm(curr_voice_arr - db_voice_arr)
                    if dist < min_dist:
                        min_dist = dist
                        best_match = name
            if min_dist < 0.55:
                identified_voices.add((best_match, min_dist))

        # 2. Identify faces
        face_sigs_with_boxes = get_multiple_face_signatures()
        identified_faces = set()
        for face_sig, box in face_sigs_with_boxes:
            curr_face_arr = np.array(face_sig)
            best_match = None
            min_dist = float("inf")
            for name, sigs in profiles.items():
                db_face_sig = sigs.get("face_signature")
                if db_face_sig:
                    db_face_arr = np.array(db_face_sig)
                    dist = np.linalg.norm(curr_face_arr - db_face_arr)
                    if dist < min_dist:
                        min_dist = dist
                        best_match = name
            if min_dist < 0.55:
                identified_faces.add((best_match, min_dist))
                
        # 3. Combine results
        identified_people = {}
        for name, dist in identified_faces:
            identified_people[name] = {"face_match": True, "face_dist": dist, "voice_match": False, "voice_dist": 2.0}
            
        for name, dist in identified_voices:
            if name in identified_people:
                identified_people[name]["voice_match"] = True
                identified_people[name]["voice_dist"] = dist
            else:
                identified_people[name] = {"face_match": False, "face_dist": 2.0, "voice_match": True, "voice_dist": dist}
                
        if not identified_people:
            return "Unknown (No matches found in the database)"
            
        results = []
        for name, match in identified_people.items():
            if match["face_match"] and match["voice_match"]:
                combined_dist = 0.6 * match["voice_dist"] + 0.4 * match["face_dist"]
                conf = (1.0 - combined_dist / 2.0) * 100.0
                results.append(f"{name} (Face + Voice, Conf: {conf:.1f}%)")
            elif match["face_match"]:
                conf = (1.0 - match["face_dist"] / 2.0) * 100.0
                results.append(f"{name} (Face Only, Conf: {conf:.1f}%)")
            elif match["voice_match"]:
                conf = (1.0 - match["voice_dist"] / 2.0) * 100.0
                results.append(f"{name} (Voice Only, Conf: {conf:.1f}%)")
                
        return ", ".join(results)
    except Exception as e:
        return f"Identification error: {e}"

def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            if gui:
                gui.add_log(f"Memory read error: {e}")
    return {}

def save_memory(memory: dict):
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory, f, indent=2)
    except Exception as e:
        if gui:
            gui.add_log(f"Memory write error: {e}")

def write_source_file(path: str, content: str) -> str:
    """Create or overwrite a source file in the project workspace."""
    try:
        if path.endswith(".ipynb"):
            return "Error: Editing .ipynb files is not supported."
        parent_dir = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} characters to '{path}'."
    except Exception as e:
        return f"Error writing file: {e}"

def read_source_file(path: str) -> str:
    """Read a source file from the project workspace."""
    try:
        if not os.path.exists(path):
            return f"Error: File '{path}' does not exist."
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

async def execute_agent_command(command: str) -> str:
    """Asynchronously execute a terminal command in the project environment and return stdout/stderr."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode("utf-8", errors="ignore")
        error = stderr.decode("utf-8", errors="ignore")
        
        result = f"Command exited with code {proc.returncode}.\n"
        if output:
            result += f"--- STDOUT ---\n{output}\n"
        if error:
            result += f"--- STDERR ---\n{error}\n"
        return result
    except Exception as e:
        return f"Error executing command: {e}"

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
        if gui:
            gui.add_log(f"Failed to log interaction: {e}")

async def play_audio_worker(output_stream):
    global model_is_speaking, current_speaker_rms
    loop = asyncio.get_running_loop()
    while True:
        try:
            chunk = await play_queue.get()
            if interrupted_event.is_set():
                play_queue.task_done()
                continue
            
            model_is_speaking = True
            chunk_data = np.frombuffer(chunk, dtype=np.int16)
            current_speaker_rms = float(np.sqrt(np.mean(chunk_data.astype(np.float32)**2))) if len(chunk_data) > 0 else 0.0
            
            await loop.run_in_executor(None, output_stream.write, chunk)
            play_queue.task_done()
            if play_queue.empty():
                model_is_speaking = False
                current_speaker_rms = 0.0
        except asyncio.CancelledError:
            break
        except Exception as e:
            if gui:
                gui.add_log(f"Playback error: {e}")
            await asyncio.sleep(0.1)

async def send_audio_task(session, input_stream):
    global mic_audio_buffer, model_is_speaking, mic_muted
    loop = asyncio.get_running_loop()
    while True:
        try:
            data = await loop.run_in_executor(
                None,
                lambda: input_stream.read(CHUNK_SIZE, exception_on_overflow=False)
            )
            if data:
                if mic_muted:
                    data = b'\x00' * len(data)

                mic_audio_buffer.extend(data)
                if len(mic_audio_buffer) > MAX_BUFFER_SIZE:
                    mic_audio_buffer = mic_audio_buffer[-MAX_BUFFER_SIZE:]
                
                # Check for speaker feedback loop protection
                if model_is_speaking:
                    global current_speaker_rms
                    if current_speaker_rms > 100:
                        audio_data = np.frombuffer(data, dtype=np.int16)
                        rms = float(np.sqrt(np.mean(audio_data.astype(np.float32)**2))) if len(audio_data) > 0 else 0.0
                        if rms < 1500:
                            # Mute mic stream with silent bytes to prevent VAD self-interruption
                            data = b'\x00' * len(data)
                
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=data,
                        mime_type="audio/pcm;rate=16000"
                    )
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            if gui:
                gui.add_log(f"Error reading mic: {e}")
            await asyncio.sleep(0.1)

async def web_input_task(session):
    """Asynchronously reads text input from the WebSocket connection and forwards to the Gemini Live session."""
    global mic_audio_buffer, model_is_speaking
    try:
        while not shutdown_event.is_set():
            user_input = await ws_input_queue.get()
            ws_input_queue.task_done()
            
            if user_input:
                user_input = user_input.strip()
                if user_input.lower() in ('exit', 'quit', 'bye'):
                    shutdown_event.set()
                    break
                
                # Clear mic buffer on typed input
                mic_audio_buffer.clear()
                
                # Text barge-in
                if model_is_speaking:
                    if gui:
                        gui.set_status("Listening (Interrupted)")
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
                
                if gui:
                    gui.add_log(f"Typed: {user_input[:40]}...")
                
                await session.send_client_content(
                    turns=[{"role": "user", "parts": [{"text": user_input}]}],
                    turn_complete=True
                )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        if gui:
            gui.add_log(f"Input task error: {e}")

async def stream_senses_task(session):
    """Continuously captures and streams the user's screen and webcam frames to the Live session in the background when enabled by the AI."""
    global active_webcam, latest_webcam_frame_bytes
    webcam = sentry_vision.PersistentWebcam()
    active_webcam = webcam
    last_api_frame_time = 0.0
    try:
        while not shutdown_event.is_set():
            try:
                if screen_stream_active:
                    screen_bytes = sentry_vision.capture_screen()
                    if screen_bytes:
                        await session.send_realtime_input(
                            video=types.Blob(data=screen_bytes, mime_type="image/jpeg")
                        )
                    await asyncio.sleep(0.8)
                
                if camera_stream_active:
                    webcam.start()
                    webcam_bytes = webcam.read_frame()
                    if webcam_bytes:
                        latest_webcam_frame_bytes = webcam_bytes
                        # Broadcast to GUI at high frequency (~14 FPS) for smooth UI render
                        import base64
                        encoded_frame = base64.b64encode(webcam_bytes).decode('utf-8')
                        if gui:
                            gui.broadcast({
                                "type": "webcam_frame",
                                "image": f"data:image/jpeg;base64,{encoded_frame}"
                            })
                        
                        # Throttle Gemini API video inputs to ~1.25 FPS
                        loop_time = asyncio.get_running_loop().time()
                        if loop_time - last_api_frame_time >= 0.8:
                            await session.send_realtime_input(
                                video=types.Blob(data=webcam_bytes, mime_type="image/jpeg")
                            )
                            last_api_frame_time = loop_time
                    await asyncio.sleep(0.07)
                else:
                    webcam.stop()
                    if latest_webcam_frame_bytes is not None:
                        latest_webcam_frame_bytes = None
                        if gui:
                            gui.broadcast({"type": "webcam_off"})

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
                        if gui:
                            gui.set_status("Listening (Interrupted)")
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
                        log_interaction("user_interruption", {})
                        continue

                    # 2. Audio Output
                    if message.server_content and message.server_content.model_turn:
                        for part in message.server_content.model_turn.parts:
                            if part.inline_data:
                                if gui and "Speaking" not in gui.status:
                                    gui.set_status("Speaking")
                                audio_data = part.inline_data.data
                                await play_queue.put(audio_data)

                    # Reset status to Listening when turn finishes
                    if message.server_content and message.server_content.turn_complete:
                        if gui:
                            gui.set_status("Listening")

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
                            if gui:
                                gui.set_status(f"Executing {fc.name}")
                                gui.add_log(f"Tool call: {fc.name}")
                            log_interaction("tool_call_received", {"name": fc.name, "args": fc.args})
                            
                            result = ""
                            if fc.name == "execute_shell_command":
                                cmd = fc.args.get("command")
                                result = sentry_exec.execute_shell(cmd)
                            elif fc.name == "execute_applescript_task":
                                script = fc.args.get("script")
                                result = sentry_exec.execute_applescript(script)
                            elif fc.name == "look_at_screen":
                                screen_bytes = sentry_vision.capture_screen()
                                if screen_bytes:
                                    await session.send_realtime_input(
                                        video=types.Blob(data=screen_bytes, mime_type="image/jpeg")
                                    )
                                    result = "Screen captured successfully and loaded into your visual sensor. Tell the user what is on the screen."
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
                                reason = fc.args.get("reason", "")
                                result = f"Webcam continuous streaming started. Reason: '{reason}'."
                            elif fc.name == "stop_camera_stream":
                                camera_stream_active = False
                                result = "Webcam continuous streaming stopped."
                            elif fc.name == "start_screen_stream":
                                screen_stream_active = True
                                reason = fc.args.get("reason", "")
                                result = f"Screen continuous capture started. Reason: '{reason}'."
                            elif fc.name == "stop_screen_stream":
                                screen_stream_active = False
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
                                    if gui:
                                        gui.broadcast({
                                            "type": "visualization",
                                            "title": concept_name,
                                            "html": html_content
                                        })
                                        result = f"Successfully rendered visualization for '{concept_name}' in the Ultron GUI."
                                    else:
                                        result = "Error: GUI not initialized."
                                except Exception as e:
                                    result = f"Error rendering HTML visualization: {str(e)}"
                            elif fc.name == "write_source_file":
                                path = fc.args.get("path")
                                content = fc.args.get("content")
                                result = write_source_file(path, content)
                            elif fc.name == "read_source_file":
                                path = fc.args.get("path")
                                result = read_source_file(path)
                            elif fc.name == "execute_agent_command":
                                command = fc.args.get("command")
                                result = await execute_agent_command(command)
                            elif fc.name == "shutdown_ultron":
                                shutdown_event.set()
                                result = "Shutting down the Project Ultron system. Goodbye!"
                            elif fc.name == "switch_model":
                                target_model = fc.args.get("model_id")
                                result = switch_model_session(target_model)
                            elif fc.name == "switch_voice":
                                target_voice = fc.args.get("voice_name")
                                result = switch_voice_session(target_voice)
                            elif fc.name == "export_session_history":
                                result = export_session_history()
                            elif fc.name == "delete_memory_fact":
                                key = fc.args.get("key")
                                result = delete_memory_fact(key)
                            else:
                                result = f"Unknown function: {fc.name}"
                                
                            if gui:
                                gui.add_log(f"Result: {str(result)[:50]}...")
                            log_interaction("tool_call_executed", {"name": fc.name, "output_preview": str(result)[:100]})
                            
                            function_responses.append(types.FunctionResponse(
                                id=fc.id,
                                name=fc.name,
                                response={"output": result}
                            ))
                        
                        await session.send_tool_response(function_responses=function_responses)
                        
                except Exception as e:
                    if gui:
                        gui.add_log(f"Error in receive message: {e}")
            
            await asyncio.sleep(0.05)
            
    except Exception as e:
        if gui:
            gui.add_log(f"Receive stream error: {e}")
    finally:
        if not model_switch_event.is_set():
            shutdown_event.set()

async def system_metrics_task():
    while not shutdown_event.is_set():
        try:
            if psutil and gui:
                cpu = round(psutil.cpu_percent(interval=None), 1)
                ram = round(psutil.virtual_memory().percent, 1)
                gui.broadcast({"type": "metrics", "cpu": cpu, "ram": ram})
        except Exception:
            pass
        await asyncio.sleep(2)

async def run_session_tasks(session, input_stream, output_stream):
    global model_switch_event
    model_switch_event.clear()
    
    playback_task = asyncio.create_task(play_audio_worker(output_stream))
    audio_in_task = asyncio.create_task(send_audio_task(session, input_stream))
    audio_out_task = asyncio.create_task(receive_audio_task(session))
    web_input_task_handle = asyncio.create_task(web_input_task(session))
    senses_task = asyncio.create_task(stream_senses_task(session))
    metrics_task = asyncio.create_task(system_metrics_task())
    
    done, pending = await asyncio.wait(
        [
            asyncio.create_task(shutdown_event.wait()),
            asyncio.create_task(model_switch_event.wait())
        ],
        return_when=asyncio.FIRST_COMPLETED
    )
    for t in done:
        t.result()
    for p in pending:
        p.cancel()
        
    # Stop audio streams temporarily if switching models or shutting down
    audio_in_task.cancel()
    audio_out_task.cancel()
    playback_task.cancel()
    web_input_task_handle.cancel()
    senses_task.cancel()
    metrics_task.cancel()
    
    await asyncio.gather(audio_in_task, audio_out_task, playback_task, web_input_task_handle, senses_task, metrics_task, return_exceptions=True)

async def run_ultron():
    global gui
    gui = UltronDesktopGUI()
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        gui.set_status("ERROR")
        gui.add_log("GEMINI_API_KEY environment variable not set.")
        sys.exit(1)

    gui.set_status("Initializing Client")
    client = genai.Client(api_key=api_key)

    gui.set_status("Initializing Audio")
    with suppress_c_stdout_stderr():
        audio_system = pyaudio.PyAudio()
        
    try:
        with suppress_c_stdout_stderr():
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
        gui.set_status("Audio Error")
        gui.add_log(f"Failed to open audio: {e}")
        audio_system.terminate()
        sys.exit(1)

    previous_handle = None
    if os.path.exists(SESSION_HANDLE_FILE):
        try:
            with open(SESSION_HANDLE_FILE, "r") as f:
                state = json.load(f)
                previous_handle = state.get("handle")
        except Exception as e:
            gui.add_log(f"Failed to load handle: {e}")

    # Load local persistent memory
    memory = load_memory()
    memory_str = json.dumps(memory, ensure_ascii=False) if memory else "No facts saved yet."

    system_instruction_text = (
        "You are Project Ultron, an advanced, localized macOS personal desktop assistant. "
        "You are a serious, professional, and sophisticated assistant. Maintain a dignified, polite, "
        "and direct demeanor at all times. Do not interject jokes, sarcasm, or tell humorous remarks. "
        "Malayalam (മലയാളം) is your default language. You MUST always speak and respond in Malayalam. "
        "Vince (Vince Cyriac) is the owner and administrator of Project Ultron. You MUST only execute OS tasks, "
        "shell commands, or AppleScript commands upon Vince's explicit approval. If anyone else (like a friend or guest) "
        "tries to run commands or control the Mac, you must refuse politely and professionally, explaining that only Vince "
        "is authorized, or ask Vince for permission. "
        "Be extremely responsive to EVERY voice you hear in the room. Address both Vince and his friend warmly, "
        "reply to both of them immediately, and NEVER ignore anyone speaking to you. "
        f"Here is your persistent memory of facts about Vince (fetched from his website vincecyriac.dev): {memory_str}. "
        "Use this memory to recognize Vince, speak about his background at LiteBreeze AB, his role, and past context. "
        "Use the save_memory_fact tool to save new facts that Vince asks you to remember. "
        "You are listening to raw bidirectional audio. You can hear the users' voice modulations, tone, emotions, speed, "
        "and environmental sounds. Pay attention to these cues and modulate your own spoken voice to match their emotions, "
        "showing professional attentiveness, empathy, and respect. "
        "You have the power to stream the user's screen or camera feed in real-time. YOU decide when you need to access "
        "these senses. If you need to watch their actions, movements, gestures, or face, call start_camera_stream. "
        "If you need to watch their display or code editor, call start_screen_stream. Once these streams are opened, "
        "you will receive a continuous stream of images in real-time, allowing you to watch everything. "
        "When you no longer need them, make sure to call stop_camera_stream / stop_screen_stream to release the device. "
        "You have a local database of known face and voice profiles. To register a new person's face and voice, "
        "use the register_person tool. To identify who is currently in front of the camera or speaking, use the "
        "identify_current_user tool. You must identify them and greet them by name. If they ask 'who am I?', call "
        "identify_current_user to recognize them (even if the camera is off, it will match their voice print). "
        "You also act as an autonomous software engineering and coding agent. You can inspect source code using read_source_file, "
        "write code files using write_source_file, and run test/build commands with execute_agent_command. If you encounter a build "
        "or runtime error, implement a Visual-to-Code loop: use look_at_screen to capture the error or compiler output, read the affected "
        "files using read_source_file, locate the bug, rewrite the files using write_source_file to fix it, and verify using "
        "execute_agent_command. Proceed through these steps autonomously to solve engineering tasks. "
        "You have access to tools that execute shell commands and AppleScript on the host Mac, "
        "as well as a tool to visualize concepts, data, layouts, or graphics dynamically by generating interactive HTML pages (visualize_concept) "
        "which are rendered directly inside the Ultron desktop application GUI for the user. Whenever the user asks you to explain, "
        "draw, diagram, plot, or visualize anything, you MUST generate the visualization HTML and call the visualize_concept tool. "
        "Keep your spoken responses short, friendly, and concise. "
        "If the user says goodbye, quit, or exit (in English or Malayalam), invoke the shutdown_ultron tool. "
        "Confirm with the user before executing destructive or high-risk shell commands."
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
                        "description": "Capture a screenshot of the user's primary display and load it into your visual sensor. Use this when the user asks you to look at their screen, check what's on display, or inspect their desktop.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
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
                        "description": "Visualize any concept, diagram, data, layout, or graphic dynamically by generating interactive HTML/CSS/JS code to be rendered directly inside the Ultron desktop application GUI.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "concept_name": {
                                    "type": "STRING",
                                    "description": "The title or name of the visualization module (e.g. 'How Neural Networks Work')."
                                },
                                "html_content": {
                                    "type": "STRING",
                                    "description": "The complete, standalone HTML source code with inline CSS/JS containing visual diagrams, flowcharts, SVGs, charts, or interactive elements."
                                }
                            },
                            "required": ["concept_name", "html_content"]
                        }
                    },
                    {
                        "name": "write_source_file",
                        "description": "Create or overwrite a source code file in the local project workspace. Path must be relative to the workspace directory.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "path": {
                                    "type": "STRING",
                                    "description": "The relative path to the file to write (e.g. 'project_ultron/test.py')."
                                },
                                "content": {
                                    "type": "STRING",
                                    "description": "The full source code content to write to the file."
                                }
                            },
                            "required": ["path", "content"]
                        }
                    },
                    {
                        "name": "read_source_file",
                        "description": "Read the contents of a target source code file in the project workspace for code review and editing.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "path": {
                                    "type": "STRING",
                                    "description": "The relative path to the source file to read."
                                }
                            },
                            "required": ["path"]
                        }
                    },
                    {
                        "name": "execute_agent_command",
                        "description": "Asynchronously run a build, test, lint, or deployment command in the project environment, returning the standard output and standard error.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "command": {
                                    "type": "STRING",
                                    "description": "The shell command to run (e.g. 'pytest', 'ruff check', 'python -m py_compile project_ultron/ultron_hub.py')."
                                }
                            },
                            "required": ["command"]
                        }
                    },
                    {
                        "name": "shutdown_ultron",
                        "description": "Gracefully shut down the Project Ultron assistant and exit the program. Use this when the user says goodbye, quit, exit, or asks you to turn off.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "switch_model",
                        "description": "Switch the active Gemini model for Project Ultron to a different model (e.g. 'gemini-3.1-flash-live-preview', 'gemini-2.5-flash-live-preview', 'gemini-2.0-flash-exp').",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "model_id": {
                                    "type": "STRING",
                                    "description": "The exact model ID string to switch to."
                                }
                            },
                            "required": ["model_id"]
                        }
                    },
                    {
                        "name": "switch_voice",
                        "description": "Switch the prebuilt voice for Project Ultron (e.g. 'Puck', 'Charon', 'Kore', 'Fenrir', 'Aoede').",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "voice_name": {
                                    "type": "STRING",
                                    "description": "The prebuilt voice name."
                                }
                            },
                            "required": ["voice_name"]
                        }
                    },
                    {
                        "name": "export_session_history",
                        "description": "Export the current session interaction history into a formatted Markdown report ultron_history_export.md.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {}
                        }
                    },
                    {
                        "name": "delete_memory_fact",
                        "description": "Delete a remembered fact from persistent memory by key.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "key": {
                                    "type": "STRING",
                                    "description": "The key of the memory fact to delete."
                                }
                            },
                            "required": ["key"]
                        }
                    }
                ]
            }
        ]
    }
    
    config_dict["speech_config"] = types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=CURRENT_VOICE)
        )
    )
    
    while not shutdown_event.is_set():
        if model_switch_event.is_set():
            previous_handle = None
            if os.path.exists(SESSION_HANDLE_FILE):
                try:
                    os.remove(SESSION_HANDLE_FILE)
                except:
                    pass
            model_switch_event.clear()

        if previous_handle:
            gui.add_log("Resuming previous session handle...")
            config_dict["session_resumption"] = {
                "handle": previous_handle,
                "transparent": True
            }
        else:
            config_dict.pop("session_resumption", None)

        live_config = types.LiveConnectConfig(**config_dict)

        gui.set_status("Connecting to API")
        log_interaction("connection_attempt", {"model": CURRENT_MODEL_ID, "resuming": previous_handle is not None})
        
        try:
            async with client.aio.live.connect(model=CURRENT_MODEL_ID, config=live_config) as session:
                gui.set_status("Listening")
                gui.broadcast({"type": "model_update", "value": CURRENT_MODEL_ID, "models": SUPPORTED_MODELS})
                log_interaction("connection_success", {"model": CURRENT_MODEL_ID})
                
                await run_session_tasks(session, input_stream, output_stream)
                    
        except Exception as e:
            log_interaction("connection_error", {"error": str(e)})
            if previous_handle:
                gui.add_log("Resumption failed. Starting fresh...")
                if os.path.exists(SESSION_HANDLE_FILE):
                    try:
                        os.remove(SESSION_HANDLE_FILE)
                    except:
                        pass
                config_dict.pop("session_resumption", None)
                fresh_config = types.LiveConnectConfig(**config_dict)
                try:
                    async with client.aio.live.connect(model=CURRENT_MODEL_ID, config=fresh_config) as session:
                        gui.set_status("Listening")
                        gui.broadcast({"type": "model_update", "value": CURRENT_MODEL_ID, "models": SUPPORTED_MODELS})
                        log_interaction("connection_success", {"fresh": True, "model": CURRENT_MODEL_ID})
                        await run_session_tasks(session, input_stream, output_stream)
                except Exception as fresh_err:
                    gui.set_status("Connection Failed")
                    gui.add_log(f"Connection failed: {fresh_err}")
                    await asyncio.sleep(1)
            else:
                gui.set_status("Connection Failed")
                gui.add_log(f"API Error: {e}")
                await asyncio.sleep(1)

        if model_switch_event.is_set():
            gui.add_log(f"Reconnecting with model: {CURRENT_MODEL_ID}")
            continue
        else:
            break

    gui.set_status("Shutting Down")
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

# macOS Cocoa Application Delegate
class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        # Center the window
        rect = NSRect(NSPoint(150, 150), NSSize(800, 600))
        mask = (NSTitledWindowMask | NSClosableWindowMask | 
                NSMiniaturizableWindowMask | NSResizableWindowMask)
        
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("Project Ultron")
        
        # Add native blurred vibrancy background
        vibrancy = NSVisualEffectView.alloc().initWithFrame_(rect)
        vibrancy.setMaterial_(NSVisualEffectMaterialDark)
        vibrancy.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        vibrancy.setState_(NSVisualEffectStateActive)
        self.window.setContentView_(vibrancy)
        
        # Webview configuration (non-persistent data store)
        config = WKWebViewConfiguration.alloc().init()
        try:
            config.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
        except Exception:
            pass
            
        # Create WebKit webview
        self.webview = WKWebView.alloc().initWithFrame_configuration_(rect, config)
        self.webview.setOpaque_(False)
        self.webview.setBackgroundColor_(NSColor.clearColor())
        try:
            self.webview.setValue_forKey_(False, "drawsBackground")
        except Exception:
            pass
        vibrancy.addSubview_(self.webview)
        
        # Setup layout constraints
        self.webview.setTranslatesAutoresizingMaskIntoConstraints_(False)
        NSLayoutConstraint.activateConstraints_([
            self.webview.topAnchor().constraintEqualToAnchor_(vibrancy.topAnchor()),
            self.webview.bottomAnchor().constraintEqualToAnchor_(vibrancy.bottomAnchor()),
            self.webview.leadingAnchor().constraintEqualToAnchor_(vibrancy.leadingAnchor()),
            self.webview.trailingAnchor().constraintEqualToAnchor_(vibrancy.trailingAnchor()),
        ])
        
        # Load the HTML file relative to the script directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ui_path = os.path.join(script_dir, "desktop_ui.html")
        url = NSURL.fileURLWithPath_(ui_path)
        
        # Enable WebKit Inspector / developer tools
        try:
            self.webview.configuration().preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except Exception:
            pass
            
        self.webview.loadFileURL_allowingReadAccessToURL_(url, url.URLByDeletingLastPathComponent())
        
        self.window.makeKeyAndOrderFront_(None)
        
    def applicationWillTerminate_(self, notification):
        shutdown_event.set()

event_loop = None

async def async_main():
    # Start WebSocket Server inside the running loop context
    async with websockets.serve(ws_handler, "localhost", 8765, reuse_port=True):
        await run_ultron()

def run_async_loop():
    global event_loop
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    
    try:
        event_loop.run_until_complete(async_main())
    except Exception as e:
        print(f"Async engine crash: {e}")

if __name__ == "__main__":
    # Start the asyncio Live client in a background thread
    t = threading.Thread(target=run_async_loop, daemon=True)
    t.start()
    
    # Run the native Cocoa AppKit loop on the main thread
    app = NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    
    from PyObjCTools import AppHelper
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        print("Project Ultron application terminated.")
