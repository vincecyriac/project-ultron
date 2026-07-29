"""
sentry_recognition.py - Face and voice recognition module for Project Ultron.

Face pipeline: YuNet ONNX face detection -> SFace ONNX 128-d embeddings
(cosine similarity). Replaces the old raw-pixel L2 approach, giving
robustness to lighting, pose and scale.

Voice pipeline: mel-filterbank MFCC features (pure numpy) pooled with
mean+std statistics over the utterance, cosine similarity against
multiple enrolled samples per person.

Profiles are stored as LISTS of embeddings per person so each new
registration adds a sample and matching uses the best (nearest) one.
"""

import os
import json
import subprocess
import numpy as np
import cv2

PROFILES_FILE = "ultron_profiles.json"
YUNET_MODEL_PATH = "face_detection_yunet.onnx"
SFACE_MODEL_PATH = "face_recognition_sface.onnx"
YUNET_MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
SFACE_MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"

# Cosine similarity thresholds (higher = stricter)
FACE_MATCH_THRESHOLD = 0.40   # SFace recommended cosine threshold ~0.363; slightly stricter
VOICE_MATCH_THRESHOLD = 0.72

MAX_SAMPLES_PER_PERSON = 8

_detector = None
_recognizer = None


def _download_model(path: str, url: str):
    """Downloads an ONNX model if missing or if it's a Git-LFS pointer stub."""
    if os.path.exists(path) and os.path.getsize(path) < 10000:
        try:
            os.remove(path)
        except OSError:
            pass
    if not os.path.exists(path):
        subprocess.run(["curl", "-sL", "-o", path, url], timeout=120, check=True)


def _get_models(frame_w: int, frame_h: int):
    global _detector, _recognizer
    _download_model(YUNET_MODEL_PATH, YUNET_MODEL_URL)
    _download_model(SFACE_MODEL_PATH, SFACE_MODEL_URL)
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(YUNET_MODEL_PATH, "", (frame_w, frame_h), 0.7)
    else:
        _detector.setInputSize((frame_w, frame_h))
    if _recognizer is None:
        _recognizer = cv2.FaceRecognizerSF.create(SFACE_MODEL_PATH, "")
    return _detector, _recognizer


# ---------------------------------------------------------------------------
# Face embeddings
# ---------------------------------------------------------------------------

def extract_face_embeddings(image_bytes: bytes) -> list:
    """Detects all faces in a JPEG frame, returns list of (embedding, bbox) tuples.
    Embeddings are 128-d SFace features aligned via YuNet landmarks."""
    if not image_bytes:
        return []
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    h, w = img.shape[:2]
    detector, recognizer = _get_models(w, h)
    _, faces = detector.detect(img)
    results = []
    if faces is None:
        return results
    for face in faces:
        try:
            aligned = recognizer.alignCrop(img, face)
            feature = recognizer.feature(aligned)
            emb = np.asarray(feature).flatten().astype(np.float32)
            norm = np.linalg.norm(emb)
            if norm > 1e-6:
                emb /= norm
            bbox = tuple(int(v) for v in face[:4])
            results.append((emb.tolist(), bbox))
        except Exception:
            continue
    return results


def face_cosine_similarity(a: list, b: list) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


# ---------------------------------------------------------------------------
# Voice embeddings (MFCC statistics, pure numpy)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000
N_FFT = 512
HOP = 160          # 10ms hop
N_MELS = 26
N_MFCC = 13


def _hz_to_mel(hz):
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel):
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank():
    n_bins = N_FFT // 2 + 1
    mel_min = _hz_to_mel(80.0)
    mel_max = _hz_to_mel(SAMPLE_RATE / 2.0)
    mel_points = np.linspace(mel_min, mel_max, N_MELS + 2)
    hz_points = _mel_to_hz(mel_points)
    bin_points = np.floor((N_FFT + 1) * hz_points / SAMPLE_RATE).astype(int)
    bin_points = np.clip(bin_points, 0, n_bins - 1)
    fbank = np.zeros((N_MELS, n_bins), dtype=np.float32)
    for m in range(1, N_MELS + 1):
        left, center, right = bin_points[m - 1], bin_points[m], bin_points[m + 1]
        for k in range(left, center):
            if center > left:
                fbank[m - 1, k] = (k - left) / (center - left)
        for k in range(center, right):
            if right > center:
                fbank[m - 1, k] = (right - k) / (right - center)
    return fbank


_FBANK = _mel_filterbank()
_DCT = np.cos(np.pi / N_MELS * (np.arange(N_MELS) + 0.5)[None, :] * np.arange(N_MFCC)[:, None]).astype(np.float32)


def extract_voice_embedding(audio_bytes: bytes) -> list:
    """Computes a speaker embedding from raw 16kHz 16-bit PCM audio.
    MFCC frames over voiced segments, pooled as mean+std -> 26-d vector."""
    try:
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < N_FFT * 4:
            return None
        # Pre-emphasis
        samples = np.append(samples[0], samples[1:] - 0.97 * samples[:-1])

        window = np.hanning(N_FFT).astype(np.float32)
        frames = []
        energies = []
        for i in range(0, len(samples) - N_FFT, HOP):
            chunk = samples[i:i + N_FFT] * window
            spec = np.abs(np.fft.rfft(chunk)) ** 2
            frames.append(spec)
            energies.append(np.sum(spec))
        if not frames:
            return None
        frames = np.asarray(frames, dtype=np.float32)
        energies = np.asarray(energies)

        # Voice-activity gate: keep frames above 15% of the 90th-percentile energy
        thresh = 0.15 * np.percentile(energies, 90)
        voiced = frames[energies > max(thresh, 1e-8)]
        if len(voiced) < 8:
            return None

        mel_energies = np.log(voiced @ _FBANK.T + 1e-8)
        mfcc = mel_energies @ _DCT.T          # (frames, N_MFCC)
        mfcc = mfcc - np.mean(mfcc, axis=0)   # cepstral mean normalization

        pooled = np.concatenate([np.mean(mfcc, axis=0), np.std(mfcc, axis=0)])
        norm = np.linalg.norm(pooled)
        if norm > 1e-6:
            pooled /= norm
        return pooled.astype(np.float32).tolist()
    except Exception:
        return None


def extract_voice_embeddings_windowed(audio_bytes: bytes, window_sec: float = 2.0, hop_sec: float = 1.0) -> list:
    """Extracts speaker embeddings over sliding windows (multi-speaker aware)."""
    bytes_per_sec = SAMPLE_RATE * 2
    win = int(window_sec * bytes_per_sec)
    hop = int(hop_sec * bytes_per_sec)
    if len(audio_bytes) <= win:
        emb = extract_voice_embedding(audio_bytes)
        return [emb] if emb else []
    out = []
    for i in range(0, len(audio_bytes) - win + 1, hop):
        emb = extract_voice_embedding(audio_bytes[i:i + win])
        if emb:
            out.append(emb)
    return out


# ---------------------------------------------------------------------------
# Profiles store
# ---------------------------------------------------------------------------

def load_profiles() -> dict:
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_profiles(profiles: dict):
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f)


def _ensure_new_format(profile: dict) -> dict:
    """Migrates a legacy single-signature profile entry to the multi-sample format.
    Legacy raw-pixel/FFT signatures are incompatible and dropped."""
    if "face_embeddings" not in profile:
        profile["face_embeddings"] = []
    if "voice_embeddings" not in profile:
        profile["voice_embeddings"] = []
    profile.pop("face_signature", None)
    profile.pop("voice_signature", None)
    return profile


def register_person(name: str, mic_audio: bytes, webcam_frame: bytes) -> str:
    """Enrolls (or adds samples to) a person from the current mic buffer and webcam frame."""
    profiles = load_profiles()
    entry = _ensure_new_format(profiles.get(name, {}))

    voice_embs = extract_voice_embeddings_windowed(mic_audio) if mic_audio else []
    face_results = extract_face_embeddings(webcam_frame) if webcam_frame else []

    if not voice_embs and not face_results:
        return ("Error: captured neither a usable voice sample nor a face. "
                "Ask the person to speak a full sentence and look at the camera, then retry.")

    added_v = 0
    for emb in voice_embs[:3]:
        entry["voice_embeddings"].append(emb)
        added_v += 1
    entry["voice_embeddings"] = entry["voice_embeddings"][-MAX_SAMPLES_PER_PERSON:]

    added_f = 0
    if face_results:
        # Enroll the largest detected face (assume it's the subject)
        face_results.sort(key=lambda r: r[1][2] * r[1][3], reverse=True)
        entry["face_embeddings"].append(face_results[0][0])
        entry["face_embeddings"] = entry["face_embeddings"][-MAX_SAMPLES_PER_PERSON:]
        added_f = 1

    profiles[name] = entry
    save_profiles(profiles)

    parts = [f"Registered '{name}'."]
    parts.append(f"Voice samples added: {added_v} (total {len(entry['voice_embeddings'])}).")
    if added_f:
        parts.append(f"Face sample added (total {len(entry['face_embeddings'])}).")
    else:
        parts.append("No face detected — ask them to face the camera and register again to add a face print.")
    parts.append("Tip: registering 2-3 times in different conditions improves accuracy.")
    return " ".join(parts)


def _best_match(query_emb: list, profiles: dict, key: str, sim_fn) -> tuple:
    """Returns (name, best_similarity) across all enrolled samples of all people."""
    best_name, best_sim = None, -1.0
    for name, entry in profiles.items():
        for emb in entry.get(key, []):
            sim = sim_fn(query_emb, emb)
            if sim > best_sim:
                best_sim = sim
                best_name = name
    return best_name, best_sim


def identify_person(mic_audio: bytes, webcam_frame: bytes) -> str:
    """Identifies people present via voice and/or face. Returns a human-readable summary."""
    profiles = {n: _ensure_new_format(dict(e)) for n, e in load_profiles().items()}
    if not profiles:
        return "No registered profiles found. Use register_person first."

    found = {}

    if webcam_frame:
        for emb, bbox in extract_face_embeddings(webcam_frame):
            name, sim = _best_match(emb, profiles, "face_embeddings", face_cosine_similarity)
            if name and sim >= FACE_MATCH_THRESHOLD:
                rec = found.setdefault(name, {})
                rec["face_sim"] = max(rec.get("face_sim", 0), sim)
            else:
                found.setdefault("__unknown_face__", {"count": 0})["count"] = \
                    found.get("__unknown_face__", {}).get("count", 0) + 1

    if mic_audio:
        for emb in extract_voice_embeddings_windowed(mic_audio):
            name, sim = _best_match(emb, profiles, "voice_embeddings", face_cosine_similarity)
            if name and sim >= VOICE_MATCH_THRESHOLD:
                rec = found.setdefault(name, {})
                rec["voice_sim"] = max(rec.get("voice_sim", 0), sim)

    unknown_faces = found.pop("__unknown_face__", None)
    if not found:
        msg = "No registered person matched by face or voice."
        if unknown_faces:
            msg += f" ({unknown_faces['count']} unrecognized face(s) visible.)"
        return msg

    lines = []
    for name, rec in found.items():
        modes = []
        if "face_sim" in rec:
            modes.append(f"face {rec['face_sim']*100:.0f}%")
        if "voice_sim" in rec:
            modes.append(f"voice {rec['voice_sim']*100:.0f}%")
        lines.append(f"{name} ({' + '.join(modes)})")
    result = "Identified: " + ", ".join(lines)
    if unknown_faces:
        result += f". Also {unknown_faces['count']} unrecognized face(s) present."
    return result


if __name__ == "__main__":
    import sentry_vision
    print("Capturing webcam for face test...")
    frame = sentry_vision.capture_webcam()
    if frame:
        faces = extract_face_embeddings(frame)
        print(f"Faces detected: {len(faces)}")
        for emb, bbox in faces:
            print(f"  bbox={bbox} emb_dim={len(emb)}")
    else:
        print("No webcam frame.")
