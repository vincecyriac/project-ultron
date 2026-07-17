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
MODEL_ID = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")

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



class UltronTerminalGUI:
    def __init__(self):
        self.status = "Initializing"
        self.logs = []
        self.wave_frame = 0
        self.waves = [
            "  ▂ ▃ ▄ ▅ ▆ ▇ ▆ ▅ ▄ ▃ ▂  ",
            "  ▃ ▄ ▅ ▆ ▇ █ ▇ ▆ ▅ ▄ ▃  ",
            "  ▄ ▅ ▆ ▇ █ ▇ ▆ ▅ ▄ ▃ ▂  ",
            "  ▅ ▆ ▇ ▆ ▅ ▄ ▃ ▂     ▂  ",
            "  ▆ ▅ ▄ ▃ ▂     ▂ ▃ ▄ ▅  "
        ]
        self.old_lines = []

    def set_status(self, status):
        self.status = status
        self.redraw()

    def add_log(self, log_msg):
        self.logs.append(log_msg)
        self.redraw()

    def tick_wave(self):
        if "Speaking" in self.status or "Thinking" in self.status or "Executing" in self.status or camera_stream_active:
            self.wave_frame = (self.wave_frame + 1) % len(self.waves)
            self.redraw()

    def redraw(self):
        try:
            columns, rows = os.get_terminal_size()
        except:
            columns, rows = 80, 24
            
        columns = max(columns, 60)
        rows = max(rows, 15)
        
        CYAN = "\033[36m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        BLUE = "\033[34m"
        MAGENTA = "\033[35m"
        RESET = "\033[0m"
        BOLD = "\033[1m"
        
        new_lines = []
        
        def get_border_row(text, align="left", color=""):
            content_width = columns - 6
            if align == "center":
                vis_w = get_visual_width(text)
                padding = content_width - vis_w
                left_pad = max(0, padding // 2)
                right_pad = max(0, padding - left_pad)
                line = " " * left_pad + text + " " * right_pad
            else:
                line = visual_ljust(text, content_width)
            return f"{CYAN}│{RESET}  {color}{line}{RESET}  {CYAN}│{RESET}"

        # 1. Top border
        new_lines.append(f"{CYAN}┌" + "─" * (columns - 2) + f"┐{RESET}")
        
        # 2. Header
        new_lines.append(get_border_row("🤖 PROJECT ULTRON SYSTEM 🤖", align="center", color=BOLD+CYAN))
        new_lines.append(f"{CYAN}├" + "─" * (columns - 2) + f"┤{RESET}")
        
        # 3. Status & Senses
        status_color = GREEN if "Listening" in self.status else (YELLOW if "Speaking" in self.status or "Thinking" in self.status or "Executing" in self.status else CYAN)
        new_lines.append(get_border_row(f"SYSTEM STATUS: [ {self.status} ]", color=BOLD+status_color))
        
        webcam_status = "STREAMING" if camera_stream_active else "READY"
        screen_status = "STREAMING" if screen_stream_active else "READY"
        senses_line = f"SENSES:        🎤 [ Mic: ACTIVE ]  🖥️ [ Screen: {screen_status} ]  📷 [ Webcam: {webcam_status} ]"
        new_lines.append(get_border_row(senses_line, color=BLUE))
        new_lines.append(f"{CYAN}├" + "─" * (columns - 2) + f"┤{RESET}")
        
        # 4. Talking Visualizer
        new_lines.append(get_border_row("TALKING VISUALIZER / EQUALIZER", align="center", color=BOLD+MAGENTA))
        if "Speaking" in self.status or "Thinking" in self.status or "Executing" in self.status:
            wave = self.waves[self.wave_frame]
            new_lines.append(get_border_row(wave, align="center", color=MAGENTA))
        else:
            idle_line = "- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -"
            new_lines.append(get_border_row(idle_line, align="center", color=BLUE))
            
        new_lines.append(f"{CYAN}├" + "─" * (columns - 2) + f"┤{RESET}")
        
        # 5. System logs
        new_lines.append(get_border_row("SYSTEM LOGS", color=BOLD+YELLOW))
        
        log_panel_height = max(4, rows - 11)
        display_logs = self.logs[-log_panel_height:] if self.logs else []
        for log in display_logs:
            new_lines.append(get_border_row(f"> {log}", color=RESET))
        for _ in range(log_panel_height - len(display_logs)):
            new_lines.append(get_border_row(""))
            
        # 6. Bottom Border
        new_lines.append(f"{CYAN}└" + "─" * (columns - 2) + f"┘{RESET}")
        
        # Incremental writing to prevent screen flickering
        if len(new_lines) != len(self.old_lines):
            sys.stdout.write("\033[H\033[J")
            for line in new_lines:
                sys.stdout.write(line + "\n")
            self.old_lines = list(new_lines)
        else:
            for i in range(len(new_lines)):
                if new_lines[i] != self.old_lines[i]:
                    sys.stdout.write(f"\033[{i+1};1H{new_lines[i]}\033[K")
                    self.old_lines[i] = new_lines[i]
        # Render the input prompt statically on the bottom line
        prompt_line = f" {BOLD}{CYAN}Type message or speak (Ctrl+C to exit):{RESET} {input_buffer}"
        sys.stdout.write(f"\033[{rows};1H{prompt_line}\033[K")
        sys.stdout.flush()

# Global states
play_queue = asyncio.Queue()
interrupted_event = asyncio.Event()
shutdown_event = asyncio.Event()
gui = None
camera_stream_active = False
screen_stream_active = False
active_webcam = None
latest_webcam_frame_bytes = None
input_buffer = ""
mic_audio_buffer = bytearray()
MAX_BUFFER_SIZE = 96000  # 3 seconds of 16kHz 16-bit PCM
model_is_speaking = False

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
            if gui:
                gui.add_log(f"Playback error: {e}")
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
                
                if not model_is_speaking:
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

async def terminal_input_task(session):
    """Asynchronously reads text input from the terminal with manual echo control to prevent character scattering."""
    global input_buffer
    loop = asyncio.get_running_loop()
    
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    
    try:
        tty.setcbreak(fd)
        while not shutdown_event.is_set():
            char = await loop.run_in_executor(None, sys.stdin.read, 1)
            if not char:
                break
                
            if char in ('\r', '\n'):
                user_input = input_buffer.strip()
                input_buffer = ""
                if user_input:
                    if user_input.lower() in ('exit', 'quit', 'bye'):
                        shutdown_event.set()
                        break
                    
                    if gui:
                        gui.add_log(f"Typed: {user_input[:40]}...")
                        gui.redraw()
                    
                    await session.send_client_content(
                        turns=[{"role": "user", "parts": [{"text": user_input}]}],
                        turn_complete=True
                    )
            elif char in ('\x7f', '\x08'):
                if len(input_buffer) > 0:
                    input_buffer = input_buffer[:-1]
                    if gui:
                        gui.redraw()
            elif ord(char) >= 32 or char == '\t':
                input_buffer += char
                if gui:
                    gui.redraw()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        if gui:
            gui.add_log(f"Input task error: {e}")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

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
                        await session.send_realtime_input(
                            video=types.Blob(data=screen_bytes, mime_type="image/jpeg")
                        )
                    await asyncio.sleep(0.8)
                
                if camera_stream_active:
                    webcam.start()
                    webcam_bytes = webcam.read_frame()
                    if webcam_bytes:
                        latest_webcam_frame_bytes = webcam_bytes
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
                                    filename = "ultron_visualization.html"
                                    with open(filename, "w", encoding="utf-8") as f:
                                        f.write(html_content)
                                    import subprocess
                                    subprocess.run(["open", filename])
                                    result = f"Successfully generated HTML visualization for '{concept_name}' and opened in default browser."
                                except Exception as e:
                                    result = f"Error generating HTML visualization: {str(e)}"
                            elif fc.name == "shutdown_ultron":
                                shutdown_event.set()
                                result = "Shutting down the Project Ultron system. Goodbye!"
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
        shutdown_event.set()

async def visualizer_tick_task():
    while True:
        try:
            if gui:
                gui.tick_wave()
            await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            break

async def run_session_tasks(session, input_stream, output_stream):
    shutdown_event.clear()
    
    playback_task = asyncio.create_task(play_audio_worker(output_stream))
    audio_in_task = asyncio.create_task(send_audio_task(session, input_stream))
    audio_out_task = asyncio.create_task(receive_audio_task(session))
    terminal_task = asyncio.create_task(terminal_input_task(session))
    senses_task = asyncio.create_task(stream_senses_task(session))
    visualizer_task = asyncio.create_task(visualizer_tick_task())
    
    await shutdown_event.wait()
    
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
    terminal_task.cancel()
    senses_task.cancel()
    visualizer_task.cancel()
    
    await asyncio.gather(audio_in_task, audio_out_task, playback_task, terminal_task, senses_task, visualizer_task, return_exceptions=True)

async def run_ultron():
    global gui
    gui = UltronTerminalGUI()
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        gui.set_status("ERROR")
        gui.add_log("GEMINI_API_KEY environment variable not set.")
        sys.exit(1)

    gui.set_status("Initializing Client")
    client = genai.Client(api_key=api_key)

    gui.set_status("Initializing Audio")
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
        "You have access to tools that execute shell commands and AppleScript on the host Mac, "
        "as well as a tool to visualize concepts dynamically by generating an HTML page. "
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
        gui.add_log("Resuming previous session handle...")
        config_dict["session_resumption"] = {
            "handle": previous_handle,
            "transparent": True
        }

    live_config = types.LiveConnectConfig(**config_dict)

    gui.set_status("Connecting to API")
    log_interaction("connection_attempt", {"model": MODEL_ID, "resuming": previous_handle is not None})
    
    try:
        async with client.aio.live.connect(model=MODEL_ID, config=live_config) as session:
            gui.set_status("Listening")
            log_interaction("connection_success", {})
            
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
                async with client.aio.live.connect(model=MODEL_ID, config=fresh_config) as session:
                    gui.set_status("Listening")
                    log_interaction("connection_success", {"fresh": True})
                    await run_session_tasks(session, input_stream, output_stream)
            except Exception as fresh_err:
                gui.set_status("Connection Failed")
                gui.add_log(f"Connection failed: {fresh_err}")
        else:
            gui.set_status("Connection Failed")
            gui.add_log(f"API Error: {e}")
    finally:
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
        sys.stdout.write("\033[H\033[J")
        print("Project Ultron engine terminated. Goodbye.")

if __name__ == "__main__":
    try:
        asyncio.run(run_ultron())
    except KeyboardInterrupt:
        sys.stdout.write("\033[H\033[J")
        print("\nProject Ultron terminated by user.")
